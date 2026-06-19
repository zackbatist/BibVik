# Corrections system

The bibliography contains two classes of known errors that automated processing
cannot resolve: systematic failures discovered through pipeline investigation
(OCR corruption, GROBID parsing failures, same-work collisions), and errors
identified by human reviewers through the audit.

Both classes are handled through the same mechanism: `corrections.yaml`.

---

## Files

**`corrections.yaml`** — confirmed corrections. Lives in the project root,
committed to the repo. Applied automatically by `--postprocess` as the first
pass, before any LLM passes. Human-edited. Every entry requires a `note`
field documenting the basis for the decision.

**`corrections_draft.yaml`** — pipeline-generated candidates. Written by
`--postprocess` after all passes, based on flags left in the bibliography by
deduplication and postprocessing. Reviewed by the researcher; accepted entries
are promoted to `corrections.yaml`. Not committed to the repo.

---

## Actions

### merge

Two entries are the same work. `cited_by` from `discard` is merged into `keep`.
`discard` is tombstoned with `_deleted: true` and `_merged_into: keep`.

```yaml
- action: merge
  keep: widerstrom2004
  discard: norderang2004
  note: "Same work. Widerström is the correct author; norderang2004 is a
         GROBID parsing error on the same reference in Cannell 2016."
```

### delete

An entry is garbage — not a real bibliographic reference. Tombstoned with
`_deleted: true`.

```yaml
- action: delete
  citekey: NOAUTHOR771
  note: "Duplicate of pentz2009a. Author name absorbed into title field
         by GROBID due to OCR corruption in source PDF."
```

### set

A field value is wrong. Overwrites the field. Applied after enrichment, so
CrossRef cannot overwrite a manually corrected value.

```yaml
- action: set
  citekey: sindbak2001a
  field: author
  value:
    - family: Sindbæk
      given: Søren Michael
  note: "GROBID transposed given and family name. Verified against CrossRef
         DOI 10.1017/S0003598X00094734."
```

---

## Workflow

### Confirmed corrections

Edit `corrections.yaml` directly. Run `--postprocess` to apply.

### Pipeline-generated candidates

After `--postprocess`, open `corrections_draft.yaml`. Each entry has:

- `_source` — what generated it (`near_duplicate`, `cross_script`,
  `noauthor_recovery`, `ocr_candidate`)
- `_confidence` — pipeline's confidence (0.0–1.0)
- `_keep_title` / `_discard_title` — titles for context (merge candidates)
- `_raw_citation` — original raw string (set/delete candidates)

To accept a candidate:
1. Copy the entry to `corrections.yaml`
2. Remove all `_`-prefixed keys
3. Fill in the `note` field
4. Delete it from `corrections_draft.yaml`

To reject: delete it from `corrections_draft.yaml` or leave it (it will be
regenerated on the next run if the flag is still present in the bibliography).

---

## Sources of draft corrections

| Source | Flag in bibliography | Draft action |
|---|---|---|
| Near-duplicate pairs (LLM inconclusive) | `_near_duplicate_candidate` | `merge` |
| Cross-script pairs (title overlap insufficient) | `_cross_script_duplicate_candidate` | `merge` |
| Failed NOAUTHOR author recovery | `_author_recovery_failed` | `set` (author field, value blank) |
| Alternate OCR candidate still unresolved | `_ocr_candidate` | `delete` |

---

## Migration from `_OCR_MERGE_PAIRS`

The hardcoded `_OCR_MERGE_PAIRS` dict in `postprocess.py` should be deleted.
Its entries move to `corrections.yaml` as `merge` actions. Known pairs:

- `wamers1994` / `wamets1994` — OCR corruption in Baastrup 2014
- `orsnes1966` / `rsnes1966` — OCR corruption

---

## Tombstoning

Deleted and merged-away entries are never removed from `bibliography.json`.
They retain `_deleted: true` (and `_merged_into: citekey` for merges).
This preserves citation link history. Exporters and graph analysis skip
tombstoned entries.
