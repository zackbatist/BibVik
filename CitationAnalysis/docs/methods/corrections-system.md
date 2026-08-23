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

---

## Case study: resolving the concatenation-flagged backlog, August 2026

Follow-on work to the case study above, in the same session window. The
manual verification pass had left 128 entries flagged with a `[CORRUPTED: ...]`
placeholder in their `title` field — diagnosed as genuine extraction failures
(GROBID or the LLM re-parse merging two or more distinct bibliography entries
into one) but not repaired, since the original garbled text needed for a
proper `split` had been overwritten by the diagnostic flag itself.

### The key finding that made this tractable

The garbled text was not actually lost. `_raw_citation` — the field GROBID
populates with the unparsed reference-list string — was never touched by the
earlier flagging pass, which only overwrote `title`. Checking confirmed all
128 flagged entries still carried their full original `_raw_citation` text.
This meant every one of the 128 could be resolved directly from data already
in `bibliography.json`, without needing the source PDFs at all. (The two
entries that genuinely required PDF text — `aust2002` and `hb2006`, handled
in the case study above — turned out to be the exception: their
`_source_footnote` field had captured the *wrong* span of the PDF, which
`_raw_citation` cannot fix since it comes from GROBID's bibliography
extraction, not the footnote-detection pass.)

### Method

For each flagged entry: read `_raw_citation`, identify how many distinct real
citations it actually contains, and resolve accordingly:

- **Single real citation, garbled or with an incidental trailing date** (a
  cited reprint year, an excavation date range) — corrected via `set` on
  `title` and any other misparsed fields. Not a concatenation error at all;
  the original Test 3 flag was a false positive.
- **Two or more genuinely distinct citations merged into one entry** —
  resolved via `split`, with the surviving entry's citers attributed to
  whichever constituent work they most plausibly belong to. Where the
  original entry had a citer and multiple constituent works were plausible
  matches, attribution used the closest year/topic fit and was noted as an
  inference, not a verified fact — the note on each such correction says so
  explicitly, and is the record of what's actually known versus assumed.
- **No confirmable single work at all** — bare cross-reference shorthand
  (e.g. `Nilsson 1995.` with nothing else), or a fragment of an unstructured
  site-gazetteer table misidentified as a citation, or citing-paper analytical
  prose with several citations embedded as asides — re-flagged with a
  specific `[CORRUPTED: ...]` description of what the content actually is,
  since these cannot be resolved into a bibliography entry without the source
  PDF and are a structurally different problem from concatenation.

Entries with no `cited_by` and multiple real citations packed into one
`_raw_citation` (common in the `Puškina et al 2017` and `Baastrup 2014`
source PDFs, which show the same severe extraction failure repeated across
many consecutive bibliography entries) were resolved by recovering the
primary/first citation only, with the note explicit that the remaining real
works in the raw text were dropped rather than reconstructed — since with no
citer requiring attribution, the marginal value of reconstructing every
trailing fragment did not justify the risk of a wrong guess on OCR-damaged
names.

### Outcome

All 128 entries resolved: roughly 95 recovered to correct records (single
citations or proper splits), roughly 30 confirmed as duplicates of an
already-existing entry and merged, and 5 re-flagged as genuinely
unrecoverable without the source PDF (`godwin1962`, `nielsen1943`,
`kivikoski1905`, `segschneider1982`, `fagerlund2005` — matching non-citation
content, not concatenation).

### Bugs found and fixed along the way

**`split` never wrote `citekey` onto its own target entries.** The action's
per-item field loop explicitly excluded `citekey` from the fields copied onto
a newly created entry (`if field in ("citekey", "citers"): continue`),
relying solely on `item_citekey` as the bibliography dict key. New entries
therefore inherited the *original* (pre-split) entry's stale `citekey` field
internally, even though they were correctly stored under the new key. Found
when a split target's `citekey` field read the old value; confirmed via
direct reading of `bibvik/corrections.py`. Fixed with a one-line addition
(`target["citekey"] = item_citekey`) immediately before the field-copy loop.
Patched locally, committed, and pulled to the server mid-session; entries
created before the patch (e.g. the first `goody1977`/`goody1986` split) needed
a follow-up `set` correction to fix the stale field on the already-created
entry, since the patch only affects entries created after it takes effect.

