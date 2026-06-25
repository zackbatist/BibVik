# Decision Log

*Starting from initial build (March 2026). Entries from March and April–May 2026 were reconstructed from session summaries and the previous decision_history.qmd. Entries from May 17, 2026 onward are recorded in real time.*

---

## March 2026 — Initial Build

### 2026-03 — Module structure and distribution

Project built as an installable Python package (`bibvik/`) with a CLI entry point (`run.py`) and YAML configuration. Initial version lacked a `sys.path` fix in `run.py`, causing `ModuleNotFoundError` when the package directory wasn't on the path. Fixed by adding `sys.path.insert(0, project_root)`. Distribution switched to zip to preserve directory structure. `pyproject.toml` had an incorrect `build-backend` string (`setuptools.backends._legacy:_Backend`); corrected to `setuptools.build_meta`.

### 2026-03 — LLM thinking mode

Qwen3.5 defaults to emitting a `<think>...</think>` reasoning block before the response. This caused the JSON parser to fail (response buried after the think block) and inflated inference time from ~12 seconds to ~4 minutes. Appending `/no_think` to the prompt had no effect. Fixed by passing `"think": false` as a top-level parameter in the Ollama API payload. `<think>` tag stripping added to the JSON parser as a defensive fallback.

### 2026-03 — Unified enriched/non-enriched analysis

Original design ran two separate LLM passes — context-only and content-enriched — producing near-identical outputs because almost no entries had abstracts (GROBID often fails to extract them). Fixed by adding `_build_content_lookup` which extracts abstract + first ~3000 chars of body text from processed PDFs. Redesigned to a single unified pass: enriched prompt when cited paper content is available, context-only otherwise. Each record tagged with `analysis_mode`. Halved LLM calls.

### 2026-03 — F1 PDF matching tiers

Original matching logic (DOI, exact title, author+year) failed for many papers because GROBID parses titles differently from different source PDFs. PDF filenames contain structured metadata (e.g., "Androshchuk 2010 - The Gift to Men..."), so filename-based matching added as a fallback tier. Five tiers established: DOI → exact title → author+year → fuzzy title with confirmation → filename parsing.

### 2026-03 — Fuzzy matching thresholds

Fuzzy matching was too loose: "Abrams 2012" was matching "Barrett 2010" (score 0.75), and the Zotero CSV matcher was matching "Aannestad 2018 - Allure of the Foreign" to `zori2013`. Fixed by raising thresholds (0.6 → 0.7 for title overlap, 0.6 → 0.8 for final acceptance), requiring both author AND year confirmation for fuzzy tiers, and tightening the Zotero matcher to only accept exact base citekey or base+single-letter suffix with 0.7+ title overlap.

### 2026-03 — Zotero CSV as tier 0

Added `bibvik/zotero_csv.py` to parse Zotero exports for exact PDF↔citekey matching. Added as tier 0 (tried before all other matching methods). The CSV provides author, year, title, DOI, and file attachment paths. Disambiguation suffixes (a/b/c) may not align between Zotero and GROBID extraction order, so the Zotero matcher uses title overlap for disambiguated entries rather than relying on exact citekey match.

### 2026-03 — Compound reference splitting

GROBID returned far fewer bibliography entries than expected for many humanities publications. Two causes: (a) the dash convention for repeated authors (—1987, —1989) where GROBID collapses multiple references into one `biblStruct`, and (b) GROBID's training bias toward STEM journal articles. Added a compound reference splitter in `tei_parser.py` that detects dash-year patterns and splits into individual entries, preserving the original author. Also detects author-boundary merges. Tested: Jansson b9 entry correctly splits into Jansson 1986, 1987, 1989, and Wikander 1978.

### 2026-03 — GROBID consolidation disabled

`consolidateCitations` set to `0` in all GROBID API calls. Enabling it causes GROBID to make a CrossRef API call for every reference, which is extremely slow for papers with many references and frequently causes timeout-induced truncation. Reference enrichment handled separately via `--resolve`.

### 2026-03 — Prompts as published methodology

All LLM prompts defined as readable string constants in source code so they can be inspected and cited in the methods section of the eventual paper.

### 2026-03 — Coverage reporting: _source_pdf bug

