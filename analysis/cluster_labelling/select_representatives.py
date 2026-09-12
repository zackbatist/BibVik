"""
select_representatives.py — For each cluster/community, select the
titles to hand to the labeling step, using citation-based ranking
within each cluster (link strength / degree in the cluster's own
induced subgraph), not embedding distance.

This replaces the earlier embedding-distance sampling (centroid /
diverse) after finding a directly applicable precedent for this exact
task. Camelo-Guerrero & Diaz-Rodriguez, "How Much Structure Do LLMs
Need? Evaluating LLMs for Bibliometric Cluster Description" (2026,
arXiv:2605.24351) compared six pipelines for LLM-generated cluster
descriptions, including one — "Ranked" — that selects the top-K papers
per cluster by citation-based link strength and gives only those to
the LLM. Across their evaluation, for citation-based clustering
specifically (as opposed to bibliographic coupling), Ranked was the
best-performing structured pipeline on cluster-quality and
graph-quality metrics, and was independently rated best by human
evaluators among the structured pipelines. Their finding: "citation
analysis... often concentrates structure around influential or
central works. In this setting, Ranked performs especially well
because top link-ranked papers can capture much of the cluster's
lineage, influence pattern, or methodological core."

Since this project's clustering is direct citation (Girvan-Newman on
the citation graph), not bibliographic coupling, this is the method
the paper's own results say to use here specifically — not a compromise
or an adaptation, the same relation type.

Method: for each cluster, build the subgraph induced by just that
cluster's own member papers (only citation edges where BOTH endpoints
are in the cluster), then rank papers by their degree within that
subgraph — how many other papers in the SAME cluster cite them or are
cited by them. This is "link strength within the cluster," the
citation-analysis analogue of the paper's bibliographic-coupling link
strength. Take the top --top-n papers by this ranking per cluster.

This is a real change in what "representative" means: previously,
"representative" meant "close to the cluster's average embedding" or
"spread across the cluster's embedding range." Now it means
"structurally central to why this cluster exists as a citation
community" — a paper highly cited within its own cluster is part of
why Girvan-Newman grouped that cluster together in the first place,
independent of what the paper's title says.

Input:  data/bibliography.json (for title/citekey lookup)
        data/citation_edgelist.csv (source,target citekey pairs)
        a partition CSV with columns node_id, community_id — if not
        given explicitly via --partition-csv, this script looks in
        ../gn_analysis/results/multi_cut/ for files matching
        communities_round_<N>.csv and picks the smallest N, matching
        02_network_structure.qmd's own logic for picking the
        fragmentation-onset cut (gn_earliest). Errors out if that
        directory or no matching files exist and --partition-csv
        wasn't given.
Output: results/representatives.csv
        (community_id, name, title, rank, within_cluster_degree,
         cluster_size)

Usage:
    # partition file found automatically from gn_analysis/results/multi_cut/:
    python select_representatives.py \
        ../../data/bibliography.json \
        ../../data/citation_edgelist.csv \
        results/ --top-n 10 --max-clusters 25

    # or specify a partition file explicitly:
    python select_representatives.py \
        ../../data/bibliography.json \
        ../../data/citation_edgelist.csv \
        results/ --top-n 10 --max-clusters 25 \
        --partition-csv ../gn_analysis/results/multi_cut/communities_round_10207.csv
"""

import argparse
import csv
import json
import re
from collections import defaultdict
from pathlib import Path


def load_titles(bibliography_json: Path) -> dict[str, str]:
    with open(bibliography_json, "r", encoding="utf-8") as f:
        bib = json.load(f)
    titles = {}
    for key, entry in bib.items():
        if entry.get("_deleted"):
            continue
        title = (entry.get("title") or "").strip()
        if title:
            titles[key] = title
    return titles


