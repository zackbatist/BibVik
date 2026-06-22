# Audit Method

> **Note:** This document was drafted with the assistance of Claude (Anthropic,
> claude-sonnet-4-6, 2026) and reviewed by the project author. All cited
> sources were independently verified to exist before inclusion. No sources have
> been inferred or hallucinated.

## Purpose

The BibVik citation graph is produced by an automated pipeline. Before the
graph can support substantive analysis, its quality must be assessed: are
references being extracted correctly? Are detected citations being matched to
the right bibliography entries? Are duplicate entries being caught? Are
citations from OCR-processed or non-English source papers being handled
correctly?

Full manual review of the graph is not feasible at scale. The audit is a
diagnostic instrument — a report that enables a researcher to form a judgment
about whether the bibliography is trustworthy enough to analyse. It does not
produce a list of things to fix. Corrective actions are recorded in
`corrections.yaml`; see the corrections system documentation.

The intended audience is domain experts in Viking Age archaeology, not
pipeline developers. The report must be interpretable without knowledge of
how the pipeline works.

## Quality dimensions

The audit report is organised around four data quality dimensions drawn from
Wang & Strong (1996), adapted to BibVik's specific failure modes:

> Wang, R.Y. & Strong, D.M. (1996). Beyond accuracy: What data quality means
> to data consumers. *Journal of Management Information Systems*, 12(4), 5–33.
> DOI: 10.1080/07421222.1996.11518099.

Wang & Strong identify fifteen data quality dimensions through empirical study
of data consumers. Of these, the following four are applicable to BibVik:

**Completeness** — are entries missing required fields? The pipeline has
distinct failure modes per detection method: GROBID commonly fails to extract
titles from complex layouts; Method 6 (LLM re-parse) sometimes returns entries
without year. Completeness is assessed as field-presence rates broken down by
detection method.

**Accuracy** — are the extracted values correct? Assessed through sampling:
a reviewer reading entries from non-English papers can judge whether names and
titles are rendering correctly in a way that no automated check can. Also
assessed through CrossRef-resolved entries, where the CrossRef metadata
provides a second source against which the reviewer can check the GROBID
extraction.

**Representational consistency** — are the same works represented consistently
across entries? The primary failure mode is deduplication misses — the same
work appearing under two citekeys because author name rendering differed
between source papers. Assessed through a sample of suspected duplicate pairs.

**Coverage** — did the pipeline process the whole corpus? Papers with
unusually low citation counts may indicate a bibliography the pipeline failed
to extract, either because GROBID produced no output or because the reference
list was formatted in a way the pipeline did not handle. Assessed through a
ranked list of papers by citation count.

A fifth dimension specific to BibVik's architecture is also reported:

**Provenance** — what fraction of entries came from each detection method?
The pipeline uses six detection methods (GROBID bibliography, GROBID inline
markers, regex, LLM body scan, LLM footnote extraction, Method 6 LLM
re-parse). An unexpected distribution — for example, very few Method 6
recoveries — is a signal of systematic failure in that method.

Dimensions from Wang & Strong that do not apply to BibVik: Concise
representation (data verbosity, not a pipeline concern), Ease of understanding
(human readability of data formats, not applicable), and Relevancy (a research
design question, not a pipeline quality question).

## Sampling

A single random sample drawn from the full bibliography would be dominated by
the most common entry type and would underrepresent cases most likely to reveal
problems. The audit uses targeted sampling: each section draws a small sample
from the specific population relevant to the quality dimension being assessed.

The principle is established in survey statistics:

> Cochran, W.G. (1977). *Sampling Techniques*, 3rd ed. New York: John Wiley
> & Sons. ISBN 9780471162407.

Cochran (ch. 5) argues that stratification reduces variance when subgroups are
internally homogeneous with respect to the quantity of interest — here, error
rate. The audit samples separately from populations with correlated error
characteristics: non-English source papers for accuracy assessment, unresolved
entries for completeness, and so on.

Sample sizes are small (10 per section by default) because the goal is
qualitative judgment, not statistical estimation. Cochran (ch. 4) notes that
for qualitative audit purposes, small samples are sufficient to reveal
systematic failure modes provided the populations are well-chosen.

A fixed random seed (default 42) ensures the sample is reproducible: re-running
the audit against the same graph state produces the same sample, allowing
comparison across pipeline versions.

## Manual validation of automated extraction

The practice of manually checking a sample of automatically extracted
bibliographic data against source documents is standard in information
extraction research:

