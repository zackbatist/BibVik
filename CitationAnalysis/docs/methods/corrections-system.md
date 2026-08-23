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

---

## Case study: manual verification session, August 2026

This section records what was actually done in a single extended manual
review session, and a process gap it surfaced. It is a record of one
session's events, not a revision to the protocol described above — the
documented `merge` and `split` actions remain the correct mechanism and
should be used going forward.

### What was done

A manual, entry-by-entry review was carried out against the `llm_from_context`,
`llm_from_footnote`, and `llm_bib_reparse` resolution-method entries, prompted
by a suspected-fabrication finding. The review was not run through `--audit`
(the audit tool's sampling report was not used as the discovery mechanism for
this pass; see `docs/methods/audit-sampling.md` for its current status). Work
proceeded directly against `bibliography.json`, checking flagged entries
against external sources (web search) and against each other, and writing
corrections straight to `corrections.yaml`.

Five ad hoc checks were run across the active bibliography, in addition to
individual entry review:

1. **Cross-entry title-stem clustering** — grouping entries by first-author
   surname and looking for near-identical title stems across 3+ entries by the
   same author. This caught a genuine fabrication cluster: eight entries under
   an `andersen*` citekey pattern, all traced to one real but unrecoverably
   garbled citation in a single source paper, where the LLM had invented eight
   plausible-looking bibliographic variants (edition numbers that decreased as
   the stated year increased — an impossible sequence for a real book series).
   This is the one technique from the session with a confirmed, otherwise-undetected
   fabrication catch. It is not implemented anywhere in the pipeline or the
   audit tool.
2. **Exact title+year duplicate scan** (full corpus, ~14,300 entries) — found
   15 exact matches; 5 were genuine duplicates (self-inflicted by citekey
   auto-renames during the same session — see below — plus some pre-existing),
   10 were legitimate distinct co-citations.
3. **Near-identical title, different first author** (full corpus) — found 357
   pairs, overwhelmingly OCR/transliteration variants of the same real
   co-authored work (e.g. "Glørstad"/"Glörstad"/"Glgrstad"). Confirmed low-yield
   for fabrication detection specifically; three clear duplicates were merged,
   the remainder left unexamined as a documented backlog rather than worked
   through exhaustively.
4. **High `cited_by` count combined with a malformed author field** — zero
   hits across the full corpus. Read as a genuine, if narrow, confirmation:
   entries independently cited by many different papers never showed
   corrupted authorship in this corpus.
5. **Stated year predates a year named inside the entry's own title** — the
   highest-yield check. 186 entries flagged; all 186 were reviewed individually
   (not resolved by pattern-matching — an attempted regex classifier for
   "true concatenation vs. harmless multi-date citation" was tried and found
   unreliable on manual spot-check, so every entry was read by hand). Result:
   roughly 30 were false positives (single, correctly-formed citations with a
   legitimate second date, e.g. a cited reprint year), and roughly 156 were
   confirmed true concatenation errors — GROBID or the LLM re-parse having
   merged two or more distinct real citations into one entry's title field.
   This concatenation pattern is already named and partly handled elsewhere in
   the pipeline: see `_title_too_long` in `docs/methods/deduplication-normalisation.md`
   and the Method 6 / compound-splitting prompts in `docs/methods/llm-prompts.md`.
   A sixth check (does the stored author's surname appear anywhere in the
   entry's own title, across the whole corpus) was attempted and abandoned:
   it returned 12,818 hits out of ~14,000 active entries and had no
   discriminating power — most real single-author citations simply don't
   contain the author's name in the title. Recorded here so it is not
   re-attempted in this form.

No fabrications were found among the 186 concatenation-flagged entries beyond
the eight already caught by the clustering method — every one traced to a
real underlying citation (or citations), just merged or garbled by extraction,
never invented.

### What was found and corrected

- 12 confirmed fabricated entries deleted (4 individually flagged, 8 from the
  Andersen cluster).
- 9 duplicate pairs merged (3 were pre-existing; 6 were newly created within
  this same session by the citekey auto-rename side effect described below).
- 2 misattributions corrected (a real work attributed to the wrong author).
- 1 spurious self-referential entry deleted (a figure caption from within a
  paper mistakenly extracted as an independent citation of that same paper).
- ~156 entries flagged with a `[CORRUPTED: ...]` marker in the `title` field,
  each noting the specific source PDF and a description of what appears to
  have been merged, for future resolution against the original PDF text.
  These are diagnostic flags, not corrections — the underlying data was not
  altered beyond marking it as unreliable.
- ~30 entries cleaned to their correct, single title, where the extra text
  could be confidently identified and trimmed without needing the source PDF.

### Process gap: merges were not done via the `merge` action

Every duplicate pair found in this session was resolved by hand — a `set`
correction to move `cited_by` from the discarded entry onto the kept entry,
followed by a separate `delete` correction — rather than via the documented
`merge` action described above. This was not a deliberate choice; it reflects
that the reviewer had not read this document before the session and
reconstructed an ad hoc equivalent instead.

**Practical consequence:** in each case, `cited_by` counts were compared by
hand before deciding which entry to keep, which is the same comparison the
`merge` action's generation-direction check performs automatically. All 9
merges in this session kept the entry with the larger or equal `cited_by`
list. No merge is currently believed to have discarded the more-central
entry.

**What was not done, and is the actual gap:** the generation-direction check
exists because this class of mistake shipped once before, and — per the
existing text above — was only caught *after* the fact, by noticing that
aggregate node and edge counts had shifted unexpectedly following a
`--postprocess` run. This session's per-merge manual check is a reasonable
substitute for the automated check, but it was never verified against that
same aggregate-level signal. No end-of-session review of total node/edge
trajectory across all corrections was performed to confirm the pattern looked
right in aggregate, the way the original incident was actually detected. This
has not been done retroactively as of this writing.

**Recommendation:** future sessions doing this kind of duplicate cleanup
should use the `merge` action directly rather than reconstructing it by hand,
both for the safety check and so the correction is legible in `corrections.yaml`
as what it actually is. If reviewing this session's corrections, treat the
9 merges as unverified against the aggregate-count method until that check is
actually run.

### Process gap: `split` was not used for concatenated entries

Several of the ~156 flagged concatenation entries had enough information
recovered during review to identify both underlying citations and, in some
cases, which citing papers belonged to which. These would have been
candidates for the `split` action rather than a `[CORRUPTED]` flag. `split`
was not used anywhere in this session — again because the reviewer had not
read this document beforehand. The flagged entries remain in their pre-split
state; resolving them via `split` (with `citers` correctly attributed per the
target entry, as the action requires) remains as future work rather than
something this session attempted.

### Citekey auto-rename interaction (observed, not new)

Not a gap in this session specifically, but worth restating in this context
since it was hit repeatedly: every `set` correction to an entry's `author`
field can trigger the citekey regeneration described above. Several times in
this session, an author-field correction landed under a citekey that already
existed for an unrelated, correct entry — producing a genuine new duplicate
that then had to be found and merged (again, by hand, per the gap above)
before the correction set could be considered complete. This is expected
pipeline behaviour, not a bug, but it means any batch of `set` corrections
touching `author` should be followed by a check for newly-collided citekeys
before being considered done — this session did that check after every batch,
but it was a manual step, not something `--postprocess` surfaces on its own
beyond the "citekey not found" warnings for the *old* key.