Coverage module had a bug where every F1 entry was classified as "has PDF" because `_source_pdf` pointed to the seed paper (the PDF the reference was extracted FROM, not the reference's own PDF). Fixed by excluding the seed paper filename from the "has PDF" check.

### 2026-03 — DOI cleaning

Unpaywall lookups were failing for some valid DOIs because GROBID extracted them with trailing punctuation or URL prefixes. Added robust DOI cleaning: strip URL prefixes, `doi:` prefixes, trailing punctuation, and unbalanced parentheses.

---

## April 2026 — Overhaul and Footnote Extraction

### 2026-04 — Major architectural overhaul

Replaced the sequential GROBID-first architecture with a unified five-method detection model. Previous architecture had 17 modules with detection as sequential "fixes." New architecture has three core modules: `detector.py` (all 5 methods applied to every paper), `resolver.py` (CrossRef + LLM resolution), `graph.py` (multi-generational graph builder). Removed: `citation_collector.py`, `footnote_extractor.py`, `reference_resolver.py`, `reference_audit.py`, `pdf_processor.py`, `citation_graph.py`. Key principle: no single source (including GROBID) is treated as authoritative; goal is maximal completeness across all sources.

### 2026-04 — Graceful Ctrl-C cancellation

SIGINT handler added that writes partial bibliography to `_partial_bibliography.json` before exiting. Prevents loss of work during multi-hour LLM runs.

### 2026-04 — LLM response caching

MD5 hash of paragraph text used as cache key. Only caches successful results. Prevents re-processing already-seen paragraphs across runs.

### 2026-04 — Paragraph batching attempted and reverted

Combining multiple paragraphs per LLM prompt was attempted to reduce inference calls. Broke response parsing — the model returned results the JSON parser couldn't handle. Reverted to per-paragraph calls with caching.

### 2026-04 — Footnote scope discovery

Corpus-wide scanning found footnote-embedded bibliographic references across 10 papers (not just Abrams 2012 as originally assumed). Made footnote extraction a first-class pipeline method rather than an edge case.

### 2026-04 — Footnote extraction implementation

Added `parse_tei_footnotes()` in `tei_parser.py` (finds `<note place="foot">` elements and unattributed notes with year patterns). Added `FOOTNOTE_EXTRACTION_PROMPT` and `extract_references_from_footnote()` in `llm_analyzer.py` (returns JSON array — not dict — so a new `_parse_llm_json_array()` helper was required). Prose-only footnotes filtered at the `extract_footnote_references` level using `min_footnote_length=40` and a year-pattern check, not inside the parser. `--footnotes` flag added; not included in `--all` (supplementary recovery step, not core pipeline).

### 2026-04 — cited_by never populated (confirmed bug)

Inspection of `bibliography.json` showed all 778 entries had `cited_by: []`. Fixes applied to `citation_graph.py` were correct but the on-disk output predated them. Reconstruction pass run against output files. `_process_one_f1` now explicitly appends `self.seed_citekey` to matched entry's `cited_by` after overwriting `_source_pdf`. Unmatched F1 entries created with `cited_by: [self.seed_citekey]` rather than `[]`.

### 2026-04 — F1 matching can claim already-matched entries

Root cause of zori2013 false-positive: when multiple F1 PDFs are processed sequentially, a bibliography entry already matched by one PDF could be re-matched by a subsequent PDF. Fixed: `_match_f1_to_existing` now skips any entry whose `_source_pdf` is not the seed PDF filename.

### 2026-04 — Integrity verification added

Comprehensive integrity check written and run after matching fixes: no self-citations, no dangling `cited_by` references, all F1 entries have seed in `cited_by`, no F2 entries have seed in `cited_by`. Result after fixes: 778 entries, 561 F1, 216 F2, 777 citation links, zero errors.

---

## May 17, 2026

### 2026-05-17 — OCR fallback for scanned PDFs

Some PDFs in the corpus are scanned images with no embedded text layer. GROBID returns HTTP 500 with `[NO_BLOCKS]` in the response body for these, rather than the expected TEI-XML. Previously the pipeline logged an error and skipped the paper.

`_submit_to_grobid(pdf_path, include_coordinates)` extracted as a private method containing the raw HTTP call to `processFulltextDocument`, eliminating duplication between the initial attempt and the OCR retry. `process_fulltext()` calls `_submit_to_grobid`, checks for `[NO_BLOCKS]` via `_is_no_blocks()`, and if found calls `_run_ocr()` and retries.

`_run_ocr` writes ocrmypdf output to a `.ocr_tmp.pdf` temp file, moves the original to `output/ocr/originals/<filename>`, then moves the temp file into the original's place. The original path is never empty for more than two filesystem operations. Since Zotero uses linked files, replacing the file under the same name is transparent — Zotero opens the new version on next access with no metadata changes needed. On subsequent runs, presence of the backup in `output/ocr/originals/` signals OCR has already been applied and the file is used directly.

`_submit_to_grobid` treats a 500 response containing `[NO_BLOCKS]` the same as a 200, passing the body through to `process_fulltext`. (Initial assumption that `[NO_BLOCKS]` would arrive as HTTP 200 was wrong — GROBID 0.8.1 returns HTTP 500.)

`GrobidClient.__init__` takes a new `ocr_dir` parameter (default `output/ocr`). Both construction sites in `run.py` pass `output_dir / "ocr"`.

Flags passed to ocrmypdf: `--skip-text` (handles mixed PDFs with partial text layers), `--rotate-pages`, `--deskew`, `--output-type pdf`.

### 2026-05-17 — Atomic rename for OCR file operations

`shutil.move` replaced with `Path.rename()` for the two filesystem operations in `_run_ocr`. On POSIX, `rename()` is atomic when source and destination are on the same filesystem, closing the window where `pdf_path` could be left empty if the process is interrupted between the backup and replace steps. The temp→original move is always same-filesystem (both in the Zotero directory) and is unconditionally atomic. The original→backup move crosses from the Zotero directory to `output/ocr/originals/`; `rename()` is attempted first with a `shutil.move` fallback for the cross-device case.

### 2026-05-17 — `ocrmypdf` declared as optional dependency

`pyproject.toml`: added `[project.optional-dependencies]` with `ocr = ["ocrmypdf>=16.0.0"]`. `requirements.txt`: `ocrmypdf` added as a commented-out entry in a labelled optional section. Both files note that Tesseract must be installed at the system level. The pipeline degrades gracefully without ocrmypdf — scanned PDFs are skipped with a clear error message rather than crashing. All third-party imports across `bibvik/*.py` audited; core dependency set confirmed complete.

### 2026-05-17 — Stratified audit sampling tool

Added `bibvik/audit.py` and `--audit` flag to `run.py`. Draws a stratified random sample from the citation graph and writes `output/audit_sample.md` for human review and annotation.

Strata: CrossRef-resolved entries (check that matches are correct, not merely plausible), unresolved entries (check raw citation parsing), minimal-completeness entries (check for extraction failures), suspected duplicate pairs (exhaustive above title similarity threshold — not sampled), OCR-source entries (check for character errors from OCR), and non-English source papers per language (stubbed — requires language detection from item C). Where a stratum has fewer entries than n, all are included and the shortfall noted.

Fixed random seed (default 42) ensures the same sample is drawn on every run against the same graph state. All parameters overridable: `--audit-n`, `--audit-seed`, `--audit-threshold`.

Smoke test against the current graph state immediately revealed real CrossRef mismatches — entries resolved with high confidence to wrong papers in unrelated fields (psychology, pedagogy). This is a known failure mode of CrossRef DOI matching on short or ambiguous reference strings and needs to be addressed in the resolver.

Methodological documentation in `docs/audit-sampling-method.md`.

### 2026-05-17 — Documentation consolidation

Merged `decision_history.qmd` and both Claude session summary files
(March 2026, April–May 2026) into the running decision log. Session
summary narrative content (presentation revisions, visualization work)
omitted as non-decision-relevant. `architecture.qmd` updated: module
map now includes `audit.py`, marks tabled modules, reflects
grobid_client OCR fallback and updated CLI flags. Both summary files
and the old `decision_history.qmd` can now be deleted.

### 2026-05-17 — coverage.py simplified

Removed structured JSON report-building, `_metadata` branch, F2
planning section, and `_entry_summary` helper. Output is now
`coverage.md` — a plain Markdown file with two lists (missing PDFs,
OA-available papers). The `download_oa_papers` function signature
simplified to take bibliography directly rather than a coverage report
dict. `run.py` updated accordingly. Unpaywall item H flagged in both
module docstring and report output.

### 2026-05-17 — Lazy imports for tabled modules; tei_xml inconsistency fixed

`context_extractor`, `llm_analyzer` (Stage 3), and `cluster_analyzer`
(Stage 4) moved from top-level imports in `run.py` to lazy imports
inside their respective stage blocks. They no longer load during normal
`--extract` and `--iterate-f1` runs, removing startup overhead and
eliminating import failures if their dependencies aren't installed.

Removed `tei_xml` from the seed paper's `processed_papers` entry in
`graph.py`. The TEI-XML is written to disk at `output/tei/` immediately
after GROBID processing and does not need to be held in memory. F1
papers never stored `tei_xml` in `processed_papers`; the seed paper was
the only exception. `_save_graph_state` in `run.py` was already
explicitly excluding `tei_xml` from serialisation, confirming it was
never intended to persist. Both entries now have identical structure.

### 2026-05-17 — CrossRef resolver: tightened matching, title/context plausibility check

The audit tool smoke test revealed CrossRef was returning high-confidence
wrong matches — entries for Androshchuk (2018), Zori (2013), and Clarke
(2017) had been resolved to a pedagogy paper, a psychology paper, and a
cosmetics handbook respectively. Root cause: author matching used only a
4-character prefix, and confidence was set to "high" whenever a DOI was
present, regardless of match quality.

Two changes to `_try_crossref`:

**Author matching** — full normalised surname now required. Short
surnames (≤3 chars after normalisation) retain prefix matching as a
fallback for truncation artifacts.

**Title/context plausibility** — at least one content word (4+ chars,
not a stopword) from the CrossRef title must appear in the combined
citation contexts. If no overlap exists, the match is rejected. If the
check is inconclusive (no content words in title, or empty contexts),
the match is accepted but confidence is downgraded to medium. This
catches domain mismatches without requiring a curated subject-area list.

**Confidence scoring** now reflects actual match quality: `high` requires
full author match + overlap confirmed + DOI present; `medium` covers
confirmed match without DOI, or inconclusive overlap check.

All three known false positives correctly rejected in smoke testing.
Known limitation: non-English citation contexts may lack vocabulary
overlap with English CrossRef titles even for correct matches. These
cases are downgraded to medium confidence rather than rejected, and
flagged for audit review. Documented in `docs/resolver-method.md`.

### 2026-05-17 — Additional data capture: section headings, paragraph numbers, language, affiliations

Four new fields captured during graph generation and stored in
`processed_papers`.

**Section headings** (`section_heading` on each paragraph): extracted
by walking each paragraph's ancestor `<div>` elements and collecting
their `<head>` children into a breadcrumb string (e.g. `"Results >
Typological Analysis"`). `_build_heading_map()` walks the body tree
once and maps `id(p_element) → heading` to avoid re-traversal per
paragraph.

**Paragraph numbers** (`paragraph_index` on each paragraph): 1-based
sequential index assigned during `parse_tei_body()`. GROBID does not
number paragraphs; the index is generated by the parser. Page numbers
were considered and not adopted — GROBID produces no `<pb>` elements
in standard mode, and paragraph index provides equivalent positional
information more reliably across corpus variants.

**Language detection** (`language` in `processed_papers[pdf_name]`):
`detect_language()` in `tei_parser.py` runs `lingua` against the first
~2000 characters of body text. Detector restricted to six languages
(English, Norwegian Bokmål, Danish, Swedish, German, French) for
accuracy and speed. `lingua` chosen over `langdetect` for determinism
by design and substantially better accuracy on closely related
Scandinavian languages. GROBID's own `xml:lang` attribute not used —
inspection found it unreliable (non-English papers tagged `en`). The
`language` field is serialised in `_save_graph_state`. This unblocks
the language strata in the audit tool.

**Author affiliations** (`affiliation` on each author dict in
`header`): `_parse_affiliation()` extracts `orgName` and address
sub-elements from `<affiliation>` children of `<author>` in the TEI
header. Quality is inconsistent — GROBID sometimes places the author
name in the institution field. Data stored as-is; reconciliation
against ROR/GRID deferred. Affiliations are properties of the citing
paper's authors and stored in `processed_papers`, not in bibliography
entries.

`lingua-language-detector>=2.0.0` added as a core dependency. The
correct constant for Norwegian Bokmål in this version is
`Language.BOKMAL` (not `Language.NORWEGIAN_BOKMAL`). Documented in
`docs/additional-data-capture.md`.

### 2026-05-17 — Methods documentation: citation detection and GROBID configuration

`docs/methods/citation-detection.md`: Documents the five-method detection
design and its rationale. Covers the principle that no single source is
treated as authoritative, per-method description (strengths, weaknesses,
implementation location), merging and deduplication logic, and known
limitations. Cites Tkaczyk et al. (2018) as precedent for ensemble
approaches to reference parsing. Notes that LLM prompt analysis is
deferred to a separate document pending full corpus run.

`docs/methods/grobid-configuration.md`: Documents GROBID 0.8.1
configuration decisions. Key entries: `consolidateCitations: 0`
(per-reference CrossRef calls cause timeout-induced truncation),
`includeRawCitations: 1` (used by compound splitter and audit review),
coordinates not requested (overhead not justified). Documents known
issues: container instability on ARM emulation under long runs, and
the domain gap between GROBID's published benchmarks (biomedical
English) and the BibVik corpus. Cross-references OCR fallback,
resolver, audit sampling, and data capture docs rather than
duplicating content.

