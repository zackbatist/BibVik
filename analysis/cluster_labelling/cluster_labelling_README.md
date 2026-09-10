# Cluster labelling — standalone

Not part of the Quarto render chain, same convention as `gn_analysis/`:
embedding the full corpus and running an LLM over every cluster is slow
and has its own Python dependency (`sentence-transformers`) that the
main `bibvik` package does not carry.

Independent of any specific partition. Takes any `node_id,community_id`
CSV as input — Girvan-Newman's `communities.csv`, or Louvain/Leiden
membership exported from `02_network_structure.qmd`. Nothing here cares
which method produced the grouping being described.

Independent of the annotation codebook. The labeling prompt sees only
representative titles; it is never shown the codebook, its categories,
or any example labels drawn from it. Comparison against the codebook
happens later, in `03_joined_descriptions.qmd` — a separate, later step
this pipeline does not touch.

## Pipeline steps

1. **`embed_titles.py`** — embed every active entry's title with a
   multilingual sentence-embedding model. Titles only, no abstract
   text (abstracts don't exist for the vast majority of this corpus),
   so F1 and F2 entries are on equal footing. Word-frequency topic
   modeling (LDA/NMF) was ruled out for this corpus specifically:
   titles average ~11 words and the corpus is genuinely multilingual
   (Danish, Norwegian, Swedish, German, English, Russian, Polish,
   Icelandic, Ukrainian at minimum), so two titles describing the same
   thing in different languages share no literal words a
   frequency-based method could use. Meaning-based embeddings sidestep
   this by placing semantically similar text near each other regardless
   of language.
2. **`select_representatives.py`** — for each cluster, pick a sample of
   member titles to actually show the LLM, since sending every member's
   title (clusters run into the dozens or hundreds) is unnecessary and
   eventually exceeds any reasonable prompt size.
3. **`label_clusters.py`** — the LLM labeling step: every cluster in
   one call.

## Sampling strategies (`--sampling`)

**centroid** (default): rank member titles by cosine distance to the
cluster's centroid, keep the closest `--top-n`. Gives the LLM the
titles most typical of the cluster.

**diverse**: farthest-point sampling instead — start from the
centroid-closest title, then repeatedly add whichever remaining title
is farthest (by minimum distance) from everything already picked.
Rationale: centroid sampling can clump the whole sample near the
cluster's average, missing a real sub-theme that sits off to one side
of the cluster's internal spread. A cluster containing two genuinely
different sub-topics might have its centroid sample drawn entirely
from the larger sub-topic. Diverse sampling trades "most typical" for
"covers more of the actual range."

Neither `--sampling` choice nor `--top-n` (15, default) has been
tuned against alternatives on this corpus — both are starting points,
not validated optima.

## Why the labeling call handles every cluster at once, not one at a time

A model asked to label one cluster per request has no visibility into
any other cluster — it cannot know what it named a different cluster,
so it has no way to avoid two unrelated clusters independently landing
on the same or a near-identical label. This is a structural blind
spot, not a prompting failure: the information needed to avoid the
collision (what has already been said about other clusters) simply
isn't available inside an isolated, one-cluster-at-a-time call.

Sending every cluster's representative titles into a single request,
and asking for every label back in one response, gives the model that
missing visibility — it can see it already used a phrase two clusters
earlier and avoid repeating it, the same way a person proofreading a
finished list would naturally catch and avoid a repeated line.

Trade-off: one call means one shot — no repeated runs to compare
against each other, no stability check. This pipeline does not include
one. It is a single automated pass with no built-in verification step
of any kind — no per-title flagging, no per-cluster confidence score,
no human review gate. That is a deliberate simplification, not an
oversight: added verification mechanisms (multi-run stability checks,
outlier detection on individual titles, automated cluster-splitting
detection, a second LLM pass critiquing the first) were tried during
development and, when checked against real cluster content, were found
to be at least as likely to introduce a new error as catch a real one.
None of them produced a clear, checked-and-confirmed improvement over
the simple single-pass version, so none are included here.

## Why the prompt names the corpus's shared baseline explicitly

A label can be technically true of a cluster and still useless if it's
also true of nearly every other cluster. On this corpus specifically,
anything invoking "Viking Age Archaeology" or "medieval Scandinavia"
in isolation is true of almost every cluster and therefore
distinguishes nothing. The fix is not clever wording — it's stating
the shared baseline directly in the prompt and instructing the model
that a label reducible to that baseline has failed, forcing it to
locate what's actually specific to each individual cluster (a method,
material, site, region, narrower time-slice, or theoretical angle)
rather than defaulting to the safe, always-true description.

