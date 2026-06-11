# LLM Prompts

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

BibVik uses a local large language model (LLM) for three tasks: body scan
citation detection, footnote reference extraction, and resolution of unmatched
citations. This document records the exact prompts used for each task and the
design rationale behind them. Because prompts are part of the methodology, they
are defined as readable string constants in the source code so they can be
inspected and cited directly.

The prompts are in `bibvik/detector.py` (`_LLM_BODY_DETECT`,
`_LLM_BODY_DETECT_BATCH`, `_LLM_FOOTNOTE_EXTRACT`) and `bibvik/resolver.py`
(`_LLM_RESOLVE_PROMPT`).

The model used for all tasks is `qwen2.5:7b` running locally via Ollama. The
`/no_think` directive at the end of each prompt suppresses extended reasoning
output when the model supports it.

---

## Method 4: Body scan (single paragraph)

**Task:** Identify all works cited or referenced in a single paragraph of body
text, returning a flat list of (first author family name, year) pairs.

**Prompt** (`_LLM_BODY_DETECT`):

```
You are an expert at identifying bibliographic references in academic text. Find ALL works cited or referenced in this passage.

## Passage
---
{text}
---

Include: formal citations (Smith 2020), narrative (Smith (2020) argued...), discursive ("as Smith argued in her 2020 monograph"), organizational authors (UNESCO 2019), non-English styles.

For each work: {"first_author": "<family name>", "year": "<4 digits>"}
Respond ONLY with a JSON array. If none: []
/no_think
```

**Design rationale:** The prompt explicitly lists the citation styles that
formal methods miss — narrative, discursive, organisational, and non-English
— because these are the primary target of LLM detection in this corpus. The
instruction to respond with a JSON array of author+year pairs (rather than full
references) reflects the pipeline's architecture: the LLM is used only for
detection, not for metadata extraction. Metadata for detected citations is
resolved separately via the GROBID bibliography or the resolver.

**Batching variant** (`_LLM_BODY_DETECT_BATCH`): When multiple paragraphs are
sent in a single request (batch size configured in `config.yaml`), the prompt
wraps each paragraph with a numbered header and instructs the model to return a
single flat array of all citations found across all paragraphs.

---

## Method 5: Footnote extraction

**Task:** Extract structured bibliographic metadata from a single footnote,
which may contain one or more complete bibliographic references embedded in
prose.

**Prompt** (`_LLM_FOOTNOTE_EXTRACT`):

```
You are an expert at extracting bibliographic references from academic footnotes. This footnote may contain one or more references to published works embedded in prose.

## Footnote text
---
{text}
---

For EACH distinct published work referenced, extract as much metadata as you can:
- first_author_family: family/surname of the first author
- first_author_given: given name(s) or initials of the first author
- additional_authors: list of {"family": "...", "given": "..."} for co-authors (empty list if sole author)
- year: publication year (4 digits)
- title: title of the article, chapter, or book
- container_title: journal name, book title (for chapters), or series name (empty string if standalone book)
- volume: volume number (empty string if n/a)
- pages: page range (empty string if n/a)
- doi: DOI if mentioned (empty string if not)
- entry_type: one of "article", "book", "incollection", "inproceedings", "thesis", "misc"

Respond ONLY with a JSON array. If no references: []
Example: [{"first_author_family": "Sindbæk", "first_author_given": "Søren M.", "additional_authors": [], "year": "2007", "title": "The Small World of the Vikings", "container_title": "Norwegian Archaeological Review", "volume": "40", "pages": "59-74", "doi": "", "entry_type": "article"}]
/no_think
```

**Design rationale:** Footnote extraction differs from body scan in two
important ways. First, footnotes in Chicago-style papers often contain complete
bibliographic references rather than brief citations, so the LLM can extract
full metadata rather than just author+year. Second, a single footnote may
contain multiple references — the prompt explicitly asks for all distinct works
and returns an array. The structured output schema mirrors the BibLaTeX model
used throughout the pipeline, minimising the need for post-processing.

