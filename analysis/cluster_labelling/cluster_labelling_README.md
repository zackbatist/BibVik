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
the cluster's representative titles; it is never shown the codebook,
its categories, or any example labels drawn from it. Comparison against
the codebook happens later, in `03_joined_descriptions.qmd` — a
separate, later step this pipeline does not touch.

## Steps

1. **`embed_titles.py`** — embed every active entry's title with a
   multilingual sentence-embedding model. Titles only, no abstract
   text, so F1 and F2 entries are on equal footing.
2. **`select_representatives.py`** — for each cluster, rank members by
   cosine distance to the cluster centroid and keep the closest N.
3. **`label_clusters.py`** — LLM labeling as a distinct downstream
   step. Runs the labeling prompt across multiple model/temperature/
   seed combinations and flags any cluster whose labels don't agree
   (pairwise cosine similarity below threshold) as unstable.
4. **Human review** — not automated. Read `candidate_labels.csv`
   against the actual member titles in `representatives.csv` before
   treating any label as established, especially anything flagged
   unstable.

## Usage

```bash
cd analysis/cluster_labelling
pip install sentence-transformers

python embed_titles.py ../../data/bibliography.json results/

python select_representatives.py \
    results/title_embeddings.npz \
    ../gn_analysis/results/communities.csv \
    results/

ollama serve   # if not already running
python label_clusters.py results/representatives.csv results/ \
    --models qwen3:35b qwen3:8b \
    --temperatures 0.3 0.7 \
    --seeds 1 2
```

## Output

- `results/title_embeddings.npz` — `names`, `titles`, `embeddings`
  arrays, order-aligned.
- `results/representatives.csv` — `community_id, name, title, rank,
  cosine_distance_to_centroid, cluster_size`.
- `results/candidate_labels.csv` — `community_id, run_id, model,
  temperature, seed, label, description, stable`. `stable` is per
  cluster (repeated across that cluster's rows): true only if every
  pairwise similarity among that cluster's run labels met the
  threshold.

## Open, not yet decided

- Final embedding model choice, and whether to also embed `author`
  affiliation or `booktitle`/`journaltitle` alongside `title`.
- Whether to label every community at the working cut, or only the
  largest N.
- Whether the same pipeline should also run against Louvain and Leiden
  communities as further independent descriptions.

## Keep out of git

Add to `.gitignore` alongside the `gn_analysis/results/` entries:

```
analysis/cluster_labelling/results/title_embeddings.npz
```

(Large binary, regenerable from `data/bibliography.json` at any time.
`representatives.csv` and `candidate_labels.csv` are small and fine to
commit.)
