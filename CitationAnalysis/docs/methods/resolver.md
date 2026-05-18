# Reference Resolution and Enrichment Method

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Overview

The BibVik pipeline separates two distinct operations that were initially
conflated:

1. **Resolution** (`bibvik/resolver.py`): building a bibliographic record for
   an unmatched citation during graph construction. Runs inline during
   `--iterate-f1`. Only the LLM is used; CrossRef is not.

2. **Enrichment** (`bibvik/enricher.py`): filling in missing metadata fields
   on already-identified bibliography entries. Runs as a separate pass after
   graph construction, via `--enrich`. Uses CrossRef (for bibliography entries)
   and OpenAlex (for paper authors).

This separation reflects a fundamental design decision: CrossRef is an
enrichment tool, not an identification tool. The reasons are documented in
detail below.

---

## Resolution

### What resolution does

During F1 paper processing, the detector finds citations in the body text —
(author, year) pairs — that do not match any existing bibliography entry.
These are "unmatched" citations: we know a work was cited but don't have a
record for it.

The resolver attempts to build a bibliographic record using the LLM. It passes
the citation contexts (the sentences in which the citation appears) to the LLM
and asks it to infer full metadata: title, journal, entry type, co-authors.

Entries resolved by LLM are tagged:
- `_resolution_method`: `"llm_from_context"` or `"llm_from_footnote"`
- `_resolution_confidence`: `"medium"` (if title inferred) or `"low"`

Entries that cannot be resolved (LLM unavailable, or no contexts) become stubs:
- `_resolution_method`: `"stub"`
- `_resolution_confidence`: `"low"`

Stubs preserve the citation relationship (that the work was cited) even without
metadata. Many stubs are resolved naturally as the corpus grows: a work cited
only in passing in one paper may appear as a complete GROBID bibliography entry
in another paper processed later in the run. The `_merge_into` logic in
`graph.py` handles this consolidation automatically.

### Why CrossRef is not used for resolution

The earlier implementation used CrossRef as the primary resolution tier,
querying by author surname and year. This was found to produce a high rate of
false positives during audit review (May 2026): approximately 70–80% of
CrossRef-resolved entries in the audit sample were matched to wrong papers —
a field marshal's memoirs, a hepatic stellate cell study, a Norwegian film
policy paper, and similar domain mismatches.

The root cause is structural. CrossRef's API always returns a result — it does
not indicate when a work is absent from its database. Querying by surname and
year is a very weak signal: many authors share a surname, and CrossRef returns
whatever scores highest against the query regardless of subject domain. The
BibVik corpus includes a large proportion of Viking Age archaeology literature
(Scandinavian monographs, edited volumes, museum publications, conference
proceedings) that is systematically underrepresented in CrossRef, meaning the
false positive rate is structurally high for this specific corpus.

A title/context overlap check was implemented to filter false positives, but
proved insufficient: when contexts are empty or short (as they often are for
citations detected only by GROBID inline markers), the overlap check falls back
to "inconclusive" and accepts the match at medium confidence. The fundamental
problem — that CrossRef has nothing reliable to match against — cannot be fixed
by filtering alone.

**Approaches considered and not adopted:**

*Restricting CrossRef to entries with raw citation strings.* Entries extracted
from the bibliography section by GROBID have raw citation strings that could be
used to validate CrossRef matches. This was considered but rejected: those
entries already have structured metadata from GROBID; they are the ones that
least need CrossRef resolution. The entries that most need resolution (bare
body-text detections) are exactly the ones with no raw string and therefore the
weakest CrossRef queries.

*Deferred CrossRef resolution (end-of-run pass).* Running CrossRef after all
F1 papers have been processed, when many entries have been built up through
natural graph accumulation, would give CrossRef more to work with. This remains
true, but the fundamental problem persists: CrossRef still returns wrong matches
for entries not in its database, and a larger bibliography just means more
false positives in absolute terms. Enrichment-mode CrossRef (see below) avoids
this by requiring a confirmed title match before accepting any result.

---

## Enrichment

### Bibliography enrichment (CrossRef)

