# GROBID Configuration

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Overview

BibVik uses GROBID 0.8.1 (GeneRation Of BIbliographic Data) for PDF parsing
and reference extraction. GROBID applies Conditional Random Field and
transformer models to extract structured bibliographic data from scholarly
PDFs, returning TEI-XML. This document describes the configuration choices
made, the rationale behind them, and known issues.

GROBID is deployed via Docker:

```
docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1
```

On Apple Silicon Macs, GROBID runs under AMD64 emulation. This is slower than
native and may contribute to occasional container instability on long runs.

## API endpoint

BibVik uses GROBID's `/api/processFulltextDocument` endpoint rather than
`/api/processReferences`. The fulltext endpoint returns both:

- `<body>`: Parsed body text with inline `<ref type="bibr">` citation markers
- `<listBibl>`: Structured bibliography entries

This dual output is essential for linking inline citations to bibliography
entries and extracting citation contexts. The references-only endpoint
(`/api/processReferences`) is used only as a fallback when fulltext processing
fails.

## Request parameters

### `consolidateHeader: 1`

GROBID is instructed to consolidate the paper's header metadata against
CrossRef. This enriches the extracted title, authors, DOI, and publication
date for the paper itself (not its references). Enabled because header
metadata quality directly affects citekey generation and paper matching.

### `consolidateCitations: 0`

GROBID's citation consolidation (`consolidateCitations: 1`) causes a CrossRef
API call for every reference in the bibliography. For a paper with 200
references, this means 200 sequential API calls during the GROBID request,
which regularly exceeds the timeout and causes the response to be truncated —
fewer bibliography entries are returned than the paper actually contains.

Citation consolidation is therefore disabled. Reference metadata enrichment is
handled separately via BibVik's own resolver (`--resolve`) which uses CrossRef
with the title/context plausibility check described in
`docs/methods/resolver-method.md`.

### `includeRawCitations: 1`

The raw (unparsed) citation string from the reference list is included in each
`<biblStruct>` entry. This is used as a fallback when structured parsing fails
and as a source for the compound reference splitter. It also serves as the
ground truth for manual audit review — a structured entry can be checked
against its raw string to detect parsing errors.

### `teiCoordinates`: not requested

Bounding box coordinates for text elements are not requested. They would enable
page-number extraction but add significant processing overhead and are not
needed for text extraction or citation detection. See `docs/methods/data-capture.md`
for the decision on page numbers.

## OCR fallback

Some PDFs in the corpus have no embedded text layer (scanned images). GROBID
returns HTTP 500 with `[NO_BLOCKS]` in the response body for these rather than
the expected TEI-XML. BibVik detects this signal and automatically runs
`ocrmypdf` to add a text layer before retrying GROBID.

The OCR'd version replaces the original at its path (the original is backed up
to `output/ocr/originals/`). Since Zotero uses linked files rather than
attached files, the replacement is transparent to Zotero. On subsequent runs,
presence of the backup file signals that OCR has already been applied.

See `docs/methods/data-capture.md` for details on the OCR process, and the
decision log entry for the implementation decisions.

## TEI parsing

GROBID's TEI-XML output is parsed by `tei_parser.py`. Several post-processing
steps are applied:

**Compound reference splitting:** GROBID collapses multiple references by the
same author into a single `<biblStruct>` when the reference list uses the
humanities dash convention (`—1987. Title Two.`). The splitter detects
dash-year patterns and author-boundary merges and splits these into individual
entries.

**`{{CITE:}}` placeholder cleanup:** GROBID sometimes produces `<ref>` elements
with empty `target` attributes, which would create placeholder tokens with no
ID. These are handled by using the original marker text directly rather than
a broken placeholder.

**Section heading extraction:** The `<div>`/`<head>` hierarchy is walked to
assign each paragraph its ancestor section heading as a breadcrumb string.
See `docs/methods/data-capture.md`.

## Known issues

**Container instability on long runs:** The GROBID Docker container has been
observed to crash during multi-hour processing runs, particularly on ARM Macs
under emulation. The pipeline handles this via the `ConnectionError` exception
path in `grobid_client.py`, which logs a clear error message and skips the
paper rather than crashing. Monitoring and restart logic for the container is
not yet implemented.

**ARM emulation:** Running GROBID under AMD64 emulation on Apple Silicon
(M-series) Macs is slower than native and may be a contributing factor to
container instability. On the GPU cluster, GROBID would run natively.

**GROBID's published benchmarks do not transfer to this corpus:** GROBID's
published F1 scores (~0.87 for reference extraction, 0.76–0.91 for citation
context resolution) were measured against PubMed Central — standardised
biomedical English literature. The BibVik corpus is multilingual, includes
diverse publication types, and contains older scanned material. Independent
manual validation via the audit tool is therefore necessary. See
`docs/methods/audit-sampling-method.md`.

## Version

GROBID 0.8.1 (`lfoppiano/grobid:0.8.1`). The version is pinned in the Docker
image tag. Different GROBID versions may produce different TEI output structure;
the TEI parser in `tei_parser.py` was written and tested against 0.8.1.
