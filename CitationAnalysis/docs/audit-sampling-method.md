# Audit Sampling Method

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, May 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

The BibVik citation graph is produced by an automated pipeline. Before the
graph can support substantive analysis, its accuracy must be validated: are
references being extracted correctly? Are detected citations being matched to
the right bibliography entries? Are duplicate entries being caught? Are
citations from OCR-processed or non-English source papers being handled
correctly?

Full manual review of the graph is not feasible at scale. This document
describes the stratified random sampling approach used to draw a
representative audit sample for human review.

## Stratified random sampling

A single random sample drawn from the full bibliography would be dominated
by the most common type of entry and would systematically underrepresent the
cases most likely to reveal problems — unresolved entries, minimally complete
records, entries from OCR-processed papers, and so on. Stratified random
sampling addresses this by dividing the population into subgroups (strata)
before sampling, and drawing independently from each.

The principle is well established in survey statistics. The standard reference
is:

> Cochran, W.G. (1977). *Sampling Techniques*, 3rd ed. New York: John Wiley
> & Sons. ISBN 9780471162407.

The core argument for stratification in Cochran (ch. 5) is that it reduces
variance when strata are internally homogeneous with respect to the quantity
of interest — here, error rate. Entries within each stratum share
characteristics correlated with the likelihood of extraction or matching
errors, making the strata appropriate units for targeted audit sampling.

## Manual validation of automated extraction

The practice of manually checking a sample of automatically extracted
bibliographic data against source documents is standard in information
extraction research. The most directly relevant precedent for reference
parsing pipelines is:

> Tkaczyk, D., Collins, A., Sheridan, P., & Beel, J. (2018). Machine Learning
> vs. Rules and Out-of-the-Box vs. Retrained: An Evaluation of Open-Source
> Bibliographic Reference and Citation Parsers. In *Proceedings of the 18th
> ACM/IEEE Joint Conference on Digital Libraries (JCDL '18)*, Fort Worth, TX,
> pp. 99–108. DOI: 10.1145/3197026.3197048.

This paper evaluates ten reference parsing tools — including GROBID, which
BibVik uses — against manually verified ground truth, establishing the
methodological precedent for spot-checking parsed output against the raw
citation strings from which it was derived.

An earlier paper by the same first author evaluates CERMINE, a comparable
system, using the same approach:

> Tkaczyk, D., Szostek, P., Fedoryszak, M., Dendek, P.J., & Bolikowski, Ł.
> (2015). CERMINE: automatic extraction of structured metadata from scientific
> literature. *International Journal on Document Analysis and Recognition
> (IJDAR)*, 18(4), 317–335. DOI: 10.1007/s10032-015-0249-8.

## Duplicate detection via string similarity

Identifying bibliography entries that should have been merged but were not
is an instance of the record linkage problem: given two records, do they
refer to the same real-world entity? The foundational theoretical treatment
is:

> Fellegi, I.P. & Sunter, A.B. (1969). A Theory for Record Linkage. *Journal
> of the American Statistical Association*, 64(328), 1183–1210.
> DOI: 10.1080/01621459.1969.10501049.

BibVik's duplicate detection uses Python's `difflib.SequenceMatcher` to
compute title similarity between pairs of entries. This is a simplified
application of the record linkage principle: rather than a probabilistic
model over multiple fields, a single string similarity score is used to flag
candidate pairs for human review. This is appropriate for audit purposes —
the goal is to surface candidates, not to make automated merge decisions.
The similarity threshold is configurable (default 0.85) and documented in
the audit output.

## Strata

The following strata are sampled independently. For each stratum, N entries
are drawn by simple random sampling without replacement, using a fixed random
seed for reproducibility. Where a stratum contains fewer than N entries, all
are included and the shortfall is noted in the output.

| Stratum | Rationale |
|---|---|
| CrossRef-resolved entries | Resolved entries have metadata from a second source; check that the CrossRef match is actually correct |
| Unresolved entries | No external validation; most likely to contain extraction errors |
| Minimal-completeness entries | Only bare minimum fields present; highest risk of being wrong or duplicated |
| Suspected duplicate pairs | High title/author similarity; may have escaped deduplication |
| Entries from OCR-processed papers | OCR may have introduced character errors corrupting names, titles, or years |
| Entries from non-English papers (per language) | Non-Latin characters most likely to produce encoding or transliteration errors; sampled independently per language |

## Sample size

N = 10 per stratum by default. This is a pragmatic choice, not a statistically
derived one: the goal is to support qualitative human judgment, not to produce
confidence intervals. Cochran (1977, ch. 4) notes that for qualitative audit
purposes, small samples are often sufficient to reveal systematic problems
provided the strata are well-chosen. The sample size can be adjusted via the
`--audit-n` flag.

## Random seed

The random seed is fixed at `42` by default and documented in the audit
output file. This ensures the sample is reproducible: re-running the audit
tool against the same graph state produces the same sample, allowing
annotations made during review to be compared against later pipeline runs.

## Output

The audit sample is written as a Markdown file (`output/audit_sample.md`)
designed for direct annotation. Each entry shows the raw citation string,
structured fields, source paper, and a blank Notes field. Duplicate candidate
pairs are shown side by side. The file is the review instrument — it is
edited directly during review and becomes part of the research record.

## Relationship to GROBID's published benchmarks

GROBID's developers publish benchmarks for reference extraction (~0.87 F1
against a PubMed Central holdout set of 1,943 PDFs) and citation context
resolution (0.76–0.91 F1 depending on collection). These are produced by
automated comparison against structured XML ground truth, not manual
spot-checking. They represent the most rigorous published evaluation of the
tool we use.