> Tkaczyk, D., Collins, A., Sheridan, P., & Beel, J. (2018). Machine Learning
> vs. Rules and Out-of-the-Box vs. Retrained: An Evaluation of Open-Source
> Bibliographic Reference and Citation Parsers. In *Proceedings of the 18th
> ACM/IEEE Joint Conference on Digital Libraries (JCDL '18)*, Fort Worth, TX,
> pp. 99–108. DOI: 10.1145/3197026.3197048.

This paper evaluates ten reference parsing tools — including GROBID — against
manually verified ground truth, establishing the methodological precedent for
spot-checking parsed output against the raw citation strings from which it was
derived.

## Duplicate detection

Identifying bibliography entries that should have been merged but were not
is an instance of the record linkage problem:

> Fellegi, I.P. & Sunter, A.B. (1969). A Theory for Record Linkage. *Journal
> of the American Statistical Association*, 64(328), 1183–1210.
> DOI: 10.1080/01621459.1969.10501049.

The audit identifies candidate duplicate pairs by computing title token overlap
between entries sharing the same first-author surname and year. Pairs with ≥70%
overlap that were not auto-merged are flagged for human review. This is a
simplified application of the record linkage principle: a single similarity
score is used to surface candidates, not to make automated merge decisions.
The threshold is configurable and documented in the report.

## Report sections

| Section | Dimension | What the reviewer assesses |
|---|---|---|
| Overview | — | Overall corpus size, generation breakdown, papers processed vs failed |
| Completeness | Completeness | Missing-field rates by detection method; sample of no-title entries |
| Accuracy | Accuracy | Sample from non-English papers; sample of CrossRef-resolved entries |
| Representational consistency | Representational consistency | Sample of suspected duplicate pairs |
| Coverage | Coverage | Bottom 20 papers by citation count |
| Provenance | Provenance | Detection method × generation breakdown |

## Output

The audit produces a single self-contained HTML report (`output/audit_report.html`).
Each section has a summary block followed by a collapsible sample. No notes
fields, no action prompts — diagnostic only. Printable via browser. Accessible
without special software.

## Relationship to the corrections system

The audit report is read-only. If a reviewer identifies a problem — a wrong
author name, a duplicate pair that should be merged, a garbage entry — they
record the fix in `corrections.yaml`. The corrections system applies it on
the next `--postprocess` run. See `docs/methods/corrections-system.md`.

## Relationship to GROBID's published benchmarks

GROBID's developers publish benchmarks for reference extraction (~0.87 F1
against a PubMed Central holdout set). These benchmarks cannot be assumed to
transfer to the BibVik corpus. The PubMed Central corpus is biomedical
literature in English with standardised layouts and consistent citation styles.
The BibVik corpus spans multiple languages (Norwegian, Swedish, Danish, German,
French, English), diverse publication types, and a significant proportion of
older scanned material. This domain gap is a primary motivation for independent
manual validation.

## Approaches considered and not adopted

**Ground-truth mini-corpus.** The most principled approach would be to select
5–10 papers whose complete reference lists are independently known and compare
the pipeline's output against this ground truth. This was considered and set
aside: constructing verified ground truth for multilingual humanities literature
is time-consuming, the resulting sample would be too small to be statistically
meaningful, and the qualitative audit approach is sufficient to identify
systematic failure modes at this stage of the project.

**Automated field-level accuracy assessment.** For CrossRef-resolved entries,
the CrossRef metadata provides a second source that could be compared
automatically against GROBID's extraction to detect mismatches. This was
considered but not implemented as an automated check: the CrossRef match itself
may be wrong, so CrossRef metadata cannot serve as ground truth. Human review
of the CrossRef-resolved sample serves this purpose instead.

**Corrective strata.** Earlier versions of the audit included strata targeting
specific error classes for correction: near-duplicate candidates, GROBID ID as
author, oversized titles, missing given names, citekey collisions. These are
now handled by the corrections system, which flags them automatically as draft
corrections. They have been removed from the audit report to keep its purpose
unambiguously diagnostic.

## Limitations

This method cannot produce a precision/recall estimate for the pipeline without
a full ground-truth corpus. The audit supports qualitative assessment —
identifying systematic failure modes and building researcher confidence in the
graph — rather than quantitative performance measurement.

Language-stratified accuracy sampling requires language detection to be
implemented in the pipeline. Until that is available, the accuracy section
samples from all non-English papers without per-language breakdown.