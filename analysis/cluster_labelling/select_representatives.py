"""
select_representatives.py — For each cluster/community, pull member
titles and rank them by cosine distance to the cluster centroid, to
identify the most centrally-representative titles rather than an
arbitrary or alphabetical sample.

Input:  results/title_embeddings.npz (from embed_titles.py)
        a partition CSV with columns node_id, community_id — any
        community-detection output (GN, Louvain, Leiden) works, since
        this step has no dependency on which one produced the grouping.
Output: results/representatives.csv
        (community_id, name, title, rank, cosine_distance_to_centroid)

Usage:
    python select_representatives.py results/title_embeddings.npz \\
        ../gn_analysis/results/communities.csv results/ \\
        --top-n 15 --min-size 5
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

    rows = []
    n_skipped_small = 0
    for community_id, idxs in members_by_cluster.items():
        if len(idxs) < args.min_size:
            n_skipped_small += 1
            continue

        cluster_embeddings = embeddings[idxs]
        centroid = cluster_embeddings.mean(axis=0)
        centroid_norm = centroid / (np.linalg.norm(centroid) + 1e-12)

        # Embeddings are already normalized at encode time (embed_titles.py
        # uses normalize_embeddings=True), so dot product is cosine similarity.
        similarities = cluster_embeddings @ centroid_norm
        distances = 1.0 - similarities

        order = np.argsort(distances)[: args.top_n]
        for rank, pos in enumerate(order, start=1):
            idx = idxs[pos]
            rows.append({
                "community_id": community_id,
                "name": names[idx],
                "title": titles[idx],
                "rank": rank,
                "cosine_distance_to_centroid": round(float(distances[pos]), 6),
                "cluster_size": len(idxs),
            })

    if n_skipped_small:
        print(f"{n_skipped_small} clusters skipped for being smaller than --min-size={args.min_size}.")

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
          f"{len(members_by_cluster) - n_skipped_small} clusters to {out_path}")


if __name__ == "__main__":
    main()
