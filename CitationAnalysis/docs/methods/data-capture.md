# Data Capture

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Overview

The BibVik pipeline captures two categories of structured data during processing:
the **citation graph** (bibliography entries with provenance and relationships),
and **paper-level data** (processed paper metadata, body text, and detection
results). This document describes all captured fields, their sources, storage
locations, and the rationale behind each.

---

## Citation graph

Stored in `output/bibliography.json` and `output/_graph_state.json` under the
`bibliography` key. Each entry is a dict keyed by citekey.

### Bibliographic metadata

Extracted by GROBID from the reference list of the citing paper, then
optionally enriched via CrossRef or LLM resolution. Fields follow biblatex
conventions.

| Field | Source | Notes |
|---|---|---|
| `citekey` | Generated | `lastnameyear` with `a/b/c` disambiguation; `unidecode` transliteration for non-Latin names |
| `entry_type` | GROBID / CrossRef | `article`, `book`, `incollection`, `inproceedings`, `misc` |
| `author` | GROBID / CrossRef | List of `{family, given}` dicts |
| `title` | GROBID / CrossRef | Normalised from ALL-CAPS where detected |
| `date` | GROBID / CrossRef | Publication year |
| `journaltitle` | GROBID / CrossRef | For articles |
| `booktitle` | GROBID / CrossRef | For book chapters |
| `volume`, `number`, `pages` | GROBID / CrossRef | |
| `publisher`, `location` | GROBID / CrossRef | |
| `doi` | GROBID / CrossRef | Cleaned of URL prefixes and trailing punctuation |
| `editor` | GROBID | For edited volumes |
| `series`, `eventtitle` | GROBID | |

### Graph provenance fields

Added by the pipeline during graph construction.

| Field | Source | Notes |
|---|---|---|
| `generation` | Pipeline | `P` (seed), `F1`, `F2` — distance from seed paper |
| `cited_by` | Pipeline | List of citekeys of papers that cite this entry |
| `_source_pdf` | Pipeline | Filename of the PDF from which this entry was extracted |
| `_grobid_id` | GROBID | Internal reference ID used for citation linking within a paper |
| `_raw_citation` | GROBID | Unparsed citation string from the reference list |
| `_resolution_method` | Resolver | `crossref`, `llm_from_context`, `llm_from_footnote`, `stub` |
| `_resolution_confidence` | Resolver | `high`, `medium`, `low` — see `docs/resolver-method.md` |

### Completeness scoring

Each bibliography entry receives a `completeness` field computed by
`biblatex_model.py`. The score (0.0–1.0) reflects how many expected fields
are present for the entry's type. Required and recommended fields vary by type
(e.g. an article requires `journaltitle`; a book requires `publisher`). Labels:
`complete` (all required and most recommended fields present), `partial` (all
required, some recommended missing), `minimal` (some required fields missing).

The completeness score is used by the audit tool to stratify entries for manual
review — minimal entries are the most likely to contain extraction errors.

---

## Paper-level data

Stored in `output/_graph_state.json` under the `processed_papers` key,
keyed by PDF filename. Serialised and reloaded between pipeline stages.

### Header metadata

Extracted from the TEI header by `parse_tei_header()` in `tei_parser.py`.
Stored under `processed_papers[pdf_name]['header']`.

| Field | Source | Notes |
|---|---|---|
| `title` | GROBID | Paper title |
| `author` | GROBID | List of `{family, given}` dicts |
| `author[n].affiliation` | GROBID | See below |
| `date` | GROBID | Publication date |
| `doi` | GROBID | |
| `abstract` | GROBID | Often absent for non-journal publications |

**Author affiliations:** Extracted from `<affiliation>` children of `<author>`
elements in the TEI header. Each affiliation dict may contain: `institution`,
`department`, `settlement`, `region`, `country`, `postCode`. Quality is
inconsistent — GROBID sometimes places the author name in the institution
field, and affiliations are absent for many corpus items (conference
proceedings, edited volume chapters, older journals). Data stored as-is;
reconciliation against a controlled vocabulary (ROR — Research Organization
Registry) is deferred. Affiliations are properties of the citing paper's
authors and are not attached to bibliography entries.

### Body text and citation locations

Extracted from the TEI body by `parse_tei_body()` in `tei_parser.py`.
Stored as a list under `processed_papers[pdf_name]['paragraphs']`.
Each paragraph dict contains:

| Field | Source | Notes |
|---|---|---|
| `text` | GROBID | Full paragraph text with `{{CITE:id}}` placeholders replacing inline citation markers |
| `paragraph_index` | Pipeline | 1-based sequential index within the paper body. Assigned by the parser; GROBID does not number paragraphs. Provides a stable location reference independent of PDF rendering |
| `section_heading` | GROBID / Pipeline | Breadcrumb of ancestor `<div>/<head>` elements, e.g. `"Results > Typological Analysis"`. Empty string for paragraphs before the first heading |
| `citations` | GROBID | List of `{grobid_id, marker_text, char_offset}` dicts — inline citations detected by GROBID in this paragraph |