**`set` silently no-ops when `value` is `None`/`null`.** Discovered while
trying to clear a stray garbled field (a fragment bled in from an adjacent
citation in the same raw text) via `set ... value: None`. The field never
changed. Confirmed the correction was in fact being read (a later log showed
`WARNING: ... missing value` for the same correction once enough runs had
passed for that log line to surface) — `None` is indistinguishable from "no
value provided" to the `set` handler, so the correction is silently treated
as a no-op rather than as "clear this field." **The working alternative,
confirmed by direct test: use an empty string (`''`) for scalar fields, or an
empty list (`[]`) for list-type fields (e.g. `editor`).** This is now the
only confirmed-working way to clear a field via a `set` correction in this
pipeline; `None`/`null` should not be used for this purpose.

**A `split` whose `into` item reuses the same citekey as the original entry
can silently fail to tombstone the original**, observed on two entries
(`kleemann1939`, `stankus1995`) where several sibling `into` items were
created correctly but the original entry was never marked `_deleted` and its
title was never updated — it simply sat unchanged alongside its new siblings.
The retry that fixed both used a distinct target citekey for the item that
would otherwise have collided with the original (`kleemann1939` →
`kleemann1939b`), which resolved cleanly. The exact mechanism was not traced
in `corrections.py` (unlike the two bugs above, this one was worked around
rather than root-caused), so it remains a known trap: **avoid giving any
`into` item the same citekey as the entry being split**, even when that item
is meant to represent the "primary" continuation of the original work.

### Process notes

- Every correction in this pass was verified against the live
  `bibliography.json` after applying, not assumed from the `corrections.yaml`
  diff or the `--postprocess` summary line alone.
- `corrections.yaml` accumulates every correction ever written, including
  ones that already succeeded — re-running `--postprocess` replays the entire
  file every time, so already-succeeded `split`/`set` corrections increasingly
  show up as `WARNING: citekey not found` (harmless: the old key no longer
  exists because the correction already renamed or tombstoned it) or, for
  `split` specifically, `ERROR: citer accounting mismatch` (also harmless in
  this case: the original's `cited_by` is now empty because the split already
  moved it, so replaying the same correction against the now-empty list
  looks like a mismatch and is correctly refused). Both are expected, not
  signs of a new problem — but the volume of accumulated noise made it worth
  periodically removing confirmed-succeeded corrections from `corrections.yaml`
  (checking each target entry's live `title` matches what the correction
  specified before removing it) purely to keep the log readable session to
  session. This is a good practice going forward for any long correction
  session, not a one-off cleanup for this one.
- A citekey rename triggered by a `set` correction to `author` happens
  *immediately*, before any later `set` corrections in the same submitted
  batch have a chance to apply under the old key. This was already known from
  the case study above, but this pass surfaced its sharper edge: fields set
  *after* the renaming correction in the same batch don't just fail to
  apply — they can be silently populated with stale or wrong data bled in
  from an adjacent, unrelated citation in the same raw text block (observed
  on `baastrup1990`, `mulkeen1995`, `lobbedey1992`, and others), because the
  entry that ends up under the new key is a copy of whatever the original
  entry's fields happened to be at that point, not a blank slate. The
  practical rule: after any batch containing an `author` correction, check
  every field intended to land on that entry, not just whether the batch
  applied without error — a clean apply does not guarantee every intended
  field actually reached the right entry.

---

## Incident: two F1 source papers missing their own bibliography node

Found while investigating why a Girvan-Newman run loaded far fewer nodes
(12,798) than the corrected bibliography's active entry count (14,328).

### Diagnosis

1,467 `F2`-generation entries had `cited_by: []` — no inbound edge at all,
despite `generation: F2` meaning by definition "extracted from an F1 paper's
reference list," which should guarantee at least one citer (the F1 paper
itself). All 1,467 traced to exactly two `_source_pdf` values: `Puškina et
al 2017 - Der Archäologische Komplex von Gnezdovo.pdf` (1,053 entries) and
`Søvsø 2014 - Ansgars Kirche in Ribe.pdf` (417 entries).