### 2026-05-17 — Codebase audit: shared helpers, private API leakage, dead code

**Shared helpers in `utils.py`:** `extract_year()` and `norm_author()` added.
Previously defined independently in `biblatex_model.py`, `detector.py`, and
`graph.py` (`_extract_year`) and in `detector.py`, `resolver.py`, and
`llm_analyzer.py` (`_norm`). Local versions retained as thin delegation
wrappers to avoid touching every call site.

**Private API leakage in `detector.py`:** Was importing `_parse_xml`,
`_get_text`, `TEI_NS`, and `NS` directly from `tei_parser.py` — all
underscore-prefixed private symbols. Three public functions added to
`tei_parser.py`: `parse_tei_xml()` (parses TEI string, returns root
element), `get_body_text()` (extracts raw body text string), and
`TEI_NAMESPACE` (public alias for the namespace URI). `detector.py`
updated to use these.

**Inconsistent `unidecode` imports:** `normalize.py` and `llm_analyzer.py`
were importing `unidecode` lazily inside function bodies. Moved to
module top level, consistent with all other modules.

**Dead code:** `build_coverage_metadata()` in `metadata.py` removed —
no longer called after `coverage.py` was simplified to produce Markdown
output rather than structured JSON.

**Unused import:** `import time` removed from `run.py`.

### 2026-05-17 — Remove unused import and dead config keys

`graph.py`: `unidecode` import removed. It was used directly in `_norm_author`
which now delegates to `utils.norm_author` — the import became unused after
the previous cleanup commit.

`config.yaml`: two keys commented out. `concurrency` under `grobid` was
never read by any code — parallel GROBID requests were planned but not
implemented. `save_tei_xml` was also never read — TEI-XML is always saved
to `output/tei/` unconditionally. Both are retained as comments with
explanatory notes rather than deleted, in case they become relevant later.

### 2026-05-17 — CLI output redesign

`utils.py`: `setup_logging()` rewritten with two handlers. File handler
(DEBUG) writes full detail with timestamps and module names to
`output/bibvik.log`. Stream handler (WARNING+) shows only warnings and
errors on stdout. Terminal output during runs now comes from explicit
`print()` calls, eliminating the noisy timestamp+module prefix on every
line.

`run.py`: Stage 1 and 2 replaced with structured `print()` output.
Stage 2 per-paper block shows: GROBID bibliography and paragraph
counts, per-method citation counts with source labels, discrepancy
between bibliography and body citations, CrossRef/unresolved resolution
counts, language tag for non-English papers, OCR notice when applied,
elapsed time, and ETA. Stage 2 summary shows total time, success/fail
counts, new entries added, and full resolution breakdown. Startup
summary shows corpus size, cached count, LLM availability, and Zotero
CSV status. Stages 3, 4, coverage, and audit updated to the same
plain-print style.

`graph.py`: `progress_callback` signature changed to keyword arguments
carrying the full per-paper data: `detection`, `n_crossref`,
`n_unresolved`, `language`, `ocr_applied`, `failure_reason`, `elapsed`.

### 2026-05-18 — lingua: warn once, cache detector

`detect_language()` in `tei_parser.py` was rebuilding the lingua
detector on every call — one per paper. Added `_lingua_detector` as a
module-level singleton: built once on first call, reused thereafter.
Added `_lingua_warned` flag so the "lingua not installed" warning
prints once per run rather than once per paper.

### 2026-05-18 — UI improvements, llama-server backend, audit and summary fixes

**Progress reporting:** `start_callback` added to `process_f1_papers` —
prints paper header and "Sending to GROBID..." immediately when processing
starts, so hangs are visible. `_process_one_f1` now returns `(bool, str)`
tuple so failure reasons surface inline rather than as "unknown error".
Prefetch timeout reduced from hardcoded 300s to `grobid.timeout + 30s`.

**Stage 2 summary:** Resolution counts (CrossRef, LLM, stub, unresolved)
now reflect only entries added in the current run. Existing citekeys
snapshotted before the F1 loop; set difference identifies new entries.

**llama-server backend:** `LLMAnalyzer` and `_llm_query_array` support
`backend="llama_server"`, using `/v1/chat/completions` with standard
`temperature`/`max_tokens` parameters. Health check uses `/health`.
Both Methods 4 and 5 pass `backend` from `llm_config`. Configured via
`llm.backend` in `config.yaml` (default `"ollama"`). To switch:
set `backend: "llama_server"` and update `base_url`. No code changes needed.

**Audit duplicate detection:** `_stratum_duplicates` now samples up to
500 entries before pairwise comparison, reducing from O(n²) on the full
bibliography to ~125k comparisons. Noted in rendered output.

### 2026-05-18 — Resolution/enrichment split; CrossRef removed from identification

Audit results (May 2026) showed ~70-80% false positive rate in
CrossRef-resolved entries. Root cause: CrossRef author+year queries are
too weak — CrossRef always returns a result regardless of whether the
work is in its database, and the BibVik corpus is systematically
underrepresented in CrossRef (Scandinavian monographs, museum
publications, grey literature). Title/context overlap filtering was
insufficient when contexts were empty or short.

**`resolver.py`** rewritten: CrossRef identification removed entirely.
`resolve_citations()` now uses only the LLM for context-based metadata
inference. Entries with no contexts become stubs immediately. The `email`
parameter is retained for API compatibility but unused. `_try_llm` is
now backend-aware (ollama/llama_server). Unused helpers removed.

**`enricher.py`** (new module): CrossRef is used strictly for enrichment
of already-identified entries — never for identification. Two strategies:
(1) DOI lookup: reliable, fills volume/pages/given names/publisher;
(2) title query at ≥0.85 similarity threshold: precise, fills DOI and
missing metadata. Both are additive only — no existing fields overwritten,
no graph structure changed. OpenAlex author enrichment fills full given
names, ORCID, OpenAlex ID, and institutional affiliation (ROR) for
paper header authors. OpenAlex integrates ORCID as a data source so no
separate ORCID query is needed.

**`run.py`**: `--enrich` flag added, with `--enrich-bib-only` and
`--enrich-auth-only` for selective passes. `--enrich-threshold` controls
title similarity threshold (default 0.85).

**`docs/methods/resolver-method.md`** fully rewritten documenting the
design rationale, approaches considered and rejected (restricted CrossRef,
deferred CrossRef), and enrichment design. Previous resolver-method.md
content archived in this log entry for reference.

### 2026-05-18 — Replace OpenAlex author enrichment with CrossRef DOI lookup

OpenAlex name-search produced wrong matches on common Scandinavian
surnames — Lund matched a wrong Lund, Hansen matched a pharmaceutical
researcher, Andersson matched a US academic. OpenAlex always returns a
result regardless of whether the person is in its database, replicating
the same fundamental problem as CrossRef identification-mode queries.