**Page numbers considered and not adopted:** GROBID does not produce `<pb>`
(page break) elements in standard processing mode — confirmed by TEI inspection
across the sample. Enabling page coordinates via `teiCoordinates` would add
processing overhead and complicate the request. Page numbers are also unreliable
across corpus variants (published PDFs, preprints, scans). Paragraph index
provides equivalent positional information in a more stable form.

### Footnotes

Extracted by `parse_tei_footnotes()` in `tei_parser.py` from `<note place="foot">`
elements. Not stored in `processed_papers` directly; passed to the LLM footnote
extraction method (Method 5) during detection. The footnote extraction method
(`--footnotes` flag) produces `output/footnote_references.json` as a separate
output.

### Detection counts

Stored under `processed_papers[pdf_name]['detection']`. Records how many
citations were found by each of the five detection methods and the merged total.

| Field | Notes |
|---|---|
| `reference_list` | Citations from GROBID bibliography extraction (Method 1) |
| `inline_markers` | Citations from GROBID inline `<ref>` markers (Method 2) |
| `text_patterns` | Citations from regex pattern matching (Method 3) |
| `llm_body_scan` | Citations from LLM paragraph scan (Method 4) |
| `llm_footnotes` | Citations from LLM footnote extraction (Method 5) |
| `merged_total` | Unique citations after deduplication across all methods |

### Bibliography references

Stored under `processed_papers[pdf_name]['references']`. The list of structured
reference dicts as parsed from the GROBID TEI bibliography (before deduplication
and graph merging). Preserved for debugging and re-processing without re-running
GROBID.

### Language

Stored under `processed_papers[pdf_name]['language']`. ISO 639-1 code
(e.g. `"en"`, `"no"`, `"da"`, `"sv"`, `"de"`, `"fr"`) or `"unknown"`.

Detected using the `lingua` library (`lingua-language-detector` on PyPI)
applied to the first ~2000 characters of body text. The detector is restricted
to the six languages expected in the corpus for accuracy and speed.

**Why `lingua` over `langdetect`:** `langdetect` (Nakatani Shuyo, 2010; Python
port by Mimino, 2014) uses character n-gram Naive Bayes trained on Wikipedia
abstracts. It is non-deterministic by default (requires `DetectorFactory.seed = 0`
for reproducibility) and performs poorly on closely related languages —
particularly Norwegian and Danish. `lingua` (Pemistahl, 2019–) uses n-grams of
lengths 1–5 with rule-based pre-filtering, is deterministic by design, and
substantially outperforms `langdetect` on short texts and related languages.
Neither library has a peer-reviewed paper; both provide documented benchmarks.

GROBID's own `xml:lang` attribute on the `<text>` element is not used.
Inspection found it unreliable — non-English papers were tagged `en` when
abstracts or keywords were in English.

Detection runs once per paper and the result is stored in the graph state;
subsequent runs reuse the stored value. The library version and language
configuration are fixed and documented here for reproducibility.

**Relationship to normalisation:** Language detection and text normalisation are
separate concerns. The `language` field records what language a paper is in. How
non-English text is handled for context analysis (translation, language-specific
prompting, or exclusion) is a downstream decision.

### GROBID ID to citekey map

Stored under `processed_papers[pdf_name]['grobid_id_to_citekey']`. Maps GROBID's
internal reference IDs (e.g. `"b42"`) to the citekeys assigned during graph
construction. Used by `context_extractor.py` to resolve `{{CITE:b42}}`
placeholders back to citekeys when extracting citation contexts.

---

## What is not captured

**Full TEI-XML:** The raw TEI-XML returned by GROBID is written to
`output/tei/<filename>.tei.xml` for reference but is not stored in
`processed_papers` or the graph state. It is available on disk for re-parsing
without re-running GROBID.

**Citation contexts:** Verbatim text surrounding each citation in the body
(the sentences before and after each `{{CITE:id}}` placeholder) is not captured
during graph generation. It is extracted in a separate stage (`--contexts`)
by `context_extractor.py`, which reads from the stored paragraphs. This
separation keeps the graph generation stage focused and the graph state smaller.

**Figure and table captions:** GROBID extracts these in the TEI body but they
are not currently included in the paragraph list. They may contain citations
but are a small proportion of the total.

**Coordinate data:** GROBID can return bounding box coordinates for text
elements if requested via `teiCoordinates`. This is not enabled — coordinates
are not needed for text extraction and would add overhead.
