# generate_verification_sample.py

Draws a random sample of bibliography entries for manual expert verification.
Writes a spreadsheet formatted for a non-technical subject-matter expert to
scan in Google Sheets — column order runs identifying info first, then
extracted metadata, then extraction/correction provenance, then a raw
source excerpt to check against, then blank reviewer columns last.

## Usage

```bash
python3 generate_verification_sample.py bibliography.json --n 100 --seed 42
```

Writes `verification_sample_n100_seed42_<date>_<time>.xlsx` and `.csv`
into `verification_samples/` (created automatically next to this
script).

## Options

| Flag | Default | Purpose |
|---|---|---|
| `--n` | 100 | Sample size |
| `--seed` | 42 | Random seed — same seed + same bibliography.json reproduces the same sample |
| `--out-dir` | `verification_samples/` | Where to write output |
| `--basename` | `verification_sample_n<N>_seed<SEED>_<DATE>_<TIME>` | Output filename (no extension) |

Deleted entries (`_deleted: true`) are excluded from the sampling pool by default.

## What's included

All bibliographic fields (title, authors, editors, translators, dates,
publication details), plus extraction/correction provenance (detection
method, confidence, completeness score, enrichment source, correction
notes and history, data-quality flags) so a reviewer can judge how much
to trust an entry. Raw source citation text is included for direct
ground-truth comparison.

Excluded: internal cross-reference pointers with no standalone
verification value (`_grobid_id`, and split/merge/rename linkage fields
that point at *other* citekeys' history rather than this entry's own).

## Output

Both `.xlsx` and `.csv` contain the same data. The `.xlsx` adds header
styling, column widths, a frozen header row, wrapped text, and
highlighted reviewer-input columns — use it for direct handoff. The
`.csv` is plain, for a simpler import or pipeline consistency. Both
import into Google Sheets via File > Import; only the `.xlsx` keeps its
formatting.

## Instructions for the reviewer

For each row, check the extracted fields (Title, Author(s), Year,
Published In, etc.) against the **Raw Citation (as extracted)** column
and, where available, the source PDF named in **Source PDF**. The raw
citation is the closest thing to ground truth here — it's the actual
text the pipeline pulled the fields from, so a mismatch between it and
the structured fields usually means an extraction error, not a
disagreement about the source itself.

Known failure patterns worth watching for, based on errors already
found in this corpus:

- **Wrong reference captured** — the title/author belong to a
  different work than the one actually cited (happens when a source
  paper's bibliography bundles several references close together).
- **OCR corruption** — garbled diacritics, dropped or substituted
  letters, especially in non-English titles and names.
- **Leading artifacts** — a stray year, chapter number, or citekey
  fragment stuck onto the front of a title.
- **Merged or split entries** — one bibliography entry that's actually
  two different works, or two entries that are really the same work
  cited twice under different spellings.
- **Placeholder or garbled text mistaken for a real title/author.**

The **Detection Method**, **Detection Confidence**, **Completeness
Score**, and **Data Quality Flags** columns indicate how the entry was
extracted and whether the pipeline itself already flagged it as
uncertain — treat a flagged or low-confidence entry with extra
scrutiny, but don't skip unflagged entries, since real errors also
occur in confident extractions.

For each row, fill in the two reviewer columns (highlighted yellow in
the `.xlsx`):

- **Reviewer: Correct? (Y/N)** — Y if the extracted fields accurately
  represent the raw citation/source; N otherwise.
- **Reviewer: Notes** — for any N, briefly describe what's wrong (e.g.
  "wrong author," "title truncated," "this is two works merged into
  one"). Notes on Y rows are optional.

No need to verify the source PDF exists or open every one — the raw
citation text is usually sufficient. Consult the source PDF when the
raw citation looks ambiguous or you want to confirm a suspected error.


