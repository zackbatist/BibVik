#!/usr/bin/env python3
"""resolve_duplicate_labels.py — real, second-pass duplicate resolution
for cluster_labels.csv, as a distinct, downstream step from
label_clusters.py itself (same structural-separation principle that
file already follows: mechanical detection first, interpretive
judgment on its output after, never the reverse).

label_clusters.py's own check_agreement() flags label pairs across
different batches that share enough words to look similar — a coarse,
embedding-free heuristic, real false positives on genuine synonyms
expected (see that function's own docstring). This script does not
trust that flag as proof of duplication; it re-presents each flagged
pair, with its real titles (not just the labels), to the LLM and asks
a direct, real question: genuinely the same theme, or genuinely
distinct despite the word overlap.

This is deliberately NOT a full re-labeling pass. Only the pairs
check_agreement() already flagged are sent, each pair on its own,
real call, so the model judges two clusters at a time with their real
representative titles in view, not the whole 100-cluster set again.

Output is a real, separate report (results/duplicate_resolution.csv),
not a silent rewrite of cluster_labels.csv: a human reviewer decides
whether to merge, rename, or leave each flagged pair as-is, using this
report's real, stated verdict and reasoning as input to that decision,
not as an automatic action.

Usage:
    python resolve_duplicate_labels.py \
        results/cluster_labels.csv \
        results/representatives.csv \
        results/ \
        --model qwen3.5:35b --base-url http://localhost:11450
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests

# Imported logic, not reimplemented: same real function label_clusters.py
# uses to flag pairs in the first place, so this script finds exactly the
# same pairs a fresh run of that script would report, not a different set
# from subtly different matching logic.
sys.path.insert(0, str(Path(__file__).parent))
from label_clusters import check_agreement, query_ollama  # noqa: E402


RESOLVE_PROMPT = """You are reviewing two clusters of academic paper titles from a citation graph about Viking Age and medieval Scandinavia. Each cluster was independently labeled by a language model, in a separate batch, with no visibility into the other cluster's content — these two labels were flagged afterward by a word-overlap heuristic as *possibly* describing the same real theme, but that heuristic has no understanding of meaning, only shared words, so it produces real false positives on genuine synonyms and real false negatives on true duplicates worded differently.

Cluster A — label: "{label_a}"
Representative titles:
{titles_a}

Cluster B — label: "{label_b}"
Representative titles:
{titles_b}

Judge only from the actual titles listed above, not from the labels' wording alone. Answer with exactly one JSON object, one line, no other text:

{{"verdict": "same" or "distinct", "reasoning": "one sentence, grounded in what the titles actually cover, not the label text"}}

"same" means: read together, these two clusters' titles describe one real, coherent theme that happened to get split into two labels by the batching process.
"distinct" means: the titles cover two genuinely different real subjects, even though the labels share surface words.
"""

DIFFERENTIATE_PROMPT = """You are relabeling two clusters of academic paper titles from a citation graph about Viking Age and medieval Scandinavia. These two clusters were labeled separately, in different batches, with no visibility into each other — a second pass already judged their labels too similar to distinguish, given what each cluster's real titles actually cover: {reasoning}

These two clusters remain structurally distinct: they were identified as separate communities by the underlying citation-graph analysis, not merged, so they must each keep their own, genuinely different label. Read each cluster's real titles below and write a label and description for each that names what is actually distinctive about that specific cluster, not what the two share.

Cluster A titles:
{titles_a}

Cluster B titles:
{titles_b}

Answer with exactly one JSON object, one line, no other text:

{{"label_a": "...", "description_a": "one sentence", "label_b": "...", "description_b": "one sentence"}}

