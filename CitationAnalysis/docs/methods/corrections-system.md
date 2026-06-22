# Corrections system

The bibliography contains two classes of known errors that automated processing
cannot resolve: systematic failures discovered through pipeline investigation
(OCR corruption, GROBID parsing failures, same-work collisions), and errors
identified by human reviewers through the audit.

Both classes are handled through the same mechanism: `corrections.yaml`.

---

## File

**`corrections.yaml`** — the single file for all manual curation decisions.
Lives in the project root, committed to the repo. Applied automatically by
`--postprocess` as the first pass, before any LLM passes. Notes required on
all confirmed entries.

Confirmed corrections and pipeline-generated draft candidates live in the same
file. Draft candidates are appended by `--postprocess` after all passes, marked
with `_draft: true`. The researcher reviews them in place, removes `_draft: true`
and fills in the note to confirm, or deletes the entry to reject.

---

## Actions

### merge

Two entries are the same work. `cited_by` from `discard` is merged into `keep`.
`discard` is tombstoned with `_deleted: true` and `_merged_into: keep`.

```yaml
- action: merge
  keep: widerstrom2004
  discard: norderang2004
  note: "Same work. Author order differs between source papers causing different
         citekeys. widerstrom2004 has volume field and cleaner author parsing."
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

After `--postprocess`, open `corrections.yaml` and scroll to the draft entries
(marked `_draft: true`). Each draft has additional context fields:

- `_source` — what generated it (`near_duplicate`, `cross_script`,
  `noauthor_recovery`, `ocr_candidate`)
- `_confidence` — pipeline's confidence (0.0–1.0)
- `_keep_title` / `_discard_title` — titles for context (merge candidates)
- `_raw_citation` — original raw string (set/delete candidates)

To confirm a draft:
1. Remove `_draft: true` and all `_`-prefixed context keys
2. Fill in the `note` field

To reject: delete the entry.

---

## Sources of draft corrections

| Source | Flag in bibliography | Draft action |
|---|---|---|
| Near-duplicate pairs (LLM inconclusive) | `_near_duplicate_candidate` | `merge` |
| Cross-script pairs (title overlap insufficient) | `_cross_script_duplicate_candidate` | `merge` |
| Failed NOAUTHOR author recovery | `_author_recovery_failed` | `set` (author field, value blank) |
| Alternate OCR candidate still unresolved | `_ocr_candidate` | `delete` |

---

## Known corrections

OCR merge pairs verified against the bibliography and recorded in
`corrections.yaml`:

| Discard | Keep | Reason |
|---|---|---|
| `rsnes1966` | `orsnes1966` | OCR dropped leading O from Orsnes |
| `wamets1985` | `wamers1985` | OCR misread Wamers as Wamets |
| `norderang2004` | `widerstrom2004` | Same work cited with different author order |

---

## Tombstoning

Deleted and merged-away entries are never removed from `bibliography.json`.
They retain `_deleted: true` (and `_merged_into: citekey` for merges).
This preserves citation link history. Exporters and graph analysis skip
tombstoned entries.