The example in the prompt uses a real Viking Age studies reference to prime the
model for the domain-specific name forms (diacritics, Scandinavian surnames)
common in this corpus.

---

## Resolution: inferring metadata from citation contexts

**Task:** Given a detected citation (author name, year) that could not be
matched to any existing bibliography entry, infer the full bibliographic
metadata from the surrounding text contexts in which the work is cited.

**Prompt** (`_LLM_RESOLVE_PROMPT`):

```
You are an expert in academic bibliography. Based on the following citation contexts, infer the full bibliographic metadata for a work by {author} published in {year}.

## Citation contexts where this work is referenced
{contexts}

## Task
Based on these contexts, infer as much bibliographic metadata as you can for this specific work. If you can identify the title, journal/book, and other details from the context, include them. If not, provide what you can.

Respond ONLY with a JSON object:
{"first_author_family": "...", "first_author_given": "...", "additional_authors": [], "year": "...", "title": "...", "container_title": "...", "volume": "", "pages": "", "entry_type": "article|book|incollection|misc"}
/no_think
```

**Design rationale:** The resolver receives up to three citation contexts — the
paragraphs in which the work is cited — and uses them to infer metadata the
pipeline could not obtain from GROBID or CrossRef. This is the weakest
inference task in the pipeline: the model must reconstruct a title and
publication type from indirect evidence. The output is tagged
`_resolution_method: llm_from_context` in the bibliography and has lower
reliability than GROBID-extracted or CrossRef-enriched entries. It is used
only when no other source is available. See `resolver-method.md` for the
full rationale for LLM-only resolution.

---

## Post-enrichment: near-duplicate resolution

**Task:** Given two bibliography entries with the same author+year and similar
titles, determine whether they refer to the same published work.

**Prompt** (`_llm_same_work` in `bibvik/postprocess.py`):

```
You are an expert bibliographer. Are the following two bibliography entries
the same published work? Consider title, author, and year.
Respond with only 'yes' or 'no'.

Entry A:
  Title: {title_a}
  Author: {author_a_family}
  Year: {year_a}

Entry B:
  Title: {title_b}
  Author: {author_b_family}
  Year: {year_b}

/no_think
```

**Design rationale:** Near-duplicate resolution is triggered by ≥70% token
overlap on both titles after the full enriched bibliography is built. Trivial
titles (Introduction, Conclusion, etc.) and titles under 20 characters are
excluded before comparison. The LLM is given author and year alongside the
titles because the same title can legitimately belong to different works (e.g.
two papers both titled "Viking Age Scandinavia"). Temperature is set to 0.0
for maximum determinism. If the LLM is unavailable or returns an inconclusive
response, the pair is flagged `_near_duplicate_candidate` for human review in
`--audit`.

---

## Inline: compound citation splitting

**Task:** Given a raw citation string containing multiple bibliographic
references merged together (GROBID compound citation blowout), split them into
individual references.

**Prompt** (`_split_compound_entry` in `bibvik/graph.py`):

```
You are an expert bibliographer. The following string contains multiple
bibliographic references merged together. Split them into individual references
and return a JSON array of objects, each with keys:
first_author_family, first_author_given, year, title, container_title, entry_type.

String: {raw_citation}

Respond ONLY with a JSON array. /no_think
```

**Design rationale:** Compound citations arise when GROBID fails to split
entries that span multiple works — most commonly dash-abbreviated author
repetitions in humanities reference lists. This pass runs inline during
per-paper graph construction, immediately after GROBID references are parsed,
for entries flagged `_possibly_compound` with a non-empty `_raw_citation` field.
Running inline (rather than post-hoc) means split entries participate in
deduplication against subsequent papers. If splitting produces fewer than two
entries or the LLM is unavailable, the original entry is used unchanged.
Split entries carry a `_split_from` field recording the originating citekey.

