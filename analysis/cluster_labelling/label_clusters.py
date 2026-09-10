"""
label_clusters.py — LLM labeling, as a distinct, downstream step
structurally separate from the embedding/clustering step (mechanical
structure first, interpretive label applied to its output after, never
the reverse).

Every cluster's representative titles go into a single prompt, one
call, so the model can see all clusters at once and avoid assigning
the same or a near-identical label to two different clusters — a
per-cluster call, blind to every other cluster's titles or label, has
no way to prevent that, since it never sees what any other cluster
was named. Real duplicate-label collisions were the trigger for this
design.

The prompt does not mention the annotation codebook, its categories,
or any example labels drawn from it, in any form — this is the
concrete mechanism by which "independent of the codebook" is enforced.

The prompt also states the corpus's shared umbrella theme (Viking Age,
medieval Scandinavia) directly and instructs the model that a label
true of nearly every cluster fails and must be replaced with something
naming what's actually distinctive about that specific cluster — the
direct fix for labels defaulting to the always-true, uninformative
broad description.

The prompt also permits an optional secondary label/description per
cluster, for the case (confirmed real, found on this corpus's own top
clusters) where a cluster's titles genuinely split into two
substantial themes rather than one dominant theme plus a few stray
outliers — forcing a single label onto that kind of cluster either
drops one whole theme silently or produces a vague label that names
neither theme clearly. Most clusters are expected to use only the
primary label; the secondary fields are empty unless the model judges
a real second theme is present.

No stability check across repeated runs, no human review step: this
is a single automated pass, by design — no manual verification of any
label happens here.

Input:  representatives_csv (from select_representatives.py)
        partition_csv (node_id, community_id — the same file passed to
        select_representatives.py, needed here to expand cluster-level
        labels back out to every member node)
Output: results/cluster_labels.csv — one row per cluster the call
        returned a label for: community_id, label, description,
        secondary_label, secondary_description (the last two are empty
        strings for clusters that did not need a second label)
        results/node_labels.csv — every member node of a labeled
        cluster, expanded via the partition CSV: node_id, community_id,
        label (primary label only — see the note in main() for why the
        secondary label isn't assigned per node)

Usage:
    ollama serve   # if not already running
    python label_clusters.py results/representatives.csv \
        ../gn_analysis/results/multi_cut/communities_round_10207.csv \
        results/ --model qwen3.5:35b
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests

LABEL_PROMPT = """You are analyzing {n_clusters} clusters of academic paper titles from a citation graph. Each cluster's titles were grouped together by an automated clustering method based on semantic similarity, not by topic labels or any predefined taxonomy. All clusters below are from the same corpus, which is entirely about Viking Age and medieval Scandinavia.

## Clusters

{clusters_block}

## Task

Write a label and short description for EVERY cluster listed above. You can see all {n_clusters} clusters at once — use that. A label like "Viking Age Archaeology" or "Norse Studies" is true of nearly every cluster here and communicates nothing about any one of them; do not assign the same or a near-identical label to two different clusters. If you notice two clusters are genuinely about to get the same label, that is a signal to look harder at what actually distinguishes them — a specific method, material, site, region, time-slice, or theoretical angle — and sharpen both labels around that difference before finalizing either one.

For each cluster, work out its dominant axis first (the one specific thing that actually organizes its titles), then write a label built from that axis, not from the shared corpus-wide period or field. Titles in a real cluster usually share one such axis even when their surface topics vary.

Some clusters genuinely do not reduce to one theme — the titles split into two substantial, roughly-comparable groups rather than one dominant topic plus a few stray outliers. Before finalizing any description, check it against this specific test: does the description mention a second topic only as a subordinate clause or add-on to the main sentence (e.g. "...and also touches on X" or "...while critically evaluating Y") rather than treating it as its own thing? That pattern — one sentence trying to hold two separate ideas together — is the actual sign that a cluster needs the secondary_label field, not a sign that the single description handled it. Compressing a real second theme into a subordinate clause instead of using secondary_label is not an acceptable alternative — if you notice yourself writing a description with a "but/while/also" clause introducing a genuinely different topic than the main sentence, stop and move that second topic into secondary_label/secondary_description instead of leaving it folded into the primary description. Use secondary_label only when the second theme is substantial (several titles, not one) — a single title that does not fit is not a second theme; note that title does not fit within the main description instead. Most clusters will still end up with only a primary label — that remains the expected default — but do not let that default become an excuse to compress a real second theme into a subordinate clause of the primary description instead of using the field built for it.

Respond in JSON format as a single object mapping each cluster's ID (as given in the input, as a string) to its label information:
{{
  "<cluster_id>": {{
    "label": "<a short phrase, 2-6 words, built from that cluster's specific axis>",
    "description": "<1-3 sentences naming the axis and explaining how the titles relate to it>",
    "secondary_label": "<optional — a second short phrase, ONLY if the cluster genuinely splits into two substantial themes; otherwise empty string>",
    "secondary_description": "<optional — 1-3 sentences for the second theme; otherwise empty string>"
  }},
  ...
}}