Needs a much larger context window than Ollama's server default (a
small default like 4096 tokens will silently truncate a large prompt
rather than erroring) — `--num-ctx` defaults to 32768, sized for
roughly 25 clusters at `--top-n 15`. Raise `--num-ctx` and
`--num-predict` (response token budget, default 8192) when labeling
substantially more clusters than that in one call.

## What was tried, tested, and removed

This pipeline went through several more elaborate designs before
settling on the current shape. Recorded here with actual specifics —
not just "several things were tried" — since a future pass on this
project could easily reinvent one of these and hit the same wall.

**Per-cluster mode with multi-model stability checks.** Original
design: one LLM call per cluster, run across multiple models
(`qwen3.5:35b` and `qwen3:8b`) and multiple temperature/seed
combinations, auto-accepting a label only if every pairwise
combination of runs agreed (cosine similarity of label embeddings
above a threshold). This caught one real, concrete error directly: on
one cluster, all four `qwen3.5:35b` runs converged on "origins and
expansion of the Viking Age" while all four `qwen3:8b` runs converged
on "Christianization and Conversion" — reading the cluster's actual 15
titles confirmed `qwen3.5:35b` was right and `qwen3:8b` had fixated on
a single unusual title (one Orkney/Shetland conversion-themed title
among fifteen migration/expansion titles) and built a confident, wrong
label around it. The stability check correctly refused to auto-accept
either answer. Removed anyway, for a different reason: because every
call is blind to every other cluster, this mode could not prevent
three separate clusters from independently landing on the same or a
near-identical generic label (all three converged on phrasing close to
"Viking Age Archaeology and Culture") — a structural problem no amount
of per-cluster stability checking fixes, since the missing information
(what another cluster was already named) is never available inside an
isolated call.

**Batch mode (kept, this is the current design).** All clusters in one
call. Fixed the duplicate-label problem directly — confirmed on a
25-cluster run with zero duplicate labels. Lost the stability check
(one call, not repeated).

**Hierarchical/multi-batch mode with a reconciliation pass.** Built to
scale batch mode past one call's context window: split clusters into
smaller sub-batches, label each with the same batch-mode call, then
run one more call showing every sub-batch's labels together (grouped
by sub-batch) asking the model to fix any label that collided across
sub-batches. Tested on a small, deliberately-split set (5 clusters
split 3+2 to force two sub-batches). Result: zero collisions found or
fixed, on a set too small and too clean to actually need it — the test
was inconclusive, not a confirmed failure, but also never demonstrated
working. Removed in the full rebuild along with everything else below;
not currently present in the code, though the concept remains valid
for scaling past ~25-30 clusters if the corpus's full ~800 need
labeling in one project.

**Self-critique second pass (`--critique`).** A second LLM call shown
every cluster's titles alongside its first-pass label, asked to check
specifically for genericness and duplication and revise only what
failed either check. Tested twice, on two different 5-cluster sets
(once with centroid sampling, once with diverse sampling + outlier
flagging). Both times: 0 of 5 labels revised. Never demonstrated
catching a real problem, because neither test set had one — the first
passes going in were already clean. Inconclusive, not proven useless,
but also never shown to work; dropped for that reason plus the general
decision to simplify.

**Few-shot examples in the batch prompt.** Included two
previously-confirmed-accurate labels (real label/description text,
verified against real titles, no fabricated example titles) as
concrete specificity targets before the model saw the clusters being
labeled. Tested once. Explicitly rejected on direct instruction,
without a diagnosed reason recorded at the time. Not reintroduced.

**Per-title outlier flagging.** Two implementations, in sequence:

- *v1 — flag a selected title if its distance to the cluster centroid
  exceeds the cluster's own mean + threshold×stdev.* Tested on real
  diverse-sampled output: flagged 13-14 of 15 titles per cluster. Root
  cause found: diverse sampling deliberately selects titles far from
  centroid by design, so almost every diversely-sampled title has high
  centroid distance regardless of whether it's actually noise — the
  metric was measuring the sampling strategy's own behavior, not
  anomalousness. Useless as built.
- *v2 — flag a selected title if its distance to its NEAREST OTHER
  selected title (not the centroid) exceeds the sample's own mean +
  threshold×stdev.* Fixed the flood of false positives (down to 1-2
  flagged titles per cluster instead of 13-14). But checked directly
  against real titles on 5 clusters: flagged titles that were
  genuinely on-topic about as often as it flagged real noise — e.g.
  flagged a strontium/lead isotope migration-tracing title in a
  migration cluster (a real, on-topic method) and flagged a wavelets
  paper in a paleoclimate-methods cluster (also genuinely on-topic).
  Roughly 50/50 right versus wrong on direct verification. Dropped:
  not reliable enough to trust unsupervised, and the whole design's
  point was to avoid needing supervision.