---

## Method 6: LLM bibliography re-parse from raw text

**Task:** Given the full raw reference list text from a paper's TEI back
section, parse it into structured bibliographic entries. Handles entries that
span PDF page breaks and dash-abbreviated author series that GROBID's structured
parser cannot recover.

**Prompt** (`_LLM_BIB_REPARSE` in `bibvik/detector.py`):

```
You are an expert at parsing academic bibliography sections. The text below is
a raw reference list extracted from a PDF. Some entries may span original page
breaks and appear garbled or truncated — do your best to recover them.

## Reference list text
---
{text}
---

For EACH distinct published work in the list, extract:
- first_author_family: family/surname of the first author
- first_author_given: given name(s) or initials of the first author
- additional_authors: list of {"family": "...", "given": "..."} for co-authors (empty list if sole author)
- year: publication year (4 digits)
- title: title of the article, chapter, or book
- container_title: journal name, book title (for chapters), or series name (empty string if standalone book)
- volume: volume number (empty string if n/a)
- pages: page range (empty string if n/a)
- doi: DOI if present (empty string if not)
- entry_type: one of "article", "book", "incollection", "inproceedings", "thesis", "misc"

Respond ONLY with a JSON array. If no references: []
/no_think
```

**Design rationale:** GROBID writes continuous raw reference list text into
`<div type="references">` in the TEI back section. Two GROBID failure modes
leave this text intact while producing broken `<biblStruct>` output: page-break
fragments (an entry split across a page boundary) and pre-split dash-abbreviated
series (each dash continuation becomes a separate biblStruct with no author).
Sending the full raw text to the LLM in a single call bypasses GROBID's
structured parser entirely for these cases.

The full reference list is sent in one call — no chunking — because completeness
is the priority and the list fits comfortably within the model's context window.
`max_tokens` is set to 4096 (vs 1024 for other methods) and the timeout to at
least 300 seconds to accommodate the larger input and output.

Results carry `_resolution_method: "llm_bib_reparse"` for provenance and are
integrated via the same rich-entry path used by Method 5 footnote extraction.
Entries already parsed correctly by GROBID are caught by deduplication; genuine
gaps land as new bibliography entries.

---

## Post-processing: title recovery from raw citations

**Task:** Given a raw citation string, extract only the title of the cited work.

**Prompt** (`_LLM_TITLE_RECOVERY` in `bibvik/postprocess.py`):

```
You are an expert bibliographer. The following is a raw citation string extracted from a bibliography.
Extract only the title of the cited work. Do not include author names, year, publisher, place of publication, volume, pages, or any other metadata.
If the string does not contain a recognisable title, respond with an empty string.

Raw citation:
{raw}

Respond with ONLY the title, nothing else. /no_think
```

**Design rationale:** GROBID sometimes extracts author and year from a
bibliography entry but fails to populate the title field, even when the title
is clearly present in the raw citation text. This pass targets those entries
specifically — ~179 in the June 2026 bibliography — and sends each raw string
to the LLM asking only for the title. The pass runs as part of `--postprocess`
after CrossRef enrichment, so entries already resolved by enrichment are
skipped. Temperature is set to 0 for maximum determinism. `num_predict` is set
to 100 — titles are short. Recovered titles are marked `_title_recovered: True`
for provenance.

---

## Citation function and content enrichment (unused in main pipeline)

`bibvik/llm_analyzer.py` also contains prompts for citation function analysis
(`CITATION_FUNCTION_PROMPT`) and content-enriched analysis
(`CONTENT_ENRICHED_PROMPT`). These prompts analyse how a cited work is used —
whether the citation is supportive, critical, or merely informational — and
optionally assess whether the citing author's characterisation is faithful to
the cited work. These functions are not called during the main graph-building
pipeline (`--iterate-f1`) but are available for post-hoc analysis.