Replaced with CrossRef DOI-based author enrichment. For each F1 paper
with a DOI in its header, the CrossRef record is fetched and its author
list is matched to GROBID's author list by normalised family name.
Matched authors receive full given names (when CrossRef has full forms
vs GROBID's initials) and ORCID identifiers (when submitted by the
publisher). If no DOI, no CrossRef record, or no family name match:
nothing is changed. No match is better than a wrong match.

This approach requires no disambiguation — the paper's DOI is a unique
identifier, and the author's identity is derived from the specific work
they wrote. Coverage depends on DOI availability and publisher metadata
submission practices; older and non-English publications will have lower
coverage, which is acceptable.

### 2026-05-18 — NOAUTHOR citekeys; noise filter for bibliography entries

**Citekey generation:** Entries with no author now receive sequential
`NOAUTHOR1`, `NOAUTHOR2`, etc. citekeys rather than `unknownyear` or
`unknownndt`. The previous pattern was opaque and produced meaningless
keys like `unknown2022a`. The new pattern is explicit and searchable.
Year is excluded from NOAUTHOR keys since it adds no disambiguating
value for anonymous works.

**Noise filter:** `parse_tei_references()` now rejects entries where
both author and title are empty after parsing. GROBID occasionally
extracts footnote numbers, running headers, or other non-reference
content as bibliography entries (e.g. "3 Journal of Archaeological
Research (2022) 30:169-229" with no author or title). These are not
valid references and would generate meaningless citekeys.

### 2026-05-18 — Citekeys, noise filter, affiliation filter, name correction, UI

**NOAUTHOR citekeys** (`utils.py`): Anonymous entries now receive
`NOAUTHOR1`, `NOAUTHOR2`, etc. instead of `unknownyear`. Year excluded
from NOAUTHOR keys.

**Noise filter** (`tei_parser.py`): `parse_tei_references()` rejects
entries with no author AND no title — GROBID extraction artifacts
(footnote numbers, running headers). Confirmed fix for `unknown2022a`.

**Publisher affiliation filter** (`tei_parser.py`): `_parse_affiliation()`
now discards institution fields containing known publisher names
(Blackwell, Wiley, Routledge, Oxfam, etc.). GROBID sometimes extracts
publisher addresses from PDF headers as author affiliations.

**Transposed name correction** (`enricher.py`): Author enrichment now
detects when GROBID swapped family and given name (e.g. Andersson Strand
parsed as family="Eva", given="Andersson"). When CrossRef DOI author
list has no match by family name but matches by given name, the parse
is corrected using CrossRef's version as canonical.

**ETA for enrichment** (`enricher.py`, `run.py`): `enrich_bibliography()`
accepts a `progress_callback(done, total)`. `run.py` uses it to show
a live `N/total · ~Xm remaining` line during enrichment.

**UI terminology** (`run.py`): "bibliography entries" → "entries in
reference list" in per-paper output. "bibliography" → "bibliography.json"
in Stage 1 summary. Removed "Resolved: 0 via CrossRef" line from
per-paper output — meaningless since CrossRef identification was removed.

### 2026-05-18 — Remove resolution counts from Stage 2 summary

Stage 2 summary was showing "Resolved: 0 CrossRef · 0 LLM · 0 stub ·
686 unresolved" — meaningless since CrossRef identification was removed
from the resolver. The counts and associated variables (`crossref_total`,
`llm_total`, `stub_total`, `unresolved_total`, `_stage2_existing_keys`)
were removed. Stage 2 summary now shows only papers succeeded/failed
and new entries in bibliography.json.

Per-paper output terminology: "bibliography entries" → "entries in
reference list", "bibliography" → "bibliography.json".

### 2026-05-18 — GROBID health monitoring and automatic restart

GROBID crashed twice during testing (Anchukaitis 2017), requiring
manual container restart before processing could continue. For the full
382-paper run this would be unacceptable.

`grobid_client.py`: `GrobidClient` takes a new `container_name`
parameter (default `"grobid-server"`). New `restart_if_down()` method
runs `docker restart <container_name>` and polls `is_alive()` at 5s
intervals for up to 120s. `_submit_to_grobid()` now calls
`restart_if_down()` on `ConnectionError` and retries the request once
if the restart succeeds. If GROBID doesn't come back up within 120s or
docker is not available, the paper is skipped as before.

`config.yaml`: `grobid.container_name` added. Set to empty string to
disable automatic restart. `utils.py`: default added to `load_config`.
Both `GrobidClient` construction sites in `run.py` pass `container_name`.

The restart mechanism requires the container to have been started with
`--name grobid-server` (or whatever name is configured). The standard
startup command in the documentation already uses this name.

### 2026-05-18 — Remote LLM support, CLI flags, cluster launch script, gitignore

**CLI flags** (`run.py`): Three new flags. `--remote` switches the LLM
base_url, backend, and model to the cluster configuration
(`llm.remote_url`, `llm.remote_backend`, `llm.remote_model` in
config.yaml). `--model` overrides the model from config at runtime.
`--no-think` sets a flag in llm_cfg (prompts already include `/no_think`
suffix for Qwen3, but flag available for future use). `_llm_status()`
now shows local/remote indicator in Stage 2 header.

**config.yaml** now gitignored. `llm` section updated — `base_url` and
`backend` now point to LM Studio (localhost:1234, llama_server).
Remote cluster keys added: `remote_url: http://localhost:11435` (via
SSH tunnel), `remote_backend: ollama`, `remote_model: qwen2.5:7b`.

**config.example.yaml** (new): public template with all sensitive values
(paths, email, cluster URL) replaced by placeholders. Documents the
SSH tunnel setup for --remote. Copy to config.yaml and fill in locally.

**launch_bibvik_llm.sh** (new, gitignored): cluster LLM launch script
replacing the admin's prototype `launch_ollama.sh`. Supports `--gpu N`,
`--model`, `--backend` (ollama or llama_server), `--port`, `--no-think`.
Port 11435 and container names `ollama_bibvik`/`llama_bibvik` to avoid
conflict with `launch_ollama.sh` (other project). Models at
`/home/zack/models` (SSHFS → NAS, 3.8T free). SSH tunnel:
`ssh -L 11435:localhost:11435 zack@132.216.183.78`.

**.gitignore** (new): excludes config.yaml, launch_bibvik_llm.sh,
output/, PDFs, Exported_Items.csv, Python artifacts, editor files.

### 2026-05-22 — Footnote number filter; language stratum message fix

`tei_parser.py`: Added post-processing step in `_parse_biblstruct` to
strip leading footnote numbers from titles. GROBID occasionally absorbs
footnote reference numbers ("58 Fanning..." or "61 Abrams...") into the
title field when parsing footnote-style citations. Pattern: leading
digits followed by whitespace are stripped.

`audit.py`: Removed stale "language detection not yet implemented"
messages in docstring, log output, and rendered audit report. Language
detection is implemented via lingua — the stratum is empty when lingua
is not installed or no non-English papers are in the current graph state.
Messages updated to reflect the actual situation.

### 2026-05-24 — Multi-GPU parallel LLM processing

**Problem:** Processing 382 papers sequentially with one LLM endpoint
would take ~9 hours on a single GPU. The cluster has 10 GPUs available.

**Solution:** `process_f1_papers()` now supports distributing papers
across multiple LLM endpoints in parallel. When `llm.extra_urls` is
set in config, papers are assigned round-robin across all endpoints
using a thread pool. GROBID processing remains sequential (single
GROBID instance). Shared bibliography and processed_papers state is
protected by a threading.Lock.

Single-endpoint path is unchanged — no behaviour change when
`extra_urls` is empty.

`launch_bibvik_llm.sh` updated to support multi-instance launch:
`--gpus 5,6,7` launches one Ollama container per GPU on ports
11440, 11441, 11442. `--tensor N` for llama-server tensor
parallelism (single model across N GPUs). `--stop` kills all
BibVik containers. Container names include GPU ID to avoid conflicts.

**Expected throughput:** 3 parallel instances → ~3x speedup → full
382-paper run in ~3 hours instead of ~9 hours.

**Config:**
```yaml
llm:
  base_url: "http://localhost:11440"
  extra_urls:
    - "http://localhost:11441"
    - "http://localhost:11442"
```

### 2026-05-24 — Fix multi-GPU parallel processing

The initial multi-GPU implementation used a two-phase approach: GROBID
all papers sequentially, then submit all LLM jobs in parallel. In
practice this meant all LLM work was queued before any worker started,
and the round-robin assignment sent most work to the first worker before
the others were ready. All requests were going to port 11440 (GPU 4).

Fixed with a producer-consumer pipeline. A GROBID producer thread feeds
a bounded queue (capacity: 2 × n_workers). N LLM worker threads each
pull from the queue and process on their assigned endpoint. GROBID and
LLM now overlap — as soon as paper N's GROBID result is ready it goes
into the queue and whichever worker is free picks it up. With 4 workers,
4 papers are in LLM simultaneously while GROBID processes the next one.

### 2026-05-25 — Multi-GPU parallel processing

Implemented parallel paper processing across multiple LLM endpoints. Papers are divided round-robin across worker threads before processing starts, each worker owning its batch and processing independently (GROBID then LLM per paper). Shared state is protected by a threading.Lock passed to `_process_one_f1`; LLM inference runs without the lock.

GPU access on the shared cluster required membership in the `video` group. Containers were starting CPU-only despite `--gpus` because the nvidia container runtime needs `/dev/nvidia*` access, gated by that group. Fixed by `sudo usermod -aG video <username>`.

`OLLAMA_KEEP_ALIVE=-1` set in all containers to prevent model eviction between papers. Launch script now runs a warm-up inference after pull so the model is in VRAM before the pipeline starts.

A bug in the progress callbacks caused "LLM unavailable" for every paper regardless of actual results. `processed_papers["detection"]` is already the method_counts dict, but callbacks called `.get("method_counts")` on it — always returning None. `fix_cache.py` had the same bug and deleted all cached papers when run. Both fixed by reading `paper_data.get("detection", {})` directly.

LLM query methods now retry up to 3 times with backoff on timeout or connection error.

### 2026-05-25 — fix_cache.py utility

Added CitationAnalysis/fix_cache.py. Scans _graph_state.json for
papers where llm_body_scan is None and removes them from the cache
so they are reprocessed. Required after discovering that a bug in
the progress callback caused all papers to appear as LLM unavailable,
which caused an earlier version of the script to incorrectly delete
all cached papers. Fixed version reads detection directly from
processed_papers rather than looking for a nested method_counts key.

### 2026-05-25 — Chronological event-based TUI

Replaced per-paper start/phase/progress output with a chronological
event stream. Each processing stage (GROBID, OCR, LLM body, resolve)
emits a timestamped start and done line with citekey and elapsed time.
A threading.Lock ensures atomic output from parallel workers so lines
from different papers don't interleave. Only run.py and graph.py
changed.

### 2026-05-25 — Incremental graph state saving

Graph state was previously only saved at the end of a complete run.
A VPN drop after an hour of processing lost all cached results.
Fixed by saving graph state inside the _progress callback after each
paper completes. Only the paper currently being processed at the
moment of interruption is lost on an unclean exit.

### 2026-05-25 — Move resolve outside state lock

LLM resolve was running inside the state lock, serialising all workers
during resolution. Moved resolve_citations() call outside _lock_ctx so
all 5 workers can resolve simultaneously, each using their own GPU
endpoint. Only the write-back of resolved records into the bibliography
is locked. Each worker already owns its endpoint for the full paper
lifecycle (GROBID, body scan, resolve) via worker_llm_cfg — the lock
change makes this parallelism real.

Also fixed resolver.py default model from qwen3.5:35b to qwen2.5:7b.

### 2026-05-25 — Tabular TUI with consistent citekeys

run.py: Fixed-width tabular columns — timestamp (8), citekey (22),
event (16), elapsed (6). Removed ETA from completion lines. Applied
to _event, _progress, and _seed_event.

graph.py: Provisional citekey derived from Zotero map before GROBID
so all events use a consistent identifier from the start rather than
a filename stub.

### 2026-05-25 — Add post-processing pipeline

Added bibvik/postprocess.py with 10 cleaning passes based on patterns
identified in the audit sample: letter prefix artifacts from GROBID
year+suffix parsing, hyphenated line-break titles, oversized titles
from compound citation blowout, DOI/date/page normalization, LLM
placeholder title removal, compound citation flagging, and orphaned
cited_by detection. Wired into run.py as --postprocess. Cross-script
duplicate detection is a placeholder for future implementation.

### 2026-05-25 — Add post-processing pipeline

Added bibvik/postprocess.py with 10 cleaning passes based on patterns
identified in the audit sample: letter prefix artifacts, hyphenated
line-break titles, oversized titles from compound citation blowout,
DOI/date/page normalization, LLM placeholder title removal, compound
citation flagging, cross-script duplicate detection (Cyrillic/Latin
pairs via transliteration table), and orphaned cited_by detection.
Wired into run.py as --postprocess.

### 2026-05-25 — Add post-processing pipeline

Added bibvik/postprocess.py with 14 cleaning passes based on patterns
identified in the audit sample: letter prefix artifacts, hyphenated
line-break titles, oversized titles, DOI/date/page normalization,
volume extraction from pages field, LLM placeholder title removal,
entry type reclassification, compound citation flagging, cross-script
duplicate detection via transliteration, orphaned cited_by detection,
missing given names flagging, and editor/author confusion flagging.
Wired into run.py as --postprocess.

### 2026-05-25 — Add post-processing pipeline

Added bibvik/postprocess.py with 19 cleaning passes based on patterns
identified in the audit sample. Fixes: letter prefix artifacts, hyphenated
line-break titles, oversized titles, DOI/date/page normalization, volume
extraction from pages, ALL CAPS normalization, LLM placeholder title
removal, entry type reclassification. Flags: citekey suffix collisions,
compound citations, cross-script duplicates via Cyrillic transliteration,
citing papers not in corpus (corpus coverage signal), titles containing
publisher/location strings, near-duplicate entries by token overlap,
missing given names, editor/author confusion, unprocessed source PDFs.
Wired into run.py as --postprocess.

### 2026-05-25 — Add graph export module

Added bibvik/exporter.py with four export formats: GraphML (for
R/igraph — reads natively, direction preserved), GEXF (for Gephi),
CSV edgelist (source/target pairs), and CSV node table (title, year,
generation, entry_type, first_author, doi, completeness, in/out
degree). Wired into run.py as --export. GraphML is the recommended
format for analysis; GEXF for visualization.

### 2026-05-25 — Fix bibliography.json save format

_save_bibliography was wrapping the bibliography in {"_metadata": ...,
"entries": ...} before writing. Everything reading bibliography.json
expected a flat citekey→entry dict, so --enrich always produced a
2-entry file. Fixed to write the flat dict directly.

### 2026-05-25 — Tighten _find_by_author_year matching (item N)

Removed loose substring containment checks from author matching during
citation resolution. Now requires exact normalized family name match,
or prefix match only when the shared prefix is ≥5 chars. Prevents
false positives like "Lee" → "Leech" or "Li" → "Lindqvist".

### 2026-05-25 — Fix race condition in progress callback

The sequential path progress callback iterated self.bibliography.values()
to count crossref-resolved and unresolved entries without holding the
shared lock. Parallel workers writing to bibliography simultaneously
caused "dictionary changed size during iteration" on Holst 2010 and
Myrberg 2008. Fixed by wrapping the iteration in _lock.

### 2026-06-01 — Fix missing json import in run.py

Added import json to top-level imports. The --export block used
json.loads without importing json, causing a NameError at runtime.

### 2026-06-01 — Fix postprocess pass 3 — oversized titles

Pass 3 was clearing titles over 300 chars to empty and storing the
first 200 chars in _title_too_long. This was destructive — even
compound citation blowout entries may contain a real title. Changed
to flag only: _title_too_long: True is added but the title field is
preserved unchanged.

### 2026-06-01 — Fix postprocess pass 10 — entry type reclassification

Article reclassification was triggering on any non-empty journaltitle,
including publisher and series names inserted by CrossRef enrichment.
Fixed to require volume, issue, or pages alongside journaltitle before
reclassifying as article. Reverted bad book→article reclassifications
from the first postprocess run.

### 2026-06-01 — Prevent incollection→inbook reclassification in pass 10

1594 incollection entries were being reclassified to inbook when the
editor field was missing. Missing editor data should not change the
entry type — the booktitle structure implies an edited volume. Added
explicit guard to skip incollection→inbook reclassification. Reverted
all 1594 affected entries.

### 2026-06-01 — Guard book→inbook false reclassification in pass 10

Some book entries had booktitles matching their own title (series names,
self-referential) causing false reclassification to inbook/incollection.
Added title/booktitle similarity check — entries where the normalized
title and booktitle overlap are kept as book.

### 2026-06-01 — Require page range for article reclassification

Monograph series (AUN, Acta Archaeologica Lundensia, etc.) were being
misidentified as journals because they have volume numbers and are
stored in journaltitle. Added requirement that pages must be a range
(not a single number) for article reclassification. Reverted affected
book→article entries.

### 2026-06-09 — Major pipeline refactor

Restructured normalisation, deduplication, postprocessing, and audit
to reflect correct order of operations and eliminate postprocess as a
cleanup patch:

- Per-entry fixes (title, date, DOI, pages, oversized, entry type for
  misc) moved from postprocess into normalize_entry() in normalize.py,
  applied at graph construction time.

- _find_duplicate() gains cross-script step 4 via ALA-LC transliteration
  table. Auto-merges on title overlap ≥50%, flags remainder for audit.

- Per-paper CrossRef enrichment added: enrich_entry() called for each
  new F2 entry, enriched titles available for subsequent deduplication.

- postprocess.py rewritten to 3 LLM-only passes: entry type
  reclassification (post-enrich, all types), near-duplicate resolution
  (trivial title blocklist, LLM for title-rich pairs), compound citation
  splitting.

- audit.py gains 4 new flag strata: citekey collisions, oversized titles,
  missing given names, near-duplicate candidates.

- All methods docs updated to reflect new architecture.

### 2026-06-09 — Move compound splitting inline; remove inbook reclassification

Compound citation splitting moved from --postprocess into graph.py,
called inline during per-paper processing for entries flagged
_possibly_compound. Split entries now participate in deduplication
against subsequent papers rather than sitting as garbage entries until
the full corpus is done. _split_compound_entry() added as a module-level
helper in graph.py.

postprocess.py now has 2 passes only: entry type reclassification
(post-enrich) and near-duplicate resolution.

normalize.py: misc→inbook reclassification removed. Missing editor data
is insufficient to distinguish inbook from incollection in a corpus
dominated by edited volumes. Only article (journal+volume/pages) and
incollection (booktitle+editors) are reclassified from misc.

### 2026-06-09 — Flag GROBID ID artifacts as author (todo Y)

Single-letter family names (b, c, etc.) are GROBID internal reference
IDs leaking into the author field. normalize_entry() now flags these
with _grobid_id_as_author: True at creation time. audit.py surfaces
them as a dedicated stratum for manual correction.

### 2026-06-09 — Replace hand-rolled transliteration with domovyk (todo Z)

_transliterate_author() in graph.py now uses domovyk's ALA-LC
Romanization tables instead of a hand-rolled character translation
table. Domovyk covers 8 Cyrillic scripts and matches the standard
used by CrossRef and library catalogues. Falls back to the hand-rolled
table if domovyk is not installed. Added domovyk to requirements.txt.

### 2026-06-09 — Update methods docs for domovyk, OCR aligner, compound splitting

deduplication-normalisation.md: documents domovyk as the ALA-LC
transliteration library for cross-script dedup, with fallback details.

audit-sampling.md: adds OCR text aligner as a considered-not-adopted
approach in the Approaches section, consistent with the note already
in deduplication-normalisation.md.

llm-prompts.md: compound splitting prompt corrected to reflect inline
placement in graph.py, not postprocess.py. Includes first_author_given
field added in the graph.py version.

### 2026-06-10 — Add Method 6: LLM bibliography re-parse from raw TEI text

Added a sixth citation detection method that bypasses GROBID's structured
bibliography parser entirely for cases where it fails.

GROBID writes a <div type="references"> in the TEI <back> section
containing the original reference list as continuous raw text, including
entries that span PDF page breaks. GROBID's biblStruct parser produces
garbage entries for these (page-break fragments), and also pre-splits
dash-abbreviated author series into separate biblStructs that lose their
author context. The raw div text is unaffected by both failure modes.

Method 6 sends the full raw reference text to the LLM in a single call
and returns structured entries in the same format as Method 5 (footnote
extraction). Results flow through the existing rich-entry integration
path in graph.py — entries already parsed correctly by GROBID are caught
by deduplication; genuine gaps land as new entries.

get_raw_references_text() added to tei_parser.py to extract the raw div
text, with a warning log when the div is present but empty (5 papers in
the F1 corpus where GROBID extraction failed entirely). _LLM_BIB_REPARSE
prompt and _method_llm_bib_reparse() added to detector.py. Uses
max_tokens=4096 and a 300s minimum timeout given the large input size.

### 2026-06-10 — Skip non-reconstructible GROBID entries at ingestion

Added _is_reconstructible() to graph.py. After normalize_entry() runs,
each GROBID-derived bibliographic entry is checked against minimum field
requirements for its entry type before being added to the bibliography.
The check is framed as: can a minimal Chicago author-date citation be
assembled from the parsed fields?

Requirements by type:
- article, incollection, inproceedings, thesis: author + year + title
- book: (author or editor) + year + title
- misc: year + (author or title)

An additional check catches page-break fragments regardless of parsed
fields: a raw citation starting with a lowercase character that is not
a known particle (von, van, de, etc.) is a mid-word continuation and
is unconditionally skipped.

The check is conditional on llm_config being present — when no LLM is
configured, all GROBID entries are kept as the best available data.
Method 6 recovers any legitimate references lost to GROBID parsing
failures when an LLM is configured.

Applied in both the seed paper and F1 paper GROBID ref integration
loops in graph.py.

### 2026-06-10 — Fix llm_cfg reassignment discarding CLI overrides (todo V)

llm_cfg was assigned from config.get("llm", {}) twice in main(): once at
line 162 before applying --remote, --model, and --no-think overrides, and
again at line 196 which silently discarded all of those overrides before
they reached any pipeline stage. Removed the duplicate assignment at line
196. The overrides now persist correctly into all subsequent stages.

No other missing imports found in conditional blocks. Lazy imports of
bibvik modules are intentional; stdlib lazy imports (socket, threading,
time) are safe anywhere.

### 2026-06-10 — Update methods docs for Method 6 and GROBID entry filtering

citation-detection.md: updated to reflect six detection methods. Added Method
6 (LLM bibliography re-parse from raw text) as a full section covering
implementation, scope, token budget, and known limitations. Updated design
rationale to include page-break fragmentation as a motivating failure mode.
Added GROBID entry filtering section documenting _is_reconstructible(). Updated
Known Limitations to cover empty references div and LLM dependency for
Methods 4-6.

deduplication-normalisation.md: added GROBID entry filtering section
documenting _is_reconstructible() — per-entry-type field requirements, mid-word
start signal for page-break fragments, and conditional behaviour based on LLM
availability.

llm-prompts.md: added Method 6 prompt (_LLM_BIB_REPARSE) and design rationale
as a new section.

### 2026-06-10 — Pipeline refinement and bibliography quality analysis

Method 6 (LLM bibliography re-parse from raw TEI text) implemented in
detector.py and tei_parser.py. get_raw_references_text() extracts the
raw reference div text; _method_llm_bib_reparse() sends it to the LLM
in one call and returns structured entries via the existing rich-entry
integration path. Warning logged for papers with empty references div.

_is_reconstructible() added to graph.py. Checks normalized GROBID entries
against minimum field requirements by entry type before adding to the
bibliography. Additional signals: mid-word raw citation start (page-break
fragment), catalogue/findspot entries where year appears only in a
parenthetical cross-reference (_CATALOGUE_PARENS_RE), and shorthand
back-references of the form "Author Year" (_SHORTHAND_RE). Conditional
on LLM being configured. Applied in seed and F1 integration loops.
NOTE: _CATALOGUE_PARENS_RE needs corpus validation before next rerun
(todo AF — Scandinavian place names may produce false positives).

Year validation added to normalize_entry() in normalize.py. Extracted
years must fall within 1450–2030. Years outside this range are cleared.

generate_citekey() in utils.py fixed to use two-letter suffixes (aa, ab,
...) after exhausting single-letter suffixes a–z, preventing overflow
into non-ASCII Unicode characters.

Title recovery pass added to postprocess.py as Pass 2. For entries with
_raw_citation but no title, sends raw string to LLM asking only for the
title. Runs before near-duplicate resolution. run.py updated to pass
llm_config to run_postprocess().

llm_cfg reassignment bug fixed in run.py — duplicate assignment was
discarding --remote/--model/--no-think CLI overrides.

Full corpus rerun completed (379/382 papers). Bibliography reduced from
22,901 to 16,578 entries. Reduction fully accounted for by systematic
comparison against May 2026 bibliography. 1,167 new entries added,
403 from Method 6 recovery. Analysis documented in
bibliography-comparison-analysis.qmd and bibliography-comparison-summary.qmd.

Outstanding: todo AF (catalogue signal validation), todo AE (20 empty
references div papers + 3 GROBID failures), todo AC (compound splitting
rewrite), todo AD (rerun after current fixes).

### 2026-06-10 — Add footnote stub resolution pass to postprocess.py

New Pass 2b in postprocess.py: resolve_footnote_stubs(). Targets entries
produced by Method 5 (LLM footnote extraction) that have author and year
but no title — 112 such entries in the June 2026 bibliography.

Three resolution mechanisms applied in order:

1. Abbreviation expansion: entries whose author field is a known series
   abbreviation (AUD → Arkæologiske Udgravninger i Danmark, etc.) have
   their title set from a hardcoded lookup table (_ABBREVIATION_TABLE).

2. OCR/normalisation merges: 10 confirmed OCR or normalisation corruptions
   of existing titled entries (_OCR_MERGE_PAIRS) are merged into their
   targets. cited_by lists are combined; source is marked _merged_into.
   Pairs were verified manually before inclusion.

3. CrossRef author+year query: for remaining stubs, queries CrossRef by
   author name and year filter. Accepts only if returned year matches
   exactly and author name similarity ≥ 0.70. Sets
   _title_from_crossref_author_year: True for provenance.

run_postprocess() signature updated to accept email parameter.
run.py updated accordingly.

### 2026-06-10 — Improve near-duplicate detection and add stub resolution pass

Pass 2b added to postprocess.py: resolve_footnote_stubs(). Targets entries
with author+year but no title from Methods 5 and 6. Three mechanisms:
abbreviation expansion (_ABBREVIATION_TABLE), OCR/normalisation merges
(_OCR_MERGE_PAIRS, 10 manually verified pairs), CrossRef author+year query
(accepts if year matches exactly and author similarity ≥ 0.70).
run_postprocess() updated to accept email parameter for CrossRef access.

_first_author_key() now uses last word of compound family name so
"Hallans Stenholm" pairs correctly with "Stenholm" in near-duplicate
detection index.

_title_tokens() now normalises through unidecode before tokenising,
catching OCR Unicode variants in title comparison.

flag_near_duplicates() token overlap gate removed when LLM is configured.
All same-author same-year title pairs with substantive titles now go
directly to the LLM for judgment. Token overlap retained as fallback
when no LLM is available. Resolves cases where titles are semantically
equivalent but lexically different — different language phrasings, OCR
variants, compound surname indexing failures.

Postprocess run on June 10 output: 68 stubs resolved by Pass 2b, 3 titles
recovered by LLM title recovery, 13 near-duplicate merges.

### 2026-06-10 — Fix editor-based citekeys, Cyrillic normalisation, and systematic merge corrections

generate_citekey() in utils.py now accepts an optional editors parameter
and falls back to first editor surname when no author is present. Edited
volumes previously generated NOAUTHOR citekeys despite having editor names
clearly in the raw citation (e.g. NOAUTHOR1 → ahola2014). All seven
generate_citekey() call sites in graph.py updated to pass editors from
the entry's editor field.

norm_author() in utils.py now applies domovyk ALA-LC transliteration for
Cyrillic names before unidecode. Previously unidecode collapsed Cyrillic
to empty strings, causing 970 of 975 Cyrillic-authored entries to have
empty normalised keys, making them invisible to all author-key-based
deduplication. Fix ensures Непомнящий → nepomniashchii, Коваленко →
kovalenko etc., enabling proper deduplication of Cyrillic-authored entries
against each other and against Latin transliterations.

Todo AH updated: design a structured corrections file system for drop-in
OCR/normalisation merge pairs (e.g. widerstrom2004/norderang2004) that
postprocess.py reads at runtime, avoiding ad-hoc code changes for each
new verified pair.

Both fixes apply on next full rerun.

### 2026-06-10 — Add pdftoppm+Tesseract alternate OCR fallback to grobid_client.py

Extends the existing ocrmypdf OCR fallback with a second mechanism that
bypasses the PDF text layer entirely by rendering pages to images and running
Tesseract OCR from pixels. This handles two failure modes that ocrmypdf cannot:

[BAD_INPUT_DATA]: GROBID's PDF parser crashes (exit code 134) before any text
extraction. Triggered by Paterson et al 2014. pdftoppm can render the PDF to
images even when GROBID's parser fails.

Font encoding failure: GROBID successfully extracts text but the PDF uses a
custom font with no standard Unicode mapping, producing private-use Unicode
characters (U+E000–U+F8FF) instead of readable text. Triggered by Feveile 2012.
Detected by _has_private_use_unicode() which checks whether private-use
characters exceed 5% of a sample of the extracted TEI.

_run_pdftoppm_tesseract(): renders at 300 DPI using pdftoppm, OCRs with
Tesseract using a multi-language pack (nor+swe+dan+deu+eng+fra+pol+ukr),
merges page PDFs with pdfunite or gs. Output cached in output/ocr/ as
<stem>.pdftoppm_ocr.pdf so subsequent reruns skip the OCR step.

process_fulltext() extended with two new fallback branches in addition to
the existing [NO_BLOCKS]/ocrmypdf path. _submit_to_grobid() now sets
_last_bad_input flag when [BAD_INPUT_DATA] is detected.

AE investigation findings documented separately. The 20 empty-div papers
were categorized: 2 genuinely sparse intro chapters, 9 well-covered by
Methods 2-5, 2 with bibliography in body text (handled by Method 6 body-tail
fallback), 1 journal special section, 1 incomplete PDF, 4 needing alternate
OCR (Gardeła 2014, Moen 2020, Paterson et al 2014, Feveile 2012).

### 2026-06-17 — Fix ghost entries, F1 cited_by bug, and begin NOAUTHOR resolution

_is_reconstructible() made unconditional in all three GROBID integration loops
(seed paper, F1, F2) in graph.py. Previously gated on llm_config — GROBID
entries with journal+volume but no author/title/year were entering the
bibliography when llm_config was not available at processing time. Ghost
entries (5 identified: NOAUTHOR525, NOAUTHOR752, NOAUTHOR822, NOAUTHOR1236,
NOAUTHOR1328) will be suppressed on next rerun.

F1 paper entries now correctly set cited_by: [self.seed_citekey]. Previously
set to [] — 16 F1 entries had empty citation relationships despite being
cited by the seed paper by definition.

AE investigation complete. 20 empty-references-div papers categorized and
documented in citation-detection.md. grobid_client.py extended with
pdftoppm+Tesseract alternate OCR fallback for [BAD_INPUT_DATA] and
font encoding failures.

NOAUTHOR investigation ongoing — 26 remaining entries categorized:
5 ghosts (suppressed next rerun), 7 dash-abbreviated (fix pending),
6 author-in-raw (fix pending), 3 legitimate (keep), 5 ambiguous (pending).

### 2026-06-17 -- Suppress dash-prefix entries and add LLM author recovery for NOAUTHOR entries

_DASH_PREFIX_RE added to graph.py. Raw citations starting with "- " or "--"
followed by a year are Scandinavian scholarly bibliography conventions where
the dash indicates the same author as the preceding entry. GROBID parses
these as standalone entries with no author. They passed _is_reconstructible()
because they had year+title satisfying misc requirements. Now suppressed.
Affects 7 entries in current bibliography (NOAUTHOR468, 486, 488, 489, 516,
637, 638) -- will be suppressed on next rerun.

recover_authors_from_raw() added to postprocess.py as Pass 3. Targets NOAUTHOR
entries with _raw_citation and title but no author. Sends raw string to LLM
to extract structured author data. Sets _author_recovered: True for provenance.
_LLM_AUTHOR_RECOVERY prompt defined as module-level string constant.
Targets 6 entries in current bibliography (NOAUTHOR131, 1267, 1268, 989,
83, 88).

Remaining NOAUTHOR entries after fixes take effect:

- 3 legitimate (NOAUTHOR1247 AdapterRemoval, NOAUTHOR635 Schreiner Collection,
  NOAUTHOR157 FARMPACT) -- keep as-is
- 5 ambiguous (NOAUTHOR1262, NOAUTHOR771, NOAUTHOR779, NOAUTHOR819, NOAUTHOR528)
  -- pending investigation

### 2026-06-19 — Corrections system

Added `bibvik/corrections.py`. The bibliography contains two classes of
errors automated processing cannot resolve: systematic failures found
through pipeline investigation (OCR corruption, same-work collisions),
and errors identified by human reviewers through the audit. Both are
handled through the same mechanism.

`corrections.yaml` in the project root holds confirmed corrections
(merge, delete, set) applied as pass 0 of `--postprocess`.
`corrections_draft.yaml` is generated by `--postprocess` from
pipeline-flagged issues for researcher review before promotion to
`corrections.yaml`.

`_OCR_MERGE_PAIRS` removed from `postprocess.py`; entries moved to
`corrections.yaml`.

### 2026-06-19 — Audit corrections system implementation

`corrections.py`: note field now required on all actions (entries skipped
if missing); `load_yaml`/`save_yaml` exposed as public functions.

`corrections_draft.yaml` removed. Draft candidates are now appended
directly to `corrections.yaml` with `_draft: true` for in-place review.

`postprocess.py`: corrected to look up `corrections.yaml` at project root
rather than `output/`.

`exporter.py`: tombstoned entries filtered before export.

### 2026-06-22 — Corrections system

Added `bibvik/corrections.py`. `corrections.yaml` in the project root is
the single file for all manual bibliography curation — both systematic
failures found through pipeline investigation and decisions from human
review. Confirmed corrections (merge, delete, set) are applied as pass 0
of `--postprocess` before LLM passes; notes required on all confirmed
entries. Pipeline-generated draft candidates are appended with `_draft: true`
after each run for in-place review.

`postprocess.py`: corrections applied as pass 0; drafts appended after all
passes. `graph.py`: tombstoned entries filtered from `get_bibliography()`.
`_OCR_MERGE_PAIRS` removed from `postprocess.py`; entries moved to
`corrections.yaml`.

### 2026-06-22 — Wire draft correction sources

`postprocess.py`: `recover_authors_from_raw()` added as Pass 3. For
NOAUTHOR entries with a raw citation string and title but no parsed
author, the LLM attempts to extract structured author data from the raw
string. Sets `_author_recovery_failed` on failure, which triggers a
draft `set` correction in `corrections.yaml`.

`grobid_client.py`: `last_ocr_degraded` flag added. Set when the
private-use Unicode fallback runs but the TEI remains garbled after
pdftoppm+Tesseract.

`graph.py`: entries from papers where `last_ocr_degraded` is set are
marked `_ocr_candidate`. These surface as draft `delete` corrections
in `corrections.yaml` after `--postprocess`.

### 2026-06-22 — Resolve widerstrom2004/norderang2004 same-work collision

Norderäng & Widerström 2004 ("Vikingatida bildstenar," Gotländskt Arkiv
76:82-89) was cited with different author order in different source papers,
causing deduplication to miss the match and produce two citekeys.
`norderang2004` merged into `widerstrom2004` via `corrections.yaml`.

### 2026-06-22 — Remove inline compound splitting

`_split_compound_entry()` and the `_possibly_compound` check removed
from `graph.py`. Compound bibliography entries are already handled by
Method 6 (LLM re-parse of raw reference div text), which operates on
continuous text before GROBID's page-break fragmentation occurs. The
inline approach was dead code — `_possibly_compound` was never set by
any part of the pipeline.

### 2026-06-22 — Rewrite audit as diagnostic report

audit.py rewritten to produce a self-contained HTML report
(audit_report.html) rather than an annotatable Markdown sample. The
audit is reoriented as a diagnostic instrument: it enables a researcher
to judge whether the bibliography is trustworthy enough to analyse,
rather than producing a list of things to fix. Corrective actions are
recorded in corrections.yaml.

The report is organised around five quality dimensions drawn from Wang &
Strong (1996): completeness (missing-field rates by detection method,
with a sample of no-title entries), accuracy (sample from non-English
source papers; sample of CrossRef-resolved entries), representational
consistency (suspected duplicate pairs), coverage (bottom 20 papers by
citation count), and provenance (detection method × generation
breakdown).

Corrective strata removed from the audit: near-duplicate candidates,
GROBID ID as author, oversized titles, missing given names, citekey
collisions. These are now surfaced automatically as draft corrections
in corrections.yaml.

docs/methods/audit-sampling.md updated to reflect the reorientation.

### 2026-06-22 — Update Zotero CSV

Replaced Exported_Items.csv with BibVik_seed.csv, a fresh export from
Zotero. Format identical; PDF attachment paths updated. Zotero CSV
files excluded via .gitignore. config.yaml zotero_csv path updated
accordingly.

### 2026-06-22 — Add confirmed corrections

Three merge corrections added to corrections.yaml after verification
against the bibliography. rsnes1966 merged into orsnes1966 (OCR dropped
leading O from Orsnes). wamets1985 merged into wamers1985 (OCR misread
Wamers as Wamets). widerstrom2004 merged into norderang2004 — same work
cited with different author order in different source papers.

### 2026-06-22 — Confirm Unpaywall working

Unpaywall API confirmed working against live DOIs. Stale warning
removed from coverage.py docstring and report output.

### 2026-06-22 — Docs update

corrections-system.md rewritten to reflect single-file approach:
corrections_draft.yaml removed; confirmed corrections and draft candidates
both live in corrections.yaml, drafts marked _draft: true. Known
corrections table added (rsnes1966→orsnes1966, wamets1985→wamers1985,
norderang2004→widerstrom2004). _OCR_MERGE_PAIRS migration note removed.

deduplication-normalisation.md: _is_reconstructible() described as
unconditional; stale LLM-gated language corrected.

llm-prompts.md: OCR merge pairs section updated to reference
corrections.yaml; unverified entries removed.

cluster-deployment.md: audit output filename and Zotero CSV path updated.

### 2026-06-22 — Docs consolidation and update

architecture.qmd merged with project_context.qmd. Project overview,
corpus description, technical environment, and cluster details moved
into architecture.qmd. Method 6, both OCR fallback paths, corrections
system, and updated postprocess pass list added. Module map and CLI
flags updated. project_context.qmd deleted.

corrections-system.md rewritten for single-file approach.
corrections_draft.yaml references removed; known corrections table added.

deduplication-normalisation.md: _is_reconstructible() correctly described
as unconditional.

llm-prompts.md: OCR merge pairs reference updated to corrections.yaml.

cluster-deployment.md: audit filename and Zotero CSV path updated.

### 2026-06-22 — Fix audit crash

`_sample()` helper missing from `audit.py` — called in `run_audit()`
but not defined. Added.

### 2026-06-22 — Fix run_postprocess call in run.py

`run_postprocess()` was called with `email=email` which was never a
valid parameter. Fixed to pass `project_root=Path(__file__).parent`
instead, so `corrections.yaml` is found at the project root. The fix
was made earlier in the session but the unfixed version of run.py
was committed.

### 2026-06-22 — Fix run.py stage order and postprocess call

run_postprocess() was called with email= which is not a valid
parameter; replaced with project_root=Path(__file__).parent.
Audit stage was ordered before postprocess; swapped so
corrections and LLM passes run before the audit reads the
bibliography.

### 2026-06-22 — Add title and author recovery passes to postprocess.py

recover_titles_from_raw() added as Pass 4. For entries with a raw
citation string but no title, sends the raw string to the LLM to
extract the title. Targets ~310 no-title entries that have raw
citations.

recover_authors_from_raw() added as Pass 3. For NOAUTHOR entries
with a raw citation and title but no author, sends the raw string
to the LLM to extract structured author data. Sets
_author_recovery_failed on failure for draft corrections.

### 2026-06-25 — Fix garbage entry detection

_is_reconstructible() in graph.py extended with two new suppression
checks. Page-reference shorthand: raw citations under 60 characters
containing page reference patterns (s. \d+, pp. \d+, :\d+-\d+) are
suppressed — these are cross-references not standalone entries.
Editorial shorthand: raw citations starting with Siehe, Vgl., Cf.,
Nach, ibid and similar are suppressed. _DASH_PREFIX_RE fixed to
match dash-year without space.