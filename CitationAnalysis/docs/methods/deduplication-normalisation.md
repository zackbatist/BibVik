# Deduplication and Normalisation

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

The BibVik bibliography accumulates entries from five detection methods across
382 F1 papers, the seed paper, and several resolution and enrichment passes.
Without deduplication, the same work would appear multiple times under
different citekeys — e.g. "Price 2002" detected as an inline marker in one
paper, as a GROBID bibliography entry in another, and as an LLM-resolved stub
in a third. This document describes how BibVik identifies and merges duplicate
entries, and how it normalises field values across entries from different sources.

## Deduplication

Deduplication happens in real time during graph construction, in
`_find_duplicate()` within `bibvik/graph.py`. Every new candidate entry is
checked against the existing bibliography before being added. Three matching
strategies are applied in order:

**DOI match.** If both the candidate and an existing entry have a DOI, and the
DOIs match after stripping whitespace and lowercasing, the entries are
considered identical. DOI matching is the most reliable strategy and is always
preferred when available.

**Exact title match.** If both entries have a title of at least 20 characters,
and the titles match after normalisation (lowercased, punctuation stripped,
whitespace collapsed), the entries are considered identical. The 20-character
threshold prevents spurious matches on short or generic titles.

**Author + year + fuzzy title match.** If the year and normalised first-author
family name match, and both entries have titles with at least 60% token overlap
(measured as the intersection over the smaller title's token set), the entries
are considered identical. If either entry lacks a title, author and year alone
are treated as sufficient for a match. This handles cases where GROBID extracts
slightly different title strings from the same work appearing in different
reference lists.

When a match is found, the existing entry is retained and the new candidate is
discarded. The `cited_by` list of the existing entry is updated to record the
citing paper. No fields are merged — the first entry encountered wins. This
means field completeness depends on the order in which papers are processed,
which in parallel mode is non-deterministic. CrossRef enrichment (`--enrich`)
addresses this by filling in missing fields after the graph is fully built.

### Limitations

Deduplication fails when:

- Author names are transliterated inconsistently (e.g. "Ravdonikas" vs
  "Равдоникас"), since the matching operates on normalised Latin-script strings.
  Cross-script duplicates are flagged by the `--postprocess` pass but not
  automatically merged.
- A title is very short (under 20 characters) and no DOI is available, so
  neither the DOI nor the exact title strategy can apply, and the author+year
  strategy may under-match.
- The same work is cited as both a book and a chapter, or with different
  date strings (e.g. "2002" vs "2002a"), leading to separate entries that
  appear distinct by all three criteria.

## Author-year matching during integration

Separate from deduplication (which checks new entries against existing ones),
`_find_by_author_year()` links detected inline citations to existing
bibliography entries. When the detector identifies a citation like "(Price
2002)", it normalises the author name and year and checks whether an entry
already in the bibliography matches.

The matching applies two rules in order: exact match on normalised family name,
then prefix match when the shared prefix is at least 5 characters (to handle
normalisation differences like diacritics removed or truncated initials). Loose
substring containment matching was deliberately rejected because it produced
false positives — "Lee" matched "Leech", "Li" matched "Lindqvist" — which
created spurious citation edges.

When no match is found, the citation becomes an unmatched entry passed to the
resolver for LLM-based identification.

## Normalisation

Normalisation is applied to bibliography entries at two points: during graph
construction (via `normalize_entry()` in `bibvik/normalize.py`) and at save
time (via `normalize_titles_in_bibliography()` and
`normalize_authors_in_bibliography()`).

**Title normalisation** handles three main problems:

1. ALL CAPS titles, common in older Scandinavian publications, are converted to
   title case using a heuristic that detects the script and applies language-
   appropriate capitalisation rules. English titles use standard title case;
   non-English titles use sentence case (first word only capitalised) to avoid
   incorrect capitalisation of Scandinavian grammatical particles.

2. Inconsistent Unicode representation — e.g. composed vs decomposed diacritics
   — is normalised to NFC form so that string comparison works correctly across
   entries from different sources.

3. Trailing punctuation and extraneous whitespace are stripped.

**Author normalisation** standardises family and given name fields: names
extracted in "Last, First" order are split and reordered, initials are expanded
where CrossRef provides full names, and Unicode normalisation is applied. No
attempt is made to disambiguate authors with the same name.

**Date normalisation** extracts the 4-digit year from ISO 8601 date strings
(e.g. "2016-01" → "2016") and removes non-numeric characters from bare year
strings.

**Post-processing** (`--postprocess`, `bibvik/postprocess.py`) applies a
further set of cleaning passes after enrichment, addressing artifacts that
are best corrected in bulk after the full corpus is processed rather than
during graph construction: letter prefixes leaked from year+suffix parsing
(e.g. "a: Title"), hyphenated line-break titles, oversized titles from compound
citation blowout, DOI and page range format normalisation, LLM placeholder
title removal, and entry type reclassification.

## Citekey generation

Citekeys are generated by `generate_citekey()` in `bibvik/utils.py`. The
format is `familynameyear` — first author's normalised family name (lowercased,
diacritics removed, non-alphabetic characters stripped) followed by the 4-digit
year. When a citekey collision occurs (same author and year for a different
work), a lowercase letter suffix is appended: `price2002`, `price2002a`,
`price2002b`, etc.

Citekeys are stable within a run but not guaranteed stable across runs, since
the order in which works are first encountered depends on paper processing
order, which in parallel mode is non-deterministic. This means citekeys should
be treated as internal identifiers rather than persistent external references.