Enrichment runs after `--iterate-f1` via `--enrich` or `--enrich-bib-only`.
It uses CrossRef strictly to fill in missing metadata fields on entries whose
identity is already established — never to determine what an entry is.

**Two enrichment strategies:**

*DOI lookup.* For entries with a DOI (extracted by GROBID from the bibliography
section), the CrossRef API is queried by DOI directly. This is fully reliable:
a DOI is a unique identifier, and CrossRef's DOI endpoint returns the canonical
record. Fields filled in: volume, issue, pages, canonical journal name, full
author given names, publisher.

*Title query.* For entries with a title but no DOI, CrossRef is queried by
title + author. A result is accepted only if the title similarity (computed by
`difflib.SequenceMatcher`) is ≥ 0.85. This threshold is high enough to reject
near-misses while accepting genuine matches. On acceptance, the DOI and missing
metadata fields are filled in. The threshold is configurable via
`--enrich-threshold`.

In both cases, enrichment is additive only — existing fields are never
overwritten. The entry's identity (citekey, generation, cited_by) is never
changed.

**CrossRef as enrichment, not identification:** This is the correct role for
CrossRef in this corpus. CrossRef is excellent at returning metadata for a work
you have already identified (given a DOI or a precise title). It is poor at
identifying a work from a weak query (author surname + year). The enrichment
design exploits CrossRef's strength while avoiding its weakness.

**Coverage limitations:** CrossRef enrichment will succeed for entries from
journals and book publishers with DOI infrastructure. A significant portion of
the BibVik bibliography — older Scandinavian publications, museum reports, grey
literature — will not be in CrossRef and will remain unenriched. This is
expected and acceptable; it does not affect the correctness of the citation
graph, only the completeness of individual records.

### Author enrichment (OpenAlex)

Author enrichment runs via `--enrich` or `--enrich-auth-only`. It operates on
the paper header data stored in `processed_papers` — the authors of the F1
papers themselves, not the authors of cited works.

GROBID frequently extracts author given names as initials only (e.g. "J. H."
rather than "James H."). It also extracts affiliations inconsistently — some
papers have structured affiliation data, many do not.

OpenAlex is queried by author name to find canonical author profiles. OpenAlex
integrates ORCID as a primary data source for author disambiguation (since July
2023), so a single OpenAlex query provides access to ORCID-verified profiles
without a separate ORCID API query:

> OpenAlex documentation: "Our information about authors comes from MAG,
> Crossref, PubMed, ORCID, and publisher websites, among other sources."
> https://docs.openalex.org/api-entities/authors

Fields enriched per author:
- Full given name (if GROBID extracted only initials)
- ORCID identifier
- OpenAlex author ID
- Current institutional affiliation (name, ROR identifier, country)

**Approach considered and not adopted:** Querying ORCID directly. OpenAlex
already integrates ORCID and provides a unified interface with additional
disambiguation. Querying both would add complexity without adding coverage.

**Coverage limitations:** OpenAlex coverage is strongest for researchers with
significant publication records in indexed journals. Early-career researchers,
authors who publish primarily in regional or non-English venues, and authors
without ORCID profiles may not be found. The enrichment is additive — authors
not found in OpenAlex retain whatever GROBID extracted.

**Author affiliation data quality note:** GROBID's affiliation extraction is
inconsistent across the corpus (see `docs/methods/data-capture.md`). OpenAlex
enrichment supplements but does not replace the raw affiliation data. All
affiliation data — whether from GROBID or OpenAlex — should be treated as
unvalidated until reconciled against ROR identifiers.

---

## Relationship to manual audit

The audit tool (`--audit`) samples bibliography entries for human review,
including a CrossRef-resolved stratum. With CrossRef no longer used for
resolution, the CrossRef-resolved entries in the bibliography are those enriched
via `--enrich`, where matches are held to a higher standard (DOI lookup or
≥0.85 title similarity). The audit stratum label `_resolution_method: crossref`
now indicates enrichment-mode CrossRef, not identification-mode CrossRef.

Enrichment should be run before the audit for the most meaningful CrossRef
stratum sample.