The cause was not a citation-detection miss (the more common, expected
reason an F2 entry lacks a citer — see the case study above). Both papers
already existed as `F1` entries, correctly `cited_by: ['lund2021']` (the
seed paper), with correctly-preserved `_raw_citation` text naming the real
titles. The problem was narrower: **both entries' `title` field had been
misparsed** — `puskina2017`'s title was overwritten with a fragment of the
*editor's* book title from the surrounding text ("Jahrhundert: Ein
archäologisches Panorama" instead of "Der archäologische Komplex von
Gnezdovo"), and `sovso2014`'s title had been swapped for an entirely
different, real Søvsø work ("Om dateringen af Ribe runehjerneskallen"
instead of "Ansgars Kirche in Ribe"). Because the extraction pipeline
apparently matches an F1 paper's reference-list children to their parent
by title (or a process downstream of title), these 1,470 F2 children were
extracted and stored correctly, but never got linked back to their parent's
`cited_by` because the parent's own title didn't match what the linking step
was looking for.

### Fix

Two `set` corrections fixed the parent titles (`puskina2017`,
`sovso2014` — no new citekey created; both already existed with correct
`generation` and `cited_by`, only `title` was wrong). Then a script wrote
one `set` correction per orphaned child, repointing `cited_by` to the
corrected parent citekey — 1,053 to `puskina2017`, 417 (416 expected from
the initial scan, 417 found live — the live count is authoritative) to
`sovso2014`. Total: 1,472 corrections in one batch, applied via
`--postprocess` in a single run. Edge count rose from 22,886 to 24,356.

Verified after applying: both parent titles correct, and the F2-orphan count
dropped from 1,467 to 60 — the residual 60 are the ordinary, scattered
citation-detection misses described in the case study above (each traces to
a different F1 parent, a handful per paper), not another instance of this
same missing-title-link problem.

### Standing lesson

