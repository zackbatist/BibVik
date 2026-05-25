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