However, these benchmarks cannot be assumed to transfer to the BibVik corpus.
The PubMed Central corpus is biomedical literature in English, with standardised
journal layouts and consistent citation styles. The BibVik corpus is
fundamentally different: Viking Age archaeology literature spanning multiple
languages (Norwegian, Swedish, Danish, German, French, English), diverse
publication types (journal articles, edited volume chapters, monographs,
conference proceedings, grey literature), and a significant proportion of
older scanned material. GROBID's own documentation notes that performance
is sensitive to document layout and that evaluation results on clean data
are more optimistic than end-to-end performance on real corpora.

This domain gap is a primary motivation for independent manual validation
rather than reliance on published benchmarks.

## Approaches considered and not adopted

Several more rigorous validation approaches were considered and rejected,
documented here for transparency.

**Ground-truth mini-corpus.** The most principled approach would be to select
5–10 papers from the corpus whose complete reference lists are independently
known — either through personal familiarity or by manually constructing a
verified list from the physical document — and compare the pipeline's output
against this ground truth to produce a precision/recall estimate for the
specific characteristics of this corpus. This was considered and set aside for
practical reasons: constructing even a small verified ground truth for
multilingual humanities literature is time-consuming, the resulting sample
would be too small to be statistically meaningful, and the qualitative audit
approach is sufficient to identify systematic failure modes at this stage of
the project. The ground-truth approach remains available as a future option
if more rigorous quantitative validation is required for publication.

**Document type as a stratification variable.** GROBID's benchmarks show
that performance varies across document layouts and types. The BibVik corpus
includes journal articles, book chapters, edited volumes, and conference
proceedings with different citation styles, and stratifying by document type
would target failures specific to each. This was considered but not
implemented in the initial version of the audit tool because document type
is not yet consistently stored as a structured field in the graph state.
It should be revisited once additional metadata capture (item C in the
project todo) is complete.

**Separate stratum for LLM-extracted citations.** The pipeline uses both
GROBID and an LLM for citation detection. The LLM component has different
failure modes from GROBID — it may miss citations that GROBID finds, or
find citations in footnotes that GROBID misses. Adding a stratum specifically
for citations detected only by the LLM (and not by GROBID) would allow
targeted assessment of the LLM component's accuracy. This was considered
and deferred because the detection method is not currently stored per
citation in the graph state, making it impossible to filter by detection
source at audit time. This should be added to the data model before the
next major audit run.

**Automated validation against CrossRef metadata.** For the 413 entries
resolved via CrossRef, the CrossRef metadata provides a second source that
could be compared against GROBID's extraction to detect mismatches
automatically. This was considered but not implemented as a separate
validation step: the CrossRef match itself may be wrong (a plausible but
incorrect match), so CrossRef metadata cannot serve as ground truth. The
CrossRef-resolved stratum in the manual audit serves this purpose instead —
human review can catch cases where the CrossRef match is implausible.

## Limitations

This method cannot produce a precision/recall estimate for the pipeline
without a full ground-truth corpus, which does not exist for this dataset.
The audit sample supports qualitative assessment — identifying systematic
failure modes, flagging problematic entries, and building researcher
confidence in the graph — rather than quantitative performance measurement.
Language-stratified sampling requires language detection to be implemented
in the pipeline (see item C in the project todo). Until that is available,
the non-English strata are omitted from the audit output.