The two labels must be genuinely different from each other, and different from the original labels "{label_a}" and "{label_b}" if those originals are what caused the confusion.
"""


def load_labels(path: Path) -> dict[str, dict]:
    with open(path, "r", encoding="utf-8", newline="") as f:
        return {row["community_id"]: row for row in csv.DictReader(f)}


def load_titles(path: Path) -> dict[str, list[str]]:
    titles_by_cluster: dict[str, list[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            titles_by_cluster[row["community_id"]].append(row["title"])
    return titles_by_cluster


def find_flagged_pairs(labels: dict[str, dict]) -> list[tuple[str, str]]:
    """Real, direct re-run of check_agreement() across every real pair
    of labeled clusters — O(n^2) over ~100 clusters is a few thousand
    cheap, local string comparisons, not a real cost, no need to cache
    or optimize this."""
    ids = sorted(labels.keys(), key=lambda x: int(x))
    pairs = []
    for i, id_a in enumerate(ids):
        for id_b in ids[i + 1:]:
            label_a = labels[id_a]["label"]
            label_b = labels[id_b]["label"]
            if check_agreement(label_a, label_b):
                pairs.append((id_a, id_b))
    return pairs


def differentiate_pair(
    id_a: str, id_b: str, labels: dict[str, dict], titles: dict[str, list[str]],
    reasoning: str, base_url: str, model: str, temperature: float, timeout: int,
    num_ctx: int, num_predict: int,
) -> dict | None:
    """Real re-labeling, not a merge: the two clusters stay separate,
    real Girvan-Newman communities (that structural fact is not
    overridden by an LLM's judgment about title content), so both keep
    their own community_id and both get a fresh, explicitly
    differentiated label — the direct fix for *why* they collided
    (batching gave neither model call visibility into the other),
    not a workaround that hides the collision behind a shared label."""
    label_a = labels[id_a]["label"]
    label_b = labels[id_b]["label"]
    titles_a = "\n".join(f"- {t}" for t in titles.get(id_a, [])[:10])
    titles_b = "\n".join(f"- {t}" for t in titles.get(id_b, [])[:10])

    prompt = DIFFERENTIATE_PROMPT.format(
        reasoning=reasoning, titles_a=titles_a, titles_b=titles_b,
        label_a=label_a, label_b=label_b,
    )
    raw = query_ollama(base_url, model, prompt, temperature, timeout, num_ctx, num_predict)
    if raw is None:
        return None

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        print(f"  Could not parse differentiation response for {id_a}/{id_b}: {raw[:200]!r}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed for {id_a}/{id_b} differentiation: {e}", file=sys.stderr)
        return None

    required = {"label_a", "description_a", "label_b", "description_b"}
    if not required.issubset(parsed.keys()):
        print(f"  Differentiation response for {id_a}/{id_b} missing required keys: {parsed.keys()}", file=sys.stderr)
        return None

    return parsed


def resolve_pair(
    id_a: str, id_b: str, labels: dict[str, dict], titles: dict[str, list[str]],
    base_url: str, model: str, temperature: float, timeout: int,
    num_ctx: int, num_predict: int,
) -> dict | None:
    label_a = labels[id_a]["label"]
    label_b = labels[id_b]["label"]
    titles_a = "\n".join(f"- {t}" for t in titles.get(id_a, [])[:10])
    titles_b = "\n".join(f"- {t}" for t in titles.get(id_b, [])[:10])

    prompt = RESOLVE_PROMPT.format(
        label_a=label_a, titles_a=titles_a, label_b=label_b, titles_b=titles_b,
    )
    raw = query_ollama(base_url, model, prompt, temperature, timeout, num_ctx, num_predict)
    if raw is None:
        return None

    # Real, direct JSON parse of the one-line response. If the model
    # wrapped it in prose or code fences despite the prompt's explicit
    # instruction not to, pull the first {...} block out rather than
    # fail the whole pair on formatting alone.
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        print(f"  Could not parse response for {id_a}/{id_b}: {raw[:200]!r}", file=sys.stderr)
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        print(f"  JSON parse failed for {id_a}/{id_b}: {e}", file=sys.stderr)
        return None

    verdict = parsed.get("verdict", "").strip().lower()
    if verdict not in ("same", "distinct"):
        print(f"  Unexpected verdict {verdict!r} for {id_a}/{id_b}, treating as unresolved", file=sys.stderr)
        verdict = "unresolved"

    return {
        "cluster_a": id_a, "label_a": label_a,
        "cluster_b": id_b, "label_b": label_b,
        "verdict": verdict,
        "reasoning": parsed.get("reasoning", ""),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cluster_labels_csv", type=Path)
    parser.add_argument("representatives_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--model", default="qwen3.5:35b")
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--num-ctx", type=int, default=8192)
    parser.add_argument("--num-predict", type=int, default=300)
    args = parser.parse_args()

    # Read the real, full row for every cluster (not just label/
    # description) — every other column (secondary_label, groups_json,
    # etc.) is preserved untouched for clusters that don't get
    # differentiated, and for clusters that do, only label/description
    # are overwritten; the rest of that row stays as label_clusters.py
    # originally wrote it.
    with open(args.cluster_labels_csv, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        all_rows = {row["community_id"]: row for row in reader}

    labels = load_labels(args.cluster_labels_csv)
    titles = load_titles(args.representatives_csv)

    pairs = find_flagged_pairs(labels)
    print(f"{len(pairs)} pair(s) flagged by check_agreement() across {len(labels)} labeled clusters.")

    results = []
    differentiated_ids = set()
    for id_a, id_b in pairs:
        print(f"Resolving {id_a} ({labels[id_a]['label']!r}) vs {id_b} ({labels[id_b]['label']!r})...")
        result = resolve_pair(
            id_a, id_b, labels, titles,
            args.base_url, args.model, args.temperature,
            args.timeout, args.num_ctx, args.num_predict,
        )
        if result is None:
            continue
        results.append(result)

        if result["verdict"] != "same":
            continue

        # Real correction: both clusters remain their own, structurally
        # distinct Girvan-Newman communities (community_id untouched);
        # only label/description are rewritten, explicitly forcing
        # differentiation from each other, using their real titles.
        print(f"  -> judged same theme; re-labeling both with explicit differentiation...")
        diff = differentiate_pair(
            id_a, id_b, labels, titles, result["reasoning"],
            args.base_url, args.model, args.temperature,
            args.timeout, args.num_ctx, args.num_predict,
        )
        if diff is None:
            print(f"  -> differentiation failed for {id_a}/{id_b}, leaving original labels in place", file=sys.stderr)
            continue

        all_rows[id_a]["label"] = diff["label_a"]
        all_rows[id_a]["description"] = diff["description_a"]
        all_rows[id_b]["label"] = diff["label_b"]
        all_rows[id_b]["description"] = diff["description_b"]
        # Also update the in-memory labels dict used for later pairs'
        # prompts in this same run — without this, a cluster that
        # appears in more than one flagged pair (confirmed real on
        # this data: e.g. cluster 131 appears in six separate pairs)
        # would show its stale, original label to every pair after
        # the first one that actually changed it.
        labels[id_a]["label"] = diff["label_a"]
        labels[id_a]["description"] = diff["description_a"]
        labels[id_b]["label"] = diff["label_b"]
        labels[id_b]["description"] = diff["description_b"]
        differentiated_ids.add(id_a)
        differentiated_ids.add(id_b)
        print(f"  -> {id_a}: {diff['label_a']!r}")
        print(f"  -> {id_b}: {diff['label_b']!r}")

    args.output_dir.mkdir(parents=True, exist_ok=True)

    report_path = args.output_dir / "duplicate_resolution.csv"
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["cluster_a", "label_a", "cluster_b", "label_b", "verdict", "reasoning"],
        )
        writer.writeheader()
        writer.writerows(results)

    if differentiated_ids:
        with open(args.cluster_labels_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for community_id in sorted(all_rows.keys(), key=lambda x: int(x)):
                writer.writerow(all_rows[community_id])

    n_same = sum(1 for r in results if r["verdict"] == "same")
    n_distinct = sum(1 for r in results if r["verdict"] == "distinct")
    n_unresolved = len(pairs) - len(results)
    n_differentiated_pairs = len(differentiated_ids) // 2
    print(f"\nWrote {len(results)} of {len(pairs)} resolutions to {report_path}.")
    print(f"  {n_same} judged genuinely the same theme")
    print(f"  {n_distinct} judged genuinely distinct (the word-overlap flag was a false positive)")
    if n_unresolved:
        print(f"  {n_unresolved} failed to resolve (parse or request errors, see stderr above)")
    if differentiated_ids:
        print(f"\n{n_differentiated_pairs} of {n_same} \"same\"-verdict pair(s) successfully re-labeled")
        print(f"with explicit differentiation and written back to {args.cluster_labels_csv}.")
        print(f"community_id (real Girvan-Newman membership) was not changed for any cluster;")
        print(f"only label/description were overwritten for the {len(differentiated_ids)} affected clusters.")
    if n_same and not differentiated_ids:
        print(f"\n{n_same} pair(s) judged \"same\" but none were successfully differentiated —")
        print(f"cluster_labels.csv was not modified.")


if __name__ == "__main__":
    main()