**Automated cluster-splitting detection.** k=2 k-means run on a
cluster's full membership (not just the sample), flagging the cluster
as a "split candidate" if the two resulting sub-groups sit far apart
relative to their own internal spread (a configurable ratio
threshold). Built specifically because manual inspection found a
cluster that looked genuinely bimodal. Run twice across different test
sets. Result: zero clusters ever flagged, including on sets where
manual title-by-title reading independently confirmed a real
dual-theme cluster was present (see "Dual labels" section below — the
same underlying phenomenon, caught by hand, that this mechanism was
built to catch automatically and never did). Clean failure on its one
known test case; dropped.

**Result of the full rebuild:** after all of the above, the design was
reset to five deliberately minimal steps (embed, sample, batch-label
with the generic-label instruction, no verification stage, write
output) — see "Why the labeling call handles every cluster at once"
and "Why the prompt names the corpus's shared baseline explicitly"
above for what was kept and why. Dual-label support (below) was added
back in afterward as the one addition with a concrete, hand-verified
finding behind it, rather than an untested guess.

## Usage

```bash
cd analysis/cluster_labelling
python3 -m venv .venv && source .venv/bin/activate
pip install sentence-transformers requests

python embed_titles.py ../../data/bibliography.json results/

python select_representatives.py \
    results/title_embeddings.npz \
    ../gn_analysis/results/multi_cut/communities_round_<N>.csv \
    results/ \
    --sampling diverse

ollama serve   # in a separate terminal, if not already running

python label_clusters.py \
    results/representatives.csv \
    ../gn_analysis/results/multi_cut/communities_round_<N>.csv \
    results/ \
    --model qwen3.5:35b
```

`<N>` is the round number of the fragmentation-onset cut
`02_network_structure.qmd` uses as `gn_earliest` — the smallest round
number under `gn_analysis/results/multi_cut/`.

`--max-clusters` on `select_representatives.py` scopes to the N
largest clusters (by member count), for a faster first pass before
running against the full set.

Model name is whatever is actually pulled locally (`ollama list`) —
double-check the exact tag exists before running; published model
names don't always match what's shown in general documentation
(`qwen3:35b` does not exist as a tag on this system, `qwen3.5:35b`
does — a different model family, not a typo).

## Dual labels for clusters that genuinely split into two themes

A hand-check of this corpus's largest clusters against their actual
titles found a real, repeated pattern: some clusters have a majority
theme plus a second, substantial, coherent theme — not one theme plus
a stray outlier title, but two real groups of several titles each
(e.g. a cluster combining ancient-DNA methodology with a distinct
cluster of titles about the ethics and identity politics of genetic
research). Forcing a single label onto a cluster like that either
drops one whole theme silently or produces a vague label that names
neither theme well.

The prompt now allows an optional `secondary_label` /
`secondary_description` per cluster for exactly this case. Most
clusters are expected to come back with only a primary label — the
instruction is explicit that a single outlier title is not grounds for
a secondary label, only a real, multi-title second theme is. This adds
no new computation and no threshold to tune; it is purely a change to
what the model is allowed to output, since the model's own description
text was already implicitly noticing dual themes in testing before
this field existed to hold it.

`node_labels.csv` still carries only the primary label per node — this
pipeline has no per-title assignment of which half of a dual-labeled
cluster each specific node belongs to (the model isn't asked to sort
individual titles into the primary/secondary theme, only to describe
both at the cluster level), so there's no way to split node membership
between the two labels.

## Output

- `results/title_embeddings.npz` — `names`, `titles`, `embeddings`
  arrays, order-aligned.
- `results/representatives.csv` — `community_id, name, title, rank,
  cosine_distance_to_centroid, cluster_size`.
- `results/cluster_labels.csv` — one row per cluster the call returned
  a label for: `community_id, label, description, secondary_label,
  secondary_description` (the last two are empty strings for the
  majority of clusters, which only need one label).
- `results/node_labels.csv` — every member node of a labeled cluster,
  expanded via the partition CSV so it covers the whole cluster, not
  just the representative-title subset: `node_id, community_id,
  label` (primary label only).

## Known limitations, not fixed here

