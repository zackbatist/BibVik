# Citation Detection Method

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

For each paper in the corpus, BibVik applies five citation detection methods
independently and merges their results. This document describes the design
rationale, each method, the merging logic, and known limitations.

## Design rationale: no single authoritative source

The conventional approach to citation extraction treats the bibliography
section as ground truth and links inline citations to it. This works well for
standardised journal articles but fails systematically for the BibVik corpus,
which includes:

- Footnote-style papers (Chicago style) where the bibliography is embedded in
  footnotes rather than a separate section
- Papers with non-standard or absent bibliography sections
- Discursive citations not recognisable as formal (author, year) pairs
- Non-English citation styles (Scandinavian, German, French)
- Scanned PDFs where OCR quality affects text layer reliability

The five-method approach reflects a design principle: no single source is
treated as authoritative. Each method has different strengths and failure modes.
Running all five and merging their results produces a more complete record than
any single method alone. The tradeoff is speed — Method 4 (LLM body scan) is
the bottleneck — but completeness is the priority for this research.

This approach is consistent with the evaluation methodology in:

> Tkaczyk, D., Collins, A., Sheridan, P., & Beel, J. (2018). Machine Learning
> vs. Rules and Out-of-the-Box vs. Retrained: An Evaluation of Open-Source
> Bibliographic Reference and Citation Parsers. In *Proceedings of the 18th
> ACM/IEEE Joint Conference on Digital Libraries (JCDL '18)*, Fort Worth, TX,
> pp. 99–108. DOI: 10.1145/3197026.3197048.

That paper evaluates ten reference parsing tools including GROBID and finds
that no single tool dominates across all document types and citation styles,
motivating ensemble approaches.

## The five methods

### Method 1: GROBID bibliography extraction

GROBID (GeneRation Of BIbliographic Data) applies Conditional Random Field
models to parse the PDF's reference list into structured `<biblStruct>` entries
in TEI-XML format. Each entry contains author names, title, journal, volume,
pages, DOI, and other metadata as available.

**Implemented in:** `tei_parser.py` (`parse_tei_references()`), called from
`detector.py` (`_method_grobid_bibliography()`).

**Post-processing:** GROBID frequently collapses multiple references by the same
author into a single entry when the reference list uses the humanities dash
convention (e.g. `—1987. Title Two.` where the dash replaces the repeated
author name). A compound reference splitter in `tei_parser.py` detects these
patterns and splits them into individual entries, preserving the original
author. It also detects cases where GROBID merged references from different
authors (an author-boundary merge).

**Output:** (author, year) pairs for deduplication, plus full structured
metadata for graph building.

**Strengths:** Richest structured metadata. DOIs when available. Handles
complex bibliography layouts better than regex.

**Weaknesses:** Trained predominantly on STEM journal articles. Under-performs
on humanities citation conventions, non-standard layouts, footnote-only papers.
Misses citations that appear only in the body text.

### Method 2: GROBID inline marker extraction

GROBID annotates inline citations in the body text as `<ref type="bibr">`
elements, linking them to their corresponding bibliography entries via `target`
attributes. We extract (author, year) pairs from the marker text of these
elements independently of whether the corresponding bibliography entry was
successfully parsed.

**Implemented in:** `detector.py` (`_method_grobid_inline()`).

**Rationale:** GROBID may successfully identify an inline citation marker even
when its bibliography entry was malformed or missing. Extracting from markers
provides a second route to detecting the citation even when Method 1 fails for
that entry.

**Strengths:** ML-detected, handles diverse inline citation styles.

**Weaknesses:** Only catches citations that GROBID recognised as references.
Does not detect citations GROBID missed entirely.

### Method 3: Regex pattern matching

Regular expression patterns applied to the raw body text, independent of GROBID.
Two pattern families are used:

- **Parenthetical:** `(Smith 2020)`, `(Smith and Jones 2020)`, `(Smith et al. 2020)`,
  semicolon-separated groups `(Smith 2020; Jones 2021)`
- **Narrative:** `Smith (2020)`, `Smith and Jones (2020)`

Patterns handle non-English name particles (`de`, `van`, `von`, `di`) and
connectors (`og`, `und`, `och`, `et` for and). Non-ASCII characters in author
names (Scandinavian, German, French) are matched via Unicode character ranges.

**Implemented in:** `detector.py` (module-level compiled regexes, `_method_regex()`).

**Compatibility:** Python 3.14 introduced stricter regex parsing. The patterns
are compiled with a try/except fallback to simplified patterns if the primary
patterns cause `re.PatternError`. En-dash and em-dash characters are expressed
as `\u2013\u2014` Unicode escapes rather than literal characters.

**Strengths:** Fast. Catches formally styled author-year citations that GROBID
missed. Provides citation contexts (surrounding text) for unmatched citations.

**Weaknesses:** Cannot detect discursive references, citations with
organisational authors, or numbered citation styles. Prone to false positives
on non-citation text that matches the pattern. Least reliable method for
multilingual text.

### Method 4: LLM body scan

Each paragraph is sent to the local LLM (qwen3.5:35b via Ollama) with a prompt
asking it to identify all referenced works. The LLM returns a JSON array of
`{first_author, year}` pairs.

**Implemented in:** `detector.py` (`_method_llm_body()`), prompt defined as
`_LLM_BODY_DETECT`.

**Prompts as published methodology:** The prompt is defined as a readable string
constant in source code (`_LLM_BODY_DETECT` in `detector.py`) so it can be
inspected, quoted, and critiqued in the methods section of publications.

**Caching:** Paragraph text is hashed (MD5) and results are cached in memory.
On subsequent runs against the same paragraph, the cached result is used
without re-querying the LLM. Only non-empty results are cached — failed queries
are retried on the next run.

**Batching:** The `detection_batch_size` config parameter controls how many
paragraphs are processed per LLM call. Currently set to 1 (one paragraph per
call) after an earlier attempt at true batching (multiple paragraphs per call)
broke response parsing. Revisiting this is item E on the project todo.

**Strengths:** Most comprehensive. Catches discursive references, organisational
authors, and any citation style recognisable to the LLM.

**Weaknesses:** Slowest method by a large margin. May produce false positives.
Requires Ollama to be running; skipped if unavailable.

### Method 5: LLM footnote extraction

Each footnote containing a year pattern is sent to the LLM with a structured
extraction prompt. Unlike Method 4 (which returns only author+year), Method 5
requests full structured bibliographic metadata: title, journal, volume, pages,
entry type, co-authors. The LLM returns a JSON array where each element
represents one distinct work cited in the footnote.

**Implemented in:** `detector.py` (`_method_llm_footnotes()`), prompt defined
as `_LLM_FOOTNOTE_EXTRACT`. Footnotes are extracted from TEI-XML by
`parse_tei_footnotes()` in `tei_parser.py`.

**Rationale:** Corpus-wide scanning found footnote-embedded bibliographic
references in 22 footnotes across 10 papers. Papers using Chicago footnote style
embed full references in footnotes rather than a separate bibliography section;
GROBID's standard bibliography extractor misses these entirely. Method 5 is
essential for these papers.

**Rich entries:** Method 5 produces "rich entries" — structured bibliography
records with full metadata — rather than just (author, year) pairs. These are
passed directly to the graph builder for incorporation into the bibliography.

**Strengths:** Extracts rich metadata from footnote prose. Handles multiple
references per footnote (a single footnote may cite several works).

**Weaknesses:** Depends on LLM accuracy. Short footnotes and those without year
patterns are filtered out before sending to the LLM.

## Merging and deduplication

After all five methods run, their results are merged by `_merge_all()` in
`detector.py`. The merge key is `(normalised_author, year)` where normalisation
uses `unidecode` transliteration and strips non-alphabetic characters — the same
normalisation used throughout the pipeline for author name handling.

When multiple methods detect the same citation, their method tags are combined
in the `methods` list of the merged record. This provenance is stored in the
graph state and will support later analysis of method coverage and reliability.

Occurrence counts are accumulated across methods; up to five citation contexts
are stored per merged citation (drawn from whichever methods provided them).

## Known limitations

**Completeness vs. false positives:** Running all five methods increases recall
but also increases the risk of false positives. Method 3 (regex) is the most
prone to false positives; Method 1 (GROBID bibliography) is the most precise.
The merged result is the union of all detections — no method's output is
discarded.

**LLM dependency:** Methods 4 and 5 require Ollama to be running with a
compatible model. If unavailable, the pipeline falls back to Methods 1–3.
For the full corpus run, this means LLM-only detections (discursive references,
footnote-embedded citations) will be absent unless Ollama is available.

**Language:** Regex patterns (Method 3) handle the six corpus languages
explicitly. Methods 1, 2, 4, and 5 are language-agnostic in principle, though
GROBID's models were trained predominantly on English-language literature and
may under-perform on non-English papers.

**Non-unique citekeys across methods:** Different methods may extract the same
citation with different author name forms (e.g. "Sindbæk" vs "Sindbaek").
The normalisation step collapses these, but edge cases (very short surnames,
particle variations) may produce spurious duplicates or missed merges.