def load_edges(edgelist_csv: Path) -> list[tuple[str, str]]:
    edges = []
    with open(edgelist_csv, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            edges.append((row["source"], row["target"]))
    return edges


def find_partition_csv(script_dir: Path) -> Path:
    """Locate the fragmentation-onset cut the same way
    02_network_structure.qmd does: look in
    ../gn_analysis/results/multi_cut/ for files matching
    communities_round_<N>.csv and return the one with the smallest N.
    Raises FileNotFoundError with a clear message if the directory or
    no matching files exist, rather than silently returning nothing."""
    multi_cut_dir = script_dir / ".." / "gn_analysis" / "results" / "multi_cut"
    multi_cut_dir = multi_cut_dir.resolve()
    if not multi_cut_dir.is_dir():
        raise FileNotFoundError(
            f"No --partition-csv given and {multi_cut_dir} does not exist. "
            f"Either run gn_analysis/ first, or pass --partition-csv explicitly."
        )
    pattern = re.compile(r"^communities_round_(\d+)\.csv$")
    candidates = []
    for f in multi_cut_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            candidates.append((int(m.group(1)), f))
    if not candidates:
        raise FileNotFoundError(
            f"No --partition-csv given and no communities_round_<N>.csv files "
            f"found in {multi_cut_dir}. Either run "
            f"gn_analysis/generate_multi_cut_communities.R first, or pass "
            f"--partition-csv explicitly."
        )
    candidates.sort(key=lambda pair: pair[0])
    round_num, path = candidates[0]
    print(f"No --partition-csv given — using the fragmentation-onset cut "
          f"found automatically: round {round_num} ({path})")
    return path


def load_partition(partition_csv: Path) -> dict[str, str]:
    """node_id -> community_id."""
    membership = {}
    with open(partition_csv, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            membership[row["node_id"]] = row["community_id"]
    return membership


def rank_by_within_cluster_degree(
    cluster_members: set[str], edges: list[tuple[str, str]],
) -> list[tuple[str, int]]:
    """For one cluster's member set, count each member's degree in the
    subgraph induced by ONLY that cluster's own members — an edge only
    counts if both its source and target are in this cluster. This is
    the citation-analysis link-strength ranking the paper's "Ranked"
    pipeline uses (their bibliographic-coupling version uses shared-
    reference weight; this project's clustering is direct citation, so
    within-cluster citation degree is the direct analogue). Returns
    (paper_id, degree) pairs sorted by degree descending."""
    degree: dict[str, int] = defaultdict(int)
    for member in cluster_members:
        degree[member] = 0
    for src, tgt in edges:
        if src in cluster_members and tgt in cluster_members:
            degree[src] += 1
            degree[tgt] += 1
    return sorted(degree.items(), key=lambda kv: kv[1], reverse=True)


def select_top_with_independence(
    cluster_members: set[str], edges: list[tuple[str, str]], top_n: int,
    hub_ratio_threshold: float = 5.0,
) -> list[tuple[str, int]]:
    """Pure top-N by within-cluster degree has a real failure mode,
    confirmed directly on this corpus: when one paper's degree massively
    dominates the rest (observed case: one paper at degree 86, every
    other paper in the same cluster at degree 1), most of the "top N"
    slots end up filled by that hub paper's own citation satellites —
    papers whose only meaningful edge in the subgraph is to the hub
    itself. The LLM then effectively reads the hub paper's title
    several times over in different phrasing and produces a label that
    restates it, rather than synthesizing genuinely independent
    structure within the cluster.

    Fix: after taking papers directly connected to the top-ranked hub
    (if a hub exists — rank-1's degree at least hub_ratio_threshold
    times rank-2's degree), fill remaining slots preferentially with
    the highest-degree papers that are NOT directly connected to that
    hub, so the LLM sees independent sub-structure within the cluster
    instead of only satellites of one paper. If no hub is detected
    (rank-1 isn't disproportionately dominant), this is equivalent to
    plain top-N by degree — the independence preference only activates
    when a real hub is present.

    Returns the same (paper_id, degree) format as
    rank_by_within_cluster_degree, in the order papers were selected
    (not necessarily strict degree order once independence-preference
    kicks in — the hub, if any, is still always first)."""
    ranked = rank_by_within_cluster_degree(cluster_members, edges)
    if len(ranked) <= top_n:
        return ranked

    top_id, top_degree = ranked[0]
    second_degree = ranked[1][1] if len(ranked) > 1 else 0
    is_hub_dominated = (
        top_degree > 0
        and second_degree > 0
        and (top_degree / second_degree) >= hub_ratio_threshold
    )
    if not is_hub_dominated:
        return ranked[:top_n]

    # Identify which papers are directly connected to the hub — these
    # are the ones most likely to just be restating the hub's own
    # framing in their own titles (the observed pattern: satellite
    # titles referencing the hub's own terminology).
    hub_neighbors: set[str] = set()
    for src, tgt in edges:
        if src == top_id and tgt in cluster_members:
            hub_neighbors.add(tgt)
        if tgt == top_id and src in cluster_members:
            hub_neighbors.add(src)

    independent = [(pid, deg) for pid, deg in ranked[1:] if pid not in hub_neighbors]
    satellites = [(pid, deg) for pid, deg in ranked[1:] if pid in hub_neighbors]

    # Hub first, then as many independent (non-satellite) papers as
    # available, then fill any remaining slots with satellites (by
    # degree) if independent papers alone don't fill top_n.
    selected = [(top_id, top_degree)]
    remaining = top_n - 1
    selected.extend(independent[:remaining])
    remaining -= len(independent[:remaining])
    if remaining > 0:
        selected.extend(satellites[:remaining])

    return selected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bibliography_json", type=Path)
    parser.add_argument("edgelist_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--partition-csv", type=Path, default=None,
        help="node_id,community_id CSV. If not given, auto-detected "
             "from ../gn_analysis/results/multi_cut/ — the smallest "
             "communities_round_<N>.csv, same logic "
             "02_network_structure.qmd uses for gn_earliest.",
    )
    parser.add_argument(
        "--top-n", type=int, default=10,
        help="Papers kept per cluster, ranked by within-cluster "
             "citation degree (default: 10, matching the paper's "
             "Ranked pipeline).",
    )
    parser.add_argument(
        "--hub-ratio-threshold", type=float, default=5.0,
        help="When the top-ranked paper's within-cluster degree is at "
             "least this many times the second-ranked paper's degree, "
             "the cluster is treated as hub-dominated: remaining "
             "--top-n slots are filled preferentially with papers NOT "
             "directly cited by or citing the hub, instead of pure "
             "degree order, so the LLM sees independent structure "
             "within the cluster rather than only the hub's own "
             "citation satellites (confirmed on this corpus: one "
             "cluster had a top paper at degree 86 against every other "
             "member at degree 1, and pure top-N selection filled "
             "every remaining slot with that hub's satellites). Set "
             "to a very large number to disable and always use plain "
             "degree-order top-N.",
    )
    parser.add_argument(
        "--min-size", type=int, default=3,
        help="Clusters smaller than this are skipped entirely (default: 3).",
    )
    parser.add_argument(
        "--max-clusters", type=int, default=None,
        help="Only process the N largest clusters (by member count). "
             "Default: no limit, process every qualifying cluster.",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    partition_csv = args.partition_csv
    if partition_csv is None:
        script_dir = Path(__file__).parent
        partition_csv = find_partition_csv(script_dir)

    titles = load_titles(args.bibliography_json)
    edges = load_edges(args.edgelist_csv)
    membership = load_partition(partition_csv)

    members_by_cluster: dict[str, set[str]] = defaultdict(set)
    skipped_no_title = 0
    for node_id, community_id in membership.items():
        if node_id not in titles:
            # Ghost node or entry with no usable title — cannot be
            # shown to the labeling LLM regardless of its citation
            # centrality, since the LLM only ever sees titles.
            skipped_no_title += 1
            continue
        members_by_cluster[community_id].add(node_id)

    print(f"{len(members_by_cluster)} clusters with at least one titled "
          f"member; {skipped_no_title} partition entries had no usable "
          f"title and were skipped.")

    qualifying = {
        cid: members for cid, members in members_by_cluster.items()
        if len(members) >= args.min_size
    }
    n_skipped_small = len(members_by_cluster) - len(qualifying)

    cluster_ids_in_order = sorted(qualifying, key=lambda cid: len(qualifying[cid]), reverse=True)
    n_skipped_by_cap = 0
    if args.max_clusters is not None and len(cluster_ids_in_order) > args.max_clusters:
        n_skipped_by_cap = len(cluster_ids_in_order) - args.max_clusters
        cluster_ids_in_order = cluster_ids_in_order[: args.max_clusters]

    rows = []
    n_zero_degree_clusters = 0
    hub_dominated_clusters = []
    for community_id in cluster_ids_in_order:
        members = qualifying[community_id]
        ranked = rank_by_within_cluster_degree(members, edges)

        if ranked and ranked[0][1] == 0:
            # No internal citation edges at all among this cluster's
            # titled members — degree ranking has nothing to rank on.
            # This can happen if the cluster's internal cohesion comes
            # mostly through ghost nodes / untitled entries not counted
            # here, or if min-size let through a genuinely sparse
            # cluster. Falls back to listing members in arbitrary
            # (dict-iteration) order rather than silently producing a
            # meaningless "top 10" that isn't actually ranked by
            # anything.
            n_zero_degree_clusters += 1

        top = select_top_with_independence(
            members, edges, args.top_n, args.hub_ratio_threshold,
        )
        if len(ranked) > 1 and ranked[1][1] > 0 and (ranked[0][1] / ranked[1][1]) >= args.hub_ratio_threshold:
            hub_dominated_clusters.append((community_id, ranked[0][1], ranked[1][1]))

        for rank, (node_id, deg) in enumerate(top, start=1):
            rows.append({
                "community_id": community_id,
                "name": node_id,
                "title": titles[node_id],
                "rank": rank,
                "within_cluster_degree": deg,
                "cluster_size": len(members),
            })

    if n_skipped_small:
        print(f"{n_skipped_small} clusters skipped for being smaller than --min-size={args.min_size}.")
    if n_skipped_by_cap:
        print(f"{n_skipped_by_cap} additional clusters skipped by --max-clusters={args.max_clusters} "
              f"(kept the {args.max_clusters} largest).")
    if n_zero_degree_clusters:
        print(f"{n_zero_degree_clusters} cluster(s) had zero within-cluster citation edges among "
              f"titled members — their representative titles are in arbitrary order, not ranked by "
              f"anything meaningful. Worth a look at which clusters these are.")
    if hub_dominated_clusters:
        print(f"{len(hub_dominated_clusters)} cluster(s) were hub-dominated (top paper's degree at "
              f"least {args.hub_ratio_threshold}x the second-ranked paper's) — remaining slots for "
              f"these were filled preferring papers NOT directly connected to the hub, instead of "
              f"pure degree order: "
              + ", ".join(f"{cid} ({top_deg} vs {second_deg})"
                           for cid, top_deg, second_deg in hub_dominated_clusters))

    out_path = args.output_dir / "representatives.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["community_id", "name", "title", "rank",
                        "within_cluster_degree", "cluster_size"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} representative-title rows across "
          f"{len(cluster_ids_in_order)} clusters to {out_path}")


if __name__ == "__main__":
    main()