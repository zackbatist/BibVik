# Deduplication and Normalisation

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, June 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

The BibVik bibliography accumulates entries from five detection methods across
382 F1 papers, the seed paper, and several resolution and enrichment passes.
Without deduplication, the same work would appear multiple times under
different citekeys. This document describes how BibVik identifies and merges
duplicate entries and normalises field values, and how these operations changed
over the course of the project.

---

## Normalisation

Normalisation is applied per entry at creation time, inside `normalize_entry()`
in `bibvik/normalize.py`. Every new entry — whether from GROBID, LLM resolution,
or footnote extraction — is normalised before being added to the bibliography.
This ensures the bibliography is always clean regardless of source.

**Title normalisation** handles:

1. ALL CAPS titles, common in older Scandinavian publications, converted to
   title case using a heuristic that detects script and applies language-
   appropriate capitalisation rules. English titles use standard title case;
   non-English titles use sentence case to avoid incorrect capitalisation of
   grammatical particles.

2. Inconsistent Unicode representation normalised to NFC form.

3. Letter prefix artifacts from year+suffix parsing — e.g. `"a: Title"` →
   `"Title"` — produced when GROBID parses `"2016a: Title"` and treats `a:`
   as a label. Stripped by regex before any case normalisation.

4. Hyphenated line breaks joined — `"Conti-\nnuity"` → `"Continuity"`.

5. LLM placeholder titles (e.g. `"Article by Ravdonikas"`, `"Статья В. И. X"`)
   cleared and stored in `_placeholder_title` for reference.

6. Oversized titles (over 300 characters) flagged with `_title_too_long: True`.
   These are almost always compound citation blowout — GROBID treating an
   entire raw citation string as a title. The title is preserved unchanged;
   the flag surfaces them for audit review.

**Date normalisation** extracts the 4-digit year from ISO 8601 date strings
(`"2016-01"` → `"2016"`) and stores it in both `date` and `year` fields.

**DOI normalisation** strips URL prefixes (`https://doi.org/`) to leave the
bare DOI string.

**Page range normalisation** removes spurious `e` characters (`e168` → `168`)
and normalises single hyphens to double hyphens (`6-30` → `6--30`).

**Volume extraction** detects page strings containing a volume number
(`"87, pp. 6-30"`) and moves the volume to the `volume` field.

**Entry type reclassification** is applied conservatively for `misc` entries
only, based on which fields are present (journaltitle + volume/pages → article;
booktitle + editors → incollection). Entries with specific types set by GROBID
are not touched at creation time — reclassification with enriched fields runs
later in `--postprocess`.

**Author normalisation** standardises given-name forms: initials are formatted
consistently, and corpus-wide given-name expansion (preferring the longest seen
form for each family name) runs at save time via `normalize_authors_in_bibliography()`.

---

## Deduplication

Deduplication happens in real time during graph construction, in
`_find_duplicate()` within `bibvik/graph.py`. Every new candidate entry is
checked against the existing bibliography before being added. Four matching
strategies are applied in order:

**DOI match.** If both the candidate and an existing entry have a DOI, and the
DOIs match after stripping whitespace and lowercasing, the entries are
considered identical. DOI matching is the most reliable strategy.

**Exact title match.** If both entries have a title of at least 20 characters,
and the titles match after normalisation (lowercased, punctuation stripped,
whitespace collapsed), the entries are considered identical.

**Author + year + fuzzy title match.** If the year and normalised first-author
family name match, and both entries have titles with at least 60% token overlap,
the entries are considered identical. If either entry lacks a title, author and
year alone are treated as sufficient.

**Cross-script match.** If the year matches and the transliterated first-author
family names match (Cyrillic → Latin via `_transliterate_author()` in
`bibvik/graph.py`), the entries are considered candidate duplicates. If their
titles also have ≥50% token overlap, or both lack titles, the entries are merged.
If titles differ despite matching transliterated authors and year, the existing
entry is flagged with `_cross_script_duplicate_candidate` for audit review —
automatic merging is not performed in this case because the works may be
genuinely distinct.

Transliteration uses the `domovyk` library, which implements the ALA-LC
Romanization tables — the standard used by North American libraries, the British
Library, and CrossRef. This covers Russian, Ukrainian, Bulgarian, Belarusian,
Serbian, Macedonian, Church Slavonic, and Carpatho-Rusyn. If `domovyk` is not
installed, the pipeline falls back to a hand-rolled ALA-LC table covering
Russian and Ukrainian basics. Transliterations are cached in `_TRANSLIT_CACHE`.

When a match is found, the existing entry is retained and the new candidate is
discarded. The `cited_by` list of the existing entry is updated. No fields are
merged — the first entry encountered wins. CrossRef enrichment (`--enrich`)
fills in missing fields after the graph is built.

### Post-hoc cross-script detection

Creation-time cross-script detection catches the common case where the same
work appears in one paper's reference list in Cyrillic and in another's in
Latin transliteration. However, different romanization conventions (German,
British, ALA-LC) can produce different transliterated forms that the creation-
time check misses. A post-hoc full-corpus scan is therefore also available in
`--audit`, which uses looser fuzzy matching and surfaces remaining cross-script
candidates for human review.

### Limitations

Deduplication fails when:

- Cross-script romanization conventions differ beyond what ALA-LC covers.
- A title is under 20 characters and no DOI is available.
- The same work is cited as both a book and a chapter, or with different
  date strings (e.g. "2002" vs "2002a"), creating entries distinct by all
  four criteria.

---

## Author-year matching during integration

