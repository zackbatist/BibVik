# Reference Resolution Method

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

When the citation detection pipeline finds a citation — an (author, year) pair
in the body text or footnotes — that does not match any entry in the
bibliography extracted by GROBID, the resolver attempts to construct a
bibliographic record for it. This document describes the resolution strategy,
its known limitations, and how those limitations are addressed through manual
audit.

## Resolution tiers

Resolution proceeds through two tiers in order, falling back to a minimal stub
if both fail.

### Tier 1: CrossRef API

The CrossRef REST API is queried with the author surname and year. CrossRef
covers a large proportion of scholarly literature with DOIs, making it the
most reliable automated source for journal articles and book chapters published
after approximately 1990.

**Query strategy.** The query uses `query.author` (surname) and
`query.bibliographic` (year), returning the top 3 results. The first result
passing all acceptance criteria is used.

**Acceptance criteria.** A CrossRef result is accepted only if all of the
following hold:

1. **Year match.** The publication year in the CrossRef record matches the
   detected citation year exactly.

2. **Author match.** The normalised first author surname in the CrossRef
   record matches the normalised detected author surname. Normalisation uses
   `unidecode` transliteration and strips non-alphabetic characters, handling
   diacritics in Scandinavian, German, and French names. A full surname match
   is required; the earlier 4-character prefix match was too permissive and
   produced false positives (see Known Limitations below).

3. **Title/context plausibility.** At least one content word from the CrossRef
   title must appear somewhere in the combined citation contexts for that
   entry. This check is designed to catch the most obvious domain mismatches —
   a pedagogy or psychology paper matched to a Viking Age archaeology citation
   will have no vocabulary overlap with the contexts in which the citation
   appears. Content words are defined as words of 4+ characters not on a
   stopword list.

**Confidence scoring.** Resolved entries are tagged with
`_resolution_confidence`:

- `high`: full author match, title/context overlap confirmed, DOI present
- `medium`: full author match, title/context overlap confirmed, no DOI; or
  author match only (overlap check inconclusive due to short/vague title or
  non-English contexts)
- `low`: author match only, no overlap evidence

### Tier 2: LLM metadata inference

For citations that CrossRef cannot resolve — typically non-English works, grey
literature, older publications without DOIs, and Scandinavian/German/French
scholarship not well covered by CrossRef — the LLM is asked to infer full
bibliographic metadata from the citation contexts. The LLM has access to the
sentences in which the work was cited, which often contain enough information
to identify the title, journal, and publisher.

Entries resolved by LLM are tagged `_resolution_method: llm_from_context` and
`_resolution_confidence: medium` (if a title was inferred) or `low` (if not).

### Stub

If neither tier succeeds, a minimal stub record is created containing only
author and year, tagged `_resolution_method: stub` and
`_resolution_confidence: low`. Stubs are preserved in the bibliography because
the citation relationship (that this work was cited) is still valid even when
full metadata cannot be recovered.

## Known limitations

### CrossRef false positives

CrossRef matching on author surname and year alone is vulnerable to false
positives when multiple authors share a surname and published in the same year.
This was observed during initial testing: entries for Androshchuk (2018) and
Clarke (2017) were matched to a pedagogy paper and a cosmetics handbook
respectively — both wrong matches returned with high confidence because a DOI
was present.

The title/context plausibility check introduced in May 2026 eliminates the
most obvious domain mismatches. Subtler false positives — where the CrossRef
title happens to share vocabulary with the contexts — remain possible and are
addressed through manual audit (see below).

### Non-English contexts

The title/context overlap check may fail to validate correct matches when the
citation contexts are in a non-English language. A correct match for a
Norwegian or Danish paper may have an English CrossRef title with no word
overlap with the Norwegian contexts. In this case the confidence is downgraded
to `medium` rather than rejecting the match. Language-aware validation is
deferred to when language detection is implemented (item C in the project todo).

### CrossRef corpus coverage

CrossRef coverage is strongest for journal articles with DOIs, weaker for
books and edited volumes, and largely absent for grey literature, theses,
conference proceedings without DOIs, and pre-1990 scholarship. Viking Age
archaeology literature includes a significant proportion of material in these
underserved categories. These entries are expected to fall through to the LLM
tier or remain stubs.

## Relationship to manual audit

The resolver's known limitations are addressed through the stratified audit
sampling process documented in `docs/audit-sampling-method.md`. The
CrossRef-resolved stratum of the audit sample specifically targets resolved
entries for human review, checking that each CrossRef match is actually correct
rather than merely plausible. Entries tagged `_resolution_confidence: medium`
or `low` warrant closer scrutiny than `high` entries during audit review.

This combination — automated resolution with known limitations, documented
confidence scoring, and systematic manual verification — is the approach
described for automated reference parsing pipelines in:

> Tkaczyk, D., Collins, A., Sheridan, P., & Beel, J. (2018). Machine Learning
> vs. Rules and Out-of-the-Box vs. Retrained: An Evaluation of Open-Source
> Bibliographic Reference and Citation Parsers. In *Proceedings of the 18th
> ACM/IEEE Joint Conference on Digital Libraries (JCDL '18)*, Fort Worth, TX,
> pp. 99–108. DOI: 10.1145/3197026.3197048.

## Approaches considered and not adopted

**Restricting CrossRef to entries with raw citation strings.** Entries without
a `_raw_citation` field (i.e., citations detected only in the body text, not
extracted from a bibliography section) have no raw string to validate the
CrossRef match against. Restricting CrossRef resolution to entries with raw
strings would make every resolved entry more trustworthy, but would
substantially reduce resolution coverage. Given that the manual audit provides
a validation layer, this restriction was judged too conservative. The
title/context overlap check provides adequate automated filtering for the
no-raw-citation case.

**Subject/journal domain filtering.** Checking whether the CrossRef result's
journal or publisher falls within a plausible subject domain for Viking Age
archaeology was considered. This was not implemented because maintaining a
domain allowlist or blocklist is brittle and requires ongoing curation. The
title/context overlap check achieves similar filtering without requiring a
manually curated list.

**Title query to CrossRef.** When a `_raw_citation` string is available, the
CrossRef title query parameter could be used directly for more precise
matching. This would improve precision substantially for the ~570 entries that
have raw citation strings. Deferred for future implementation — the current
author+year query strategy is simpler and the audit provides a validation
backstop.
