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

Before applying, the correction's direction is checked against `keep` and
`discard`'s `generation` (P/F1/F2/F3 — see [Citekey generation](deduplication-normalisation.md)
for how generation is assigned). If `discard` is more central than `keep`
(e.g. discarding an F1 paper to keep an F2 stub), the merge is refused and
logged rather than applied — this class of mistake shipped once already: a
confirmed correction discarded a real F1 entry with several citers in favor
of a weaker F2 duplicate, and was only caught afterward by noticing aggregate
generation and edge counts had shifted unexpectedly after a `--postprocess`
run. Add `override: true` to the correction to apply it anyway, for the rare
case where generation alone doesn't reflect which side should actually
survive.

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

If a `set` correction changes the `author` field and the entry's citekey no
longer matches the corrected author, the citekey is automatically regenerated
and every `cited_by` reference to it elsewhere in the bibliography is remapped
to the new citekey. See [Citekey generation](deduplication-normalisation.md)
for the exact matching rule.

### split

An entry was incorrectly merged and needs to be separated back into two or
more entries — for example, two different people sharing a family name and
initials, wrongly combined by the duplicate-detection heuristics. The
original entry is tombstoned (`_deleted: true`, `_split_into: [new_citekey, ...]`)
rather than removed, consistent with merge and delete.

```yaml
- action: split
  citekey: jorgensennorgard1997
  into:
    - citekey: jorgensen1997
      author:
        - family: Jørgensen
          given: Lars
      citers: [baastrup2014, hilberg2018]
    - citekey: norgardjorgensen1997
      author:
        - family: Nørgård Jørgensen
          given: A.
      citers: [iversen2015, lemm2014]
  note: "Two different works by different authors were wrongly merged under
         one citekey. Citing papers checked individually to determine which
         citations belong to which author."
```

`into[].citers` is required on every item and must be listed explicitly — the
pipeline cannot infer which citing paper meant which person, this requires a
reviewer to have actually checked the citing papers' reference-list context.
The union of every `citers` list across all `into` items must exactly equal
the original entry's `cited_by` list. Any citekey that's missing, duplicated,
or doesn't belong causes the entire split to be refused (logged as an error,
no partial changes made) rather than silently dropping or double-counting a
citation edge. Fields other than `citekey` and `citers` on an `into` item
(e.g. `author`, `title`) are set on the target entry, whether it's newly
created or already exists.

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
| Cross-script pairs (title overlap insufficient, or both titleless) | `_cross_script_duplicate_candidate` | `merge` |
| Same author+year, both entries titleless | `_titleless_duplicate_candidate` | `merge` (confidence 0.3 — weakest evidence tier; includes both sides' `_raw_citation` since no title is available to compare) |
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
They retain `_deleted: true` (`_merged_into: citekey` for merges,
`_split_into: [citekey, ...]` for splits). This preserves citation link
history.

Export (`exporter.py`) treats tombstoned entries differently depending on
why they were tombstoned. Entries tombstoned via `merge` are fully excluded
from the exported graph — their outbound citation relationships were already
remapped onto the surviving `keep` entry during the merge, so nothing is
lost by dropping the tombstone itself. Entries tombstoned via `delete` are
excluded as a *node* — the record's own metadata is being discarded as
unreliable — but if the entry appears as a citer anywhere in the corpus
(i.e. it genuinely cited other works, even though its own bibliographic
record is garbage), it is retained as a flagged "ghost" node so those
outbound edges are not silently lost. A `delete` correction should be
understood as "this record's own metadata cannot be trusted," not "this
paper's citations to other works never happened" — those are two different
claims, and only the exporter's ghost-node handling keeps them from being
conflated.