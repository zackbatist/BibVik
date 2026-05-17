# BibVik Decision Log — May 17, 2026

## OCR Fallback for Scanned PDFs

### 37. OCR fallback implementation in `grobid_client.py`

Some PDFs in the corpus are scanned images with no embedded text layer. GROBID processes these without error (HTTP 200) but returns TEI-XML containing the marker `[NO_BLOCKS]` where body content would otherwise appear, indicating it found nothing to extract.

Previously the pipeline silently produced empty output for these papers: zero references, zero detected citations, no body text. The problem was known for two specific papers (Baastrup 2014, Bergstøl 2004) but is not limited to them — any scanned PDF in the corpus triggers the same failure.

**Implementation:**

- `_submit_to_grobid(pdf_path, include_coordinates)` extracted as a private method containing the raw HTTP call to `processFulltextDocument`. This eliminates code duplication between the initial attempt and the retry.
- `process_fulltext()` now calls `_submit_to_grobid`, checks the result for `[NO_BLOCKS]` via `_is_no_blocks()`, and if found, calls `_run_ocr()` and retries.
- `_is_no_blocks(tei_xml)` checks for the `[NO_BLOCKS]` string. This string is GROBID's documented signal for this failure mode.
- `_run_ocr(pdf_path)` shells out to `ocrmypdf` with `--skip-text --rotate-pages --deskew --output-type pdf`. The OCR'd copy is written to `<stem>_ocr.pdf` in the same directory as the original; the original is never modified. If the `_ocr.pdf` already exists (from a previous run), it is reused without re-running OCR — consistent with the pipeline's caching approach elsewhere.

**ocrmypdf flags rationale:**
- `--skip-text`: Allows mixed PDFs (scanned body with a text-layer title page) without failing
- `--rotate-pages`: Corrects orientation errors common in scanned documents
- `--deskew`: Straightens skewed scan pages before recognition
- `--output-type pdf`: Avoids PDF/A requirements (colour profiles, metadata) that add no value here

**Exit code 5 handling:** ocrmypdf exits 5 when the input "already appears to have OCR." This should not occur since we only call `_run_ocr` after confirming `[NO_BLOCKS]`, but some malformed PDFs trigger it anyway. Handled by checking whether the output file was nonetheless written.

**Dependency:** Requires `ocrmypdf` on PATH, which itself depends on Tesseract at the system level. If missing, `_run_ocr` logs a clear error with installation instructions and returns `None`; the paper is then skipped cleanly rather than crashing.

**Scope:** The fallback is fully automatic — no config changes or flags required. Any scanned PDF in the corpus will be OCR'd on first encounter and the `_ocr.pdf` reused on subsequent runs. Re-running OCR on every pass would be wasteful and inconsistent with the pipeline's caching philosophy; if a better Tesseract version is available, delete the `_ocr.pdf` files manually to trigger a fresh run.

## Dependency Management

### 38. `ocrmypdf` declared as optional extra

The existing `pyproject.toml` and `requirements.txt` were in sync and complete for the core pipeline (`requests`, `lxml`, `pyyaml`, `unidecode`, `tqdm`). The OCR fallback introduced the first system-level dependency: `ocrmypdf` wraps Tesseract, which must be installed at the OS level and cannot be expressed in pip metadata alone.

`ocrmypdf` is declared as an optional extra rather than a core dependency because the pipeline degrades gracefully without it — scanned PDFs are skipped with a clear error message rather than crashing. Users with no scanned PDFs in their corpus have no reason to install it.

**`pyproject.toml`:** Added `[project.optional-dependencies]` section with `ocr = ["ocrmypdf>=16.0.0"]`. Install with `pip install -e ".[ocr]"`. A comment notes the Tesseract system-level requirement, since that cannot be expressed in pip metadata.

**`requirements.txt`:** `ocrmypdf` added as a commented-out entry in a clearly labelled optional section, with the same Tesseract installation note. Uncommenting is the manual equivalent of `pip install -e ".[ocr]"` for users working from `requirements.txt` directly.

All third-party imports across `bibvik/*.py` were audited before editing. The core dependency set was confirmed complete; no other additions were needed.