An F2 entry's `cited_by` being empty has (at least) two structurally
different causes that look identical from the entry's own fields alone:
ordinary citation-detection miss (the common case — the F1 paper's in-text
citation to this specific work wasn't matched), or a broken link to the
parent itself (rare, but total when it happens — every child of that parent
loses its citer at once). The second is diagnosable by checking whether many
orphaned F2 entries cluster under one or two `_source_pdf` values rather
than being scattered thinly across many — a large cluster under a single
source PDF is the signature to check the parent F1 entry's own correctness
first, before assuming each child needs individual citation-detection work.

### Follow-up: the remaining 60 orphans, and the true final count

After the `puskina2017`/`sovso2014` fix, 60 F2 entries remained orphaned,
scattered across many different F1 parents (unlike the 1,467, which
concentrated in two). These were mostly the deliberate `citers: []` stub
entries created throughout the concatenation-backlog case study above —
real, correctly split-out works that had no citer at the time because no
evidence supported attributing one. Since each stub's `_source_pdf` records
which F1 paper's raw citation it was originally split out of, the F1 parent
that generated the original garbled entry was almost always the correct
citer to add — this is not a new citation being invented, it is the citer
relationship the original split correction should have carried but the
stub's `citers: []` had deferred.

Resolution: for each orphan, parse the author-surname and year out of its
`_source_pdf` filename, then match against F1 entries by exact surname and
year. 53 resolved unambiguously (single exact match). Of the remaining 7,
5 resolved with individual review — two multi-candidate cases
(`price2006c`, `zoega2004b`) where the correct match was confirmed by
checking which candidate's own `_source_pdf` matched the actual uploaded
PDF, and three (`radins1998`, `radins2001b`, `tonisson1999`) that shared one
F1 parent (`Mägi 2016`) findable only by an exact-title search of
`_raw_citation`, since the parent's own `year` field (2015) didn't match the
source PDF's filename year (2016) — an edited-volume original-vs-publication
date mismatch, not an error.

**One entry remains genuinely unresolved**: `holmqvist1977` (source
`Åhfeldt 2015 - Picture-Stone Workshops on Viking Age Gotland`). Checked
two ways — a title-fragment search against every F1 entry's `_raw_citation`,
and a direct `_source_pdf` equality check across all generations — and
neither found any trace of this paper as an F1 node under any citekey.
`mannerfelt1936` (source `Sanmark and Semple 2008`), initially believed to
be in the same position, turned out not to be: the direct `_source_pdf`
equality check found `sanmarknd`, an already-correct F1 entry with a
non-standard citekey (`nd` in place of a year) that the earlier
surname-plus-year matching heuristic had no way to find, since it filtered
on an exact year match the citekey didn't carry. That is the general lesson
of this whole follow-up: `_source_pdf` equality is the reliable check;
citekey-pattern matching is a shortcut that can miss real matches when a
citekey doesn't follow the usual convention.

Fixing `holmqvist1977` properly would require creating a new F1 entry and
asserting `cited_by: ['lund2021']` without a directly parsed citation to
support it — the same inference-risk tradeoff considered and avoided for
`puskina2017`/`sovso2014`. Left flagged rather than forced.

**Final count: of the original 1,467 orphaned F2 entries found via the GN
node-count mismatch, 1,466 are now correctly linked to their real citer.
1 remains, with a specific, checked, and named cause — not a mystery.**

---

## Incident: `_merged_into` is not safely `set`-correctable retroactively

Discovered and fixed in a follow-up validation session (same August 2026
window as the case study above), after a fresh re-run of the cross-entry
title-stem clustering check against a fuller export of the corpus surfaced
two new fabricated entries (unrelated to this incident) and prompted a
retroactive audit of the merges described above.

**What was attempted:** two of the manual merges from the case study
(`pentz2009a` → `pentz2009`, `ahfeldt2015` → `kitzlerahfeld2015a`) were found,
on retroactive review, to have discarded the more-central (F1) entry in favor
of a less-central (F2) one — the exact mistake class the `merge` action's
generation-direction check exists to catch. Since both entries were already
tombstoned via manual `delete` rather than the `merge` action, the fix
attempted was a `set` correction adding the missing `_merged_into` field
after the fact, to at least restore correct provenance.

**What went wrong:** the exporter's documented behaviour for `merge`-type
tombstones is to fully exclude the node, on the assumption that "their
outbound citation relationships were already remapped onto the surviving
`keep` entry during the merge" (see *Tombstoning*, above). Setting
`_merged_into` retroactively made the exporter treat these two entries as if
they had gone through that remapping — but they had not, because the
original operation was a manual `set`+`delete`, not the `merge` action, and
no remapping step ever ran. `ahfeldt2015` was itself a real F1 paper with 94
outbound citation edges (94 other bibliography entries listed it as a citer).
All 94 were silently dropped from the exported graph the moment
`_merged_into` was set — confirmed by an edge count drop from 23,005 to
22,910 immediately following the correction.

**Why it was hard to undo:** the natural fix — a further `set` correction
setting `_merged_into` back to `null`/`None` — did not work. Re-running
`--postprocess` after adding the revert correction left the field unchanged
on the live entries, and removing the correction lines from `corrections.yaml`
entirely *also* left the field unchanged. `bibliography.json` is mutated
incrementally across runs (per the pipeline's stateful design, described at
the top of this document), so a field write, once applied, is not undone
merely by removing or reverting the correction that caused it — the value
persists in the live JSON regardless of what `corrections.yaml` currently
says. `_graph_state.json` was checked as a possible second source of truth
and found not to be relevant here: it had not been touched since August 12,
predating this session, confirming `bibliography.json` alone is what
`--postprocess` actually reads and writes on each run in this workflow.

**The actual fix:** direct edit of the live `bibliography.json` (`del
bib[citekey]['_merged_into']`), bypassing `corrections.yaml` entirely, followed
by a normal `--postprocess` run to confirm the field did not reappear and the
edge count recovered. It did — 23,005 edges restored exactly. This is the
only case in this project's history (that this documentation is aware of) 
where a fix required editing `bibliography.json` directly rather than going
through `corrections.yaml`, and it should remain exceptional: direct edits
bypass the note-and-provenance discipline the whole corrections system exists
to enforce, and were only justified here because the corrections-file
mechanism had already been shown not to reach the actual problem.

**Standing conclusion:** `_merged_into` (and by extension, most likely
`_split_into`) should be treated as **write-once, pipeline-internal fields**,
correctly set only by the `merge` and `split` actions performing the actual
edge remapping they describe. They are not safe to set via a plain `set`
correction after the fact, even to fix missing provenance on an
already-tombstoned entry — doing so does not merely annotate the record, it
changes how the exporter treats that node's real outbound edges. If an entry
was merged manually (as happened throughout the case study above) and its
provenance needs correcting, the safe options are: leave `_merged_into` unset
(the entry keeps its current `delete`-tombstone/ghost-node treatment, which is
correct if not fully descriptive), or re-do the operation properly through
the `merge` action from scratch, accepting that this will re-run the
generation-direction check and may require `override: true` if the direction
genuinely needs to go against a stronger entry.