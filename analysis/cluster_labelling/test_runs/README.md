# Reconstructed test result files — what each one is

All files below were reconstructed as real CSVs from text pasted into
the chat during the session, since the actual files only ever existed
on the local machine and were never preserved as files beyond what got
pasted into the conversation. Transcribed from pasted text, not
re-derived — treat as faithful copies of what was shown, not as
independently regenerated output.

## test0_ORIGINAL_percluster_25clusters_22accepted.csv

The genuine first full pipeline run of the whole session — predates
every other file here. Original per-cluster mode (before the rebuild
to batch mode): one LLM call per cluster, run across `qwen3.5:35b` and
`qwen3:8b` at multiple temperature/seed combinations, auto-accepted
only if every run agreed. Scoped to 25 clusters. Result: 22 of 25
accepted (stable), 3 flagged unstable and excluded — clusters 22, 49,
and 292 are absent from this file for that reason (22's identity
wasn't retained elsewhere; 49 and 292 both reappear labeled
successfully in later batch-mode tests). This is the run where three
separate clusters (8, 52, 216) each converged on "Viking Age
Archaeology[...]" — the concrete duplicate-label problem that
motivated moving to batch mode in the first place.

## test1 — MISSING, not reconstructed

The first labeling test using the REBUILT batch-mode pipeline (after
per-cluster mode above was abandoned for the duplicate-label problem):
5 clusters (101, 14, 67, 86, 171), plain batch mode, centroid
sampling, no critique, no few-shot. Its full CSV (with descriptions)
was never pasted into the chat in one complete block the way every
later test was — only its label text survives, embedded in later
comparison tables built during the session. Not reconstructed as a
file because a labels-only version without descriptions would be a
materially incomplete copy, not a faithful one. If needed, the label
text alone can be pulled from the "Test 1 (centroid)" column of later
comparison tables in the chat history.

## test2_batch_critique_centroid.csv

5 clusters (101, 14, 67, 86, 171). Batch mode + `--critique` second
pass. Centroid sampling. Result: 0 of 5 labels revised by the critique
pass — inconclusive, the set was already clean going in.

## test3_hierarchical_centroid.csv

Same 5 clusters, split into two sub-batches (3+2) to force
hierarchical mode's cross-batch reconciliation pass to actually run.
Centroid sampling. Result: 0 collisions found or fixed — inconclusive,
same reason as test2.

## test4_batch_diverse.csv

Same 5 clusters. Plain batch mode, no critique, no outlier flagging —
first test using `--sampling diverse` instead of centroid. This is
the run that first showed diverse sampling surfacing real content
centroid sampling had been missing (cluster 171's label shifted from
migration-focused to explicitly naming ethics/identity content).

## test5_batch_diverse_outlier.csv

Same 5 clusters, diverse sampling, plus the (later-removed) per-title
outlier-flagging mechanism, v2 (nearest-other-selected-title distance,
not centroid distance). Descriptions explicitly name which titles were
flagged as possibly not fitting.

## test6_batch_diverse_25clusters.csv

First full-scale test: 25 clusters (the corpus's largest, by member
count), diverse sampling, plain batch mode — the "clean five-step
design" after the full pipeline rebuild that stripped out critique,
hierarchical, outlier-flagging, and split-detection. No secondary_label
column yet (built before dual-label support existed).

## test7_batch_25clusters_duallabel_v1_empty.csv

Same 25 clusters, same settings as test6, but with the FIRST version
of the dual-label prompt instruction added. Every secondary_label/
secondary_description cell is empty — this version of the instruction
produced zero dual labels, including on clusters where the model's own
description text showed it had noticed a second theme but folded it
into a subordinate clause instead of using the field. This result is
what triggered rewriting the instruction.

## test8_batch_10clusters_duallabel_v2_fixed.csv

10 clusters (the corpus's largest 10, a superset overlap with the
5-cluster tests), same batch settings, but with the REWRITTEN
dual-label instruction that explicitly names the "but/while/also
clause" failure pattern and disallows it as a substitute for using the
field. Result: 9 of 10 clusters received a secondary label. Four
(101, 86, 171, 292) were checked directly against real titles and
confirmed accurate. Four (11, 39, 43, 49) were not independently
re-verified with the same rigor. One (14) correctly stayed single —
independently confirmed clean on every prior test all session.

## What's still missing

- **test1** (see above) — label text only, no descriptions, not
  reconstructed as a file.
- **representatives.csv** — the actual sampled-title INPUT file (what
  the LLM was shown before labeling). One full 25-cluster version of
  this was pasted in the chat (the diverse-sampled dump used to
  produce test6/test7), but has not been reconstructed as a file yet.
  Needed to independently re-verify any label against its real titles
  without relying on chat-pasted text.
- **node_labels.csv** — never pasted in full at any point; only
  summary line counts ("expanded to N node(s)") were shown.