Separate from deduplication, `_find_by_author_year()` links detected inline
citations to existing bibliography entries. The matching applies two rules:
exact match on normalised family name, then prefix match when the shared prefix
is at least 5 characters. Loose substring containment matching was rejected
because it produced false positives — "Lee" matched "Leech", "Li" matched
"Lindqvist".

---

## Post-enrichment reclassification

A second entry type reclassification pass runs as part of `--postprocess`, after
CrossRef enrichment has filled in `journaltitle`, `volume`, `pages`, and other
fields. This pass operates on all entry types (not only `misc`) and uses the
enriched fields to make more accurate classifications. It applies guards against:

- Reclassifying `incollection` to `inbook` due to missing editor data
- Reclassifying `book` to `article` when only a series name appears in `journaltitle`
  (requires a page range, not just a volume number)
- Reclassifying `book` to `inbook/incollection` when the booktitle matches the
  entry's own title (the entry is the book itself, not a chapter)

---

## Citekey generation

Citekeys are generated by `generate_citekey()` in `bibvik/utils.py`. The
format is `familynameyear` — first author's normalised family name (lowercased,
diacritics removed, non-alphabetic characters stripped) followed by the 4-digit
year. Collisions (same author and year) receive lowercase letter suffixes:
`price2002`, `price2002a`, `price2002b`, etc.

Citekeys are stable within a run but not guaranteed stable across runs in
parallel mode, since processing order is non-deterministic. Treat citekeys as
internal identifiers, not persistent external references.

---

## GROBID entry filtering at ingestion

Before a GROBID-derived entry is added to the bibliography, `_is_reconstructible()`
in `bibvik/graph.py` checks whether the normalized fields are sufficient to
assemble a minimal Chicago author-date citation. The check runs after
`normalize_entry()` — so entry type inference and field cleanup have already
been applied — and is conditional on an LLM being configured.

The check is framed as: are the fields sufficient to identify this entry as a
specific publication? Requirements vary by entry type:

- `article`, `incollection`, `inproceedings`, `thesis`: author + year + title
- `book`: (author or editor) + year + title
- `misc`: year + (author or title)

A separate check catches page-break fragments regardless of parsed fields: a
raw citation starting with a lowercase character that is not a known particle
(`von`, `van`, `de`, `di`, `el`, `al-`, `la`, `le`, `du`, `des`, `den`, `der`,
`das`, `ten`, `ter`, `op`, `af`, `av`) is a mid-word continuation produced by
GROBID splitting an entry across a PDF page break. These are skipped
unconditionally.

Two further checks catch entries that passed the field presence test but are
not standalone bibliography entries:

**Catalogue/findspot entries** (`_CATALOGUE_PARENS_RE`): entries where the year
GROBID extracted comes only from a parenthetical cross-reference such as
`(Pedersen 1995, 71)` rather than from the entry's own publication year. These
are artefact catalogue records — findspot descriptions, typological catalogue
entries — where GROBID misidentified a place name or catalogue identifier as an
author and borrowed the year from a nearby cross-reference. The actual referenced
work already exists in the bibliography under its own citekey. Five parenthetical
patterns are covered: page references, catalogue numbers (Kat-Nr), plate
references (Taf), page ranges (ff.), and figure references (Abb). Pending
validation on the full corpus — Scandinavian place names (Lund, Bergen, Oslo)
may appear as author surnames in legitimate parenthetical citations and produce
false positives (todo AF).

**Shorthand back-references** (`_SHORTHAND_RE`): raw citations of the form
`Author Year` or `Author/Author Year` with nothing else — cross-reference
shorthand pointers to entries already in the bibliography, not standalone
references.

The check is applied unconditionally regardless of LLM availability. Previously
it was gated on LLM configuration, which allowed ghost entries (GROBID biblStructs
with journal/volume metadata but no author, title, or year) to enter the
bibliography when the LLM was not available at processing time. The unconditional
check ensures ghost entries are suppressed regardless of whether an LLM is
configured for the run.

---

## Year validation

Year extraction in `normalize_entry()` checks that the extracted year falls
within a plausible range (1450–2030) before writing to the `date` and `year`
fields. The lower bound of 1450 accepts genuinely old historical sources
legitimately cited in Viking Age scholarship (16th–18th century works) while
rejecting pre-printing-press dates and common failure modes: page numbers
absorbed as years (e.g. `0177`), medieval historical dates absorbed from
content (e.g. `1050`), and garbled OCR (e.g. `2602`). Years outside the range
are cleared and a debug log entry is written.

---

## OCR quality detection — considered and not adopted

A proactive OCR quality detection step was considered for identifying degraded
PDFs before sending them to GROBID. The `ocr-text-aligner` tool (Farr 2026)
maps LLM-cleaned text back onto ALTO XML word by word using fuzzy matching and
geometric proximity, producing per-word confidence scores (`ALIGNCONF`) that
could serve as an OCR quality signal. This approach was not implemented for two
reasons: BibVik does not use ALTO XML output (it reads GROBID's TEI-XML derived
from the PDF text layer), and the tool's primary purpose — restoring bounding
boxes for searchable/accessible document delivery — is not relevant to BibVik's
pipeline. The approach remains available as a future option if OCR quality
becomes a systematic problem and the pipeline is extended to use Tesseract
output directly.

> Farr, C. (2026). *OCR Text Aligner: Maps LLM-cleaned text to ALTO XML OCR
> elements using fuzzy string matching, context-based scoring, and geometric
> proximity analysis.* GitHub. https://github.com/chloe-farr/ocr-text-aligner