- **No accuracy verification of any kind.** Nothing in this pipeline
  checks a generated label against the cluster's actual full
  membership, or against a second independent read.

  **A full manual audit was done once**, on the 25 largest clusters
  from a diverse-sampled, dual-label-unaware run (before the dual-label
  field existed), reading every cluster's real 15 sampled titles
  against its generated label. Rough tally: 14 of 25 clusters were
  fully or near-fully accurate; ~6 had exactly one clearly-unrelated
  title the label silently ignored (genuine noise, not a second theme
  — e.g. a single Mexican-immigration-studies title in an otherwise
  coherent Viking-diaspora cluster, a single photosynthesis-biochemistry
  title in an otherwise coherent isotope-analysis cluster); ~4 had a
  real, substantial second theme (several titles, not one) that the
  label dropped entirely rather than naming (the motivating case for
  dual-label support, above); 2 were genuinely incoherent grab-bag
  clusters where no single label could have been accurate, because the
  underlying community-detection grouping itself had weak topical
  coherence (see the bridging-paper case study below for why).

  Pattern observed: clusters organized around a specific method or
  technique (paleoclimate reconstruction, remote sensing, stable
  isotope analysis, archaeological chemistry, ancient-DNA
  bioinformatics) were consistently the most accurately labeled —
  tighter, more topically coherent groupings than clusters organized
  around a broader theme, period, or region.

- **A cluster's single label can flatten real internal diversity.**
  Dual-label support (above) addresses the "real second theme" case
  when the model catches it — it does not guarantee the model will
  catch every instance, and it does nothing for the single-outlier-
  title case (no title-level flag exists in the current design; see
  "Per-title outlier flagging" above for why that was tried and
  removed).

- **A cluster can be topically incoherent for a structural reason that
  no label wording can fix.** Traced one specific case directly: two
  titles in the same cluster — one on Holocene glacier archaeology in
  the Swiss Alps (`grosjean2007`), one on Holocaust memory studies
  (`piotrowski2006`) — have no citation relationship to each other at
  all (checked directly against `data/citation_edgelist.csv`), but
  both cite the same third paper, a multi-author interdisciplinary
  piece on "The Archaeology of Ice" (`solli2011`) whose author list
  includes several theorists of "archaeologies of the recent past," a
  subfield that discusses deep-time and traumatic/contemporary heritage
  sites within one shared theoretical framework. Both citing papers are
  almost certainly separate case studies referenced within different
  sections of that one bridging piece, which is why Girvan-Newman
  grouped them together (real citation-graph proximity) despite having
  no topical relationship to each other directly. No prompt improvement
  produces an accurate single label for a cluster whose coherence comes
  from one shared bridging citation rather than a shared topic — the
  "Within-cluster betweenness centrality" idea below was proposed
  specifically to surface this kind of structural bridge to the model
  so it can name it rather than invent a false thematic label, but is
  not built.

- **Scaling beyond one call's context window is not automated.** For a
  cluster count too large to fit in a single request even after
  raising `--num-ctx`, this pipeline has no built-in way to split the
  work and later reconcile labels across the split (hierarchical mode
  did this; see "What was tried, tested, and removed" above — it is
  not in the current code) — that would need to be rebuilt if the
  corpus's full ~800 clusters are labeled at once rather than in a
  scoped `--max-clusters` subset.

## Open, not yet decided

- Final embedding model choice, and whether to also embed `author`
  affiliation or `booktitle`/`journaltitle` alongside `title`.
- Whether to label every community at the working cut, or only the
  largest N, as standard practice.
- Whether the same pipeline should also run against Louvain and Leiden
  communities as further independent descriptions.
- Whether `--sampling diverse` actually improves label quality on this
  corpus relative to `centroid` — never systematically compared.
- Whether `--top-n` (15) is the right sample size.
- **Within-cluster betweenness centrality as a diagnostic.** A hand
  trace of one confusing cluster (two topically unrelated titles that
  turned out to share no direct citation link, but both cited the same
  third paper) confirmed that a cluster's apparent topical incoherence
  can come from a genuine structural bridge — one paper, often a
  broad theoretical or interdisciplinary piece, connecting two
  otherwise-unrelated sub-groups. Surfacing the highest-betweenness
  title within a cluster (computed on the citation edgelist, not the
  embeddings) to the labeling prompt could let the model explain that
  kind of cluster accurately (naming the bridging theme) instead of
  stretching a single label to cover unrelated content. Betweenness
  does NOT help with diffuse clusters that have no single bridge, only
  weak all-over connectivity — those would need a different diagnostic
  (e.g. overall within-cluster edge density) to flag. Not built.

## Keep out of git

Add to `.gitignore` alongside the `gn_analysis/results/` entries:

```
analysis/cluster_labelling/results/title_embeddings.npz
```

(Large binary, regenerable from `data/bibliography.json` at any time.
`representatives.csv` and `cluster_labels.csv` are small and fine to
commit.)