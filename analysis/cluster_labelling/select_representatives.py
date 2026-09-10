"""
select_representatives.py — For each cluster/community, pull member
titles and select a representative sample to hand to the labeling step,
rather than every title in the cluster (clusters run into the dozens or
hundreds of members; showing all of them to an LLM is unnecessary and,
at scale, exceeds any reasonable prompt size).

Two sampling strategies (--sampling):
- centroid (default): the --top-n titles closest to the cluster
  centroid (cosine distance). Good for finding what's most typical of
  the cluster.
- diverse: farthest-point sampling — start from the title closest to
  centroid, then repeatedly add whichever remaining title is farthest
  (by minimum distance) from every title already picked. Spreads the
  --top-n selection across the cluster's actual internal variation
  instead of clustering all of them near the center, so a cluster that
  contains more than one real sub-theme is more likely to have all of
  them represented in the titles a downstream labeling prompt sees,
  rather than only its single most typical theme.

Input:  results/title_embeddings.npz (from embed_titles.py)
        a partition CSV with columns node_id, community_id — any
        community-detection output (GN, Louvain, Leiden) works, since
        this step has no dependency on which one produced the grouping.
Output: results/representatives.csv
        (community_id, name, title, rank, cosine_distance_to_centroid,
         cluster_size)

Usage:
    python select_representatives.py results/title_embeddings.npz \
        ../gn_analysis/results/communities.csv results/ \
        --top-n 15 --min-size 5 --max-clusters 25 --sampling diverse
"""

import argparse
import csv
from collections import defaultdict
from pathlib import Path

import numpy as np


def load_embeddings(npz_path: Path):
    data = np.load(npz_path, allow_pickle=True)
    names = data["names"].tolist()
    titles = data["titles"].tolist()
    embeddings = data["embeddings"]
    name_to_idx = {n: i for i, n in enumerate(names)}
    return names, titles, embeddings, name_to_idx


