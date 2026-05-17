**Log entries:**

---

### 2026-05-17 — OCR fallback for scanned PDFs

Some PDFs in the corpus are scanned images with no embedded text layer. GROBID processes these without error (HTTP 200) but returns TEI-XML containing the marker `[NO_BLOCKS]`, indicating it found nothing to extract. Previously the pipeline silently produced empty output for these papers. The problem was known for two specific files but is not limited to them — any scanned PDF triggers the same failure.

`_submit_to_grobid(pdf_path, include_coordinates)` extracted as a private method containing the raw HTTP call to `processFulltextDocument`, eliminating duplication between the initial attempt and the retry. `process_fulltext()` now calls `_submit_to_grobid`, checks for `[NO_BLOCKS]` via `_is_no_blocks()`, and if found calls `_run_ocr()` and retries. `_run_ocr()` shells out to `ocrmypdf` with `--skip-text --rotate-pages --deskew --output-type pdf`. The OCR'd copy is written to `<stem>_ocr.pdf` alongside the original; the original is never modified. If `_ocr.pdf` already exists it is reused without re-running OCR, consistent with the pipeline's caching approach elsewhere. Re-running on every pass would be wasteful; to get fresher output after a Tesseract upgrade, delete the `_ocr.pdf` files manually.

Flag rationale: `--skip-text` handles mixed PDFs (scanned body with a text-layer title page); `--rotate-pages` and `--deskew` correct orientation and skew common in scanned documents; `--output-type pdf` avoids PDF/A requirements that add no value here. Exit code 5 ("input already appears to have OCR") is handled gracefully. If `ocrmypdf` is not on PATH, the paper is skipped with a clear error message rather than crashing.

---

### 2026-05-17 — `ocrmypdf` declared as optional dependency

The existing `pyproject.toml` and `requirements.txt` were in sync and complete for the core pipeline (`requests`, `lxml`, `pyyaml`, `unidecode`, `tqdm`). The OCR fallback introduced the first system-level dependency: `ocrmypdf` wraps Tesseract, which must be installed at the OS level and cannot be expressed in pip metadata alone.

`ocrmypdf` is declared as an optional extra rather than a core dependency because the pipeline degrades gracefully without it. `pyproject.toml`: added `[project.optional-dependencies]` with `ocr = ["ocrmypdf>=16.0.0"]`. `requirements.txt`: `ocrmypdf` added as a commented-out entry in a labelled optional section. Both files note that Tesseract must be installed at the system level. All third-party imports across `bibvik/*.py` were audited before editing; the core dependency set was confirmed complete.

---

### 2026-05-17 — Fix: [NO_BLOCKS] arrives as HTTP 500, not 200

Initial testing revealed that GROBID 0.8.1 returns HTTP 500 (not 200) when a PDF has no text layer, with [NO_BLOCKS] in the response body. The original implementation only checked for [NO_BLOCKS] in 200 responses, so _submit_to_grobid was returning None on the 500 and the OCR fallback never fired. Fixed by adding an explicit branch in _submit_to_grobid: a 500 response whose body contains [NO_BLOCKS] is passed through to the caller rather than treated as a generic error, allowing process_fulltext to detect it and trigger OCR normally.

---

### 2026-05-17 — OCR replaces files in place, originals backed up

Rather than writing OCR'd copies to a separate directory, the OCR'd version replaces the original at its path and the original is backed up. Since Zotero uses linked files (not attached), replacing the file under the same name is transparent — Zotero opens the new version on next access with no metadata changes needed.
_run_ocr writes ocrmypdf output to a .ocr_tmp.pdf temp file, then moves the original to output/ocr/originals/<filename>, then moves the temp file into the original's place. The ocr_dir parameter on GrobidClient sets the backup directory root, and run.py passes output_dir / "ocr". Cache detection on subsequent runs: presence of output/ocr/originals/<filename> signals OCR has already been applied, so the file at the original path is used directly.

---

### 2026-05-17 — OCR fallback for scanned PDFs

Some PDFs in the corpus are scanned images with no embedded text layer.
GROBID returns HTTP 500 with `[NO_BLOCKS]` in the response body for these,
rather than the expected TEI-XML. Previously the pipeline logged an error
and skipped the paper entirely.

`_submit_to_grobid` now treats a 500 response containing `[NO_BLOCKS]` the
same as a 200, passing the body through to `process_fulltext` so the OCR
path can be triggered. `process_fulltext` detects `[NO_BLOCKS]` via
`_is_no_blocks()` and calls `_run_ocr()`.

`_run_ocr` writes ocrmypdf output to a `.ocr_tmp.pdf` temp file, moves the
original to `output/ocr/originals/<filename>`, then moves the temp file into
the original's place. The original path is never empty for more than the
duration of two filesystem operations. The OCR'd version replaces the
original under the same name, so Zotero (which uses linked files) opens it
transparently. On subsequent runs, presence of the backup in
`output/ocr/originals/` signals that OCR has already been applied and the
file at the original path is used directly.

`GrobidClient.__init__` takes a new `ocr_dir` parameter (default
`output/ocr`). Both `GrobidClient` construction sites in `run.py` pass
`output_dir / "ocr"`.

Flags passed to ocrmypdf: `--skip-text` (handles mixed PDFs with partial
text layers), `--rotate-pages`, `--deskew`, `--output-type pdf`.

Commit message not implemented due to redundancy:

```
Add OCR fallback for scanned PDFs

When GROBID returns [NO_BLOCKS] (HTTP 500), automatically run ocrmypdf,
replace the original PDF in place, and back up the original to
output/ocr/originals/. Uses a temp file during write to avoid leaving
the original path empty on interruption. Backup presence is the cache
signal on subsequent runs.

GrobidClient takes a new ocr_dir parameter; run.py passes output_dir/ocr
at both construction sites.
```

---

### 2026-05-17 — Atomic rename for OCR file operations

`shutil.move` was replaced with `Path.rename()` for the two filesystem
operations in `_run_ocr`. On POSIX, `rename()` is atomic when source and
destination are on the same filesystem, closing the window where `pdf_path`
could be left empty if the process is interrupted between the backup and
replace steps.

The temp→original move (`tmp_path.rename(pdf_path)`) is always
same-filesystem since both paths are in the Zotero directory, so it is
unconditionally atomic. The original→backup move crosses from the Zotero
directory to `output/ocr/originals/`; `rename()` is attempted first with a
`shutil.move` fallback for the cross-device case, which is not atomic but
unavoidable if the two directories are on different volumes.

---