Include an entry for every cluster ID given above, in any order. Respond ONLY with the JSON object. No preamble, no markdown fences."""


def load_representatives(path: Path) -> dict[str, list[str]]:
    titles_by_cluster: dict[str, list[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            titles_by_cluster[row["community_id"]].append(row["title"])
    return titles_by_cluster


def load_partition(path: Path) -> dict[str, list[str]]:
    """community_id -> [node_id, ...], for expanding cluster-level labels
    back out to every member node (not just the representative subset)."""
    members_by_cluster: dict[str, list[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            members_by_cluster[row["community_id"]].append(row["node_id"])
    return members_by_cluster


def query_ollama(
    base_url: str, model: str, prompt: str, temperature: float,
    timeout: int, num_ctx: int, num_predict: int,
) -> str | None:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict},
    }
    try:
        resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
    except requests.RequestException as e:
        print(f"request failed: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"Ollama returned status {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return None
    text = resp.json().get("response", "")
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    return text or None


def parse_batch_json(raw: str, expected_ids: set[str]) -> dict[str, dict] | None:
    """Parse the {cluster_id: {label, description, secondary_label,
    secondary_description}} response. secondary_label/secondary_description
    are optional per cluster — most clusters will have them empty, since
    a dual label is only meant for clusters that genuinely split into two
    substantial themes rather than one theme plus stray outliers. Returns
    only entries that are well-formed and whose ID was actually asked
    for; logs any expected ID the model dropped."""
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    result = {}
    for cid, entry in obj.items():
        if cid not in expected_ids:
            continue
        if not isinstance(entry, dict) or "label" not in entry or "description" not in entry:
            continue
        result[cid] = {
            "label": entry["label"],
            "description": entry["description"],
            "secondary_label": entry.get("secondary_label") or "",
            "secondary_description": entry.get("secondary_description") or "",
        }

    missing = expected_ids - set(result.keys())
    if missing:
        print(f"response missing {len(missing)} expected cluster(s): {sorted(missing)}",
              file=sys.stderr)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("representatives_csv", type=Path)
    parser.add_argument("partition_csv", type=Path,
                         help="node_id,community_id CSV — same file passed "
                              "to select_representatives.py")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3.5:35b")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--timeout", type=int, default=1800,
                         help="Call timeout in seconds (default: 1800 = 30 min).")
    parser.add_argument("--num-ctx", type=int, default=32768,
                         help="Context window in tokens, covering prompt + "
                              "response together (default: 32768). Raise this "
                              "if labeling more clusters than fit comfortably "
                              "— Ollama's server default (much smaller) "
                              "silently truncates on overflow rather than "
                              "erroring.")
    parser.add_argument("--num-predict", type=int, default=8192,
                         help="Max response tokens (default: 8192) — needs "
                              "to fit a label + description for every "
                              "cluster in the request.")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    titles_by_cluster = load_representatives(args.representatives_csv)
    members_by_cluster = load_partition(args.partition_csv)
    cluster_ids = list(titles_by_cluster.keys())
    print(f"{len(cluster_ids)} clusters to label.", file=sys.stderr)

    clusters_block_parts = []
    for cid in cluster_ids:
        titles_block = "\n".join(f"  - {t}" for t in titles_by_cluster[cid])
        clusters_block_parts.append(f"### Cluster {cid}\n{titles_block}")
    clusters_block = "\n\n".join(clusters_block_parts)

    prompt = LABEL_PROMPT.format(n_clusters=len(cluster_ids), clusters_block=clusters_block)

    print(f"Labeling {len(cluster_ids)} cluster(s) in one call ({args.model}, "
          f"T={args.temperature})...", file=sys.stderr)
    raw = query_ollama(
        args.base_url, args.model, prompt, args.temperature,
        args.timeout, args.num_ctx, args.num_predict,
    )
    if raw is None:
        print("Call failed — nothing written.", file=sys.stderr)
        sys.exit(1)

    parsed = parse_batch_json(raw, expected_ids=set(cluster_ids))
    if parsed is None:
        print("Response was not valid JSON — nothing written. Raw response follows:\n"
              + raw[:2000], file=sys.stderr)
        sys.exit(1)

    cluster_labels_path = args.output_dir / "cluster_labels.csv"
    with open(cluster_labels_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["community_id", "label", "description",
                           "secondary_label", "secondary_description"],
        )
        writer.writeheader()
        for cid in cluster_ids:
            if cid in parsed:
                writer.writerow({
                    "community_id": cid,
                    "label": parsed[cid]["label"],
                    "description": parsed[cid]["description"],
                    "secondary_label": parsed[cid]["secondary_label"],
                    "secondary_description": parsed[cid]["secondary_description"],
                })

    # node_labels.csv carries only the primary label per node — a node
    # is a member of one cluster, and the cluster's primary label is its
    # main identity; the secondary theme (when present) describes a
    # sub-pattern within the cluster, not a separate assignment for
    # individual nodes, since this pipeline has no way to tell which
    # specific member nodes belong to which half of a dual-labeled
    # cluster (that would need per-title, not per-cluster, information
    # the model was never asked to provide).
    node_labels_path = args.output_dir / "node_labels.csv"
    node_rows = []
    for cid, entry in parsed.items():
        for node_id in members_by_cluster.get(cid, []):
            node_rows.append({"node_id": node_id, "community_id": cid, "label": entry["label"]})
    with open(node_labels_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["node_id", "community_id", "label"])
        writer.writeheader()
        writer.writerows(node_rows)

    n_dual = sum(1 for entry in parsed.values() if entry["secondary_label"])
    print(f"Wrote {len(parsed)} of {len(cluster_ids)} cluster labels to {cluster_labels_path} "
          f"and expanded to {len(node_rows)} node(s) in {node_labels_path}.")
    if n_dual:
        print(f"{n_dual} cluster(s) received a secondary label (genuinely split into "
              f"two themes): "
              + ", ".join(cid for cid, entry in parsed.items() if entry["secondary_label"]))


if __name__ == "__main__":
    main()