def load_partition(partition_csv: Path) -> dict[str, str]:
    """node_id -> community_id, as strings throughout (community ids may
    not be purely numeric depending on source)."""
    membership = {}
    with open(partition_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            membership[row["node_id"]] = row["community_id"]
    return membership


def select_centroid(cluster_embeddings: np.ndarray, top_n: int) -> tuple[np.ndarray, np.ndarray]:
    """top_n positions closest to the cluster centroid.
    Returns (positions, distance_to_centroid_for_each_selected_position)."""
    centroid = cluster_embeddings.mean(axis=0)
    centroid_norm = centroid / (np.linalg.norm(centroid) + 1e-12)
    # Embeddings are already normalized at encode time (embed_titles.py
    # uses normalize_embeddings=True), so dot product is cosine similarity.
    similarities = cluster_embeddings @ centroid_norm
    distances = 1.0 - similarities
    order = np.argsort(distances)[:top_n]
    return order, distances[order]


def select_diverse(cluster_embeddings: np.ndarray, top_n: int) -> tuple[np.ndarray, np.ndarray]:
    """Farthest-point sampling: start from the centroid-closest title
    (the one most representative of the cluster overall), then greedily
    add whichever remaining title maximizes its minimum cosine distance
    to every title already selected. Spreads the selection across the
    cluster's internal variation instead of clumping near the centroid.
    Returns (positions, distance_to_centroid_for_each_selected_position)
    — centroid distance is still reported for comparability with
    --sampling centroid's output, even though it isn't what drove
    selection after the first pick."""
    n = cluster_embeddings.shape[0]
    top_n = min(top_n, n)

    centroid = cluster_embeddings.mean(axis=0)
    centroid_norm = centroid / (np.linalg.norm(centroid) + 1e-12)
    centroid_similarities = cluster_embeddings @ centroid_norm
    centroid_distances = 1.0 - centroid_similarities

    # Full pairwise cosine distance matrix — fine at this scale (a
    # cluster here tops out around a few hundred members, so n^2 is
    # small; this would need rethinking for clusters in the thousands).
    similarity_matrix = cluster_embeddings @ cluster_embeddings.T
    distance_matrix = 1.0 - similarity_matrix

    selected = [int(np.argmin(centroid_distances))]
    min_dist_to_selected = distance_matrix[selected[0]].copy()

    while len(selected) < top_n:
        # Exclude already-selected positions from consideration by
        # forcing their distance to -inf so argmax never re-picks them.
        candidate_scores = min_dist_to_selected.copy()
        candidate_scores[selected] = -np.inf
        next_idx = int(np.argmax(candidate_scores))
        selected.append(next_idx)
        min_dist_to_selected = np.minimum(min_dist_to_selected, distance_matrix[next_idx])

    order = np.array(selected)
    return order, centroid_distances[order]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("embeddings_npz", type=Path)
    parser.add_argument("partition_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--top-n", type=int, default=15,
        help="Representative titles kept per cluster (default: 15)",
    )
    parser.add_argument(
        "--min-size", type=int, default=3,
        help="Clusters smaller than this are skipped entirely (default: 3)",
    )
    parser.add_argument(
        "--max-clusters", type=int, default=None,
        help="Only process the N largest clusters (by member count, after "
             "--min-size filtering). Default: no limit, process every "
             "qualifying cluster.",
    )
    parser.add_argument(
        "--sampling", choices=["centroid", "diverse"], default="centroid",
        help="centroid (default): the --top-n titles closest to the "
             "cluster centroid. diverse: farthest-point sampling, "
             "spreading the selection across the cluster's internal "
             "variation so a cluster with real sub-themes is more "
             "likely to have all of them represented.",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    names, titles, embeddings, name_to_idx = load_embeddings(args.embeddings_npz)
    membership = load_partition(args.partition_csv)

    members_by_cluster: dict[str, list[int]] = defaultdict(list)
    skipped_no_embedding = 0
    for node_id, community_id in membership.items():
        idx = name_to_idx.get(node_id)
        if idx is None:
            # Ghost node or entry with no title — no embedding exists.
            skipped_no_embedding += 1
            continue
        members_by_cluster[community_id].append(idx)

    print(
        f"{len(members_by_cluster)} clusters with at least one embedded "
        f"member; {skipped_no_embedding} partition entries had no title "
        f"embedding (ghost nodes or missing titles) and were skipped.",
    )

    # Filter by min-size first, then optionally keep only the largest N —
    # sorting by size so --max-clusters always means "the biggest ones",
    # not an arbitrary dict-iteration-order subset.
    qualifying = {
        cid: idxs for cid, idxs in members_by_cluster.items()
        if len(idxs) >= args.min_size
    }
    n_skipped_small = len(members_by_cluster) - len(qualifying)

    cluster_ids_in_order = sorted(qualifying, key=lambda cid: len(qualifying[cid]), reverse=True)
    n_skipped_by_cap = 0
    if args.max_clusters is not None and len(cluster_ids_in_order) > args.max_clusters:
        n_skipped_by_cap = len(cluster_ids_in_order) - args.max_clusters
        cluster_ids_in_order = cluster_ids_in_order[: args.max_clusters]

    rows = []
    for community_id in cluster_ids_in_order:
        idxs = qualifying[community_id]
        cluster_embeddings = embeddings[idxs]

        if args.sampling == "diverse":
            order, selected_centroid_distances = select_diverse(cluster_embeddings, args.top_n)
        else:
            order, selected_centroid_distances = select_centroid(cluster_embeddings, args.top_n)

        for rank, pos in enumerate(order, start=1):
            idx = idxs[pos]
            rows.append({
                "community_id": community_id,
                "name": names[idx],
                "title": titles[idx],
                "rank": rank,
                "cosine_distance_to_centroid": round(float(selected_centroid_distances[rank - 1]), 6),
                "cluster_size": len(idxs),
            })

    if n_skipped_small:
        print(f"{n_skipped_small} clusters skipped for being smaller than --min-size={args.min_size}.")
    if n_skipped_by_cap:
        print(f"{n_skipped_by_cap} additional clusters skipped by --max-clusters={args.max_clusters} "
              f"(kept the {args.max_clusters} largest).")

    out_path = args.output_dir / "representatives.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "community_id", "name", "title", "rank",
                "cosine_distance_to_centroid", "cluster_size",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} representative-title rows across "
          f"{len(cluster_ids_in_order)} clusters to {out_path}")


if __name__ == "__main__":
    main()