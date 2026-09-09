"""
label_clusters.py — LLM labeling, as a distinct, downstream step
structurally separate from the embedding/clustering step (mechanical
structure first, interpretive label applied to its output after, never
the reverse).

The prompt supplies only the representative titles for one cluster at a
time and asks for a short thematic description in the model's own
words. It does not mention the annotation codebook, its categories, or
any example labels drawn from it, in any form — this is the concrete
mechanism by which "independent of the codebook" is enforced.

Stability check: the labeling prompt is run across multiple
model/temperature/seed combinations. Labels that shift substantially
across those runs are flagged as unstable for that cluster rather than
averaged or arbitrarily picked. Stability is judged by embedding each
run's label text and checking pairwise cosine similarity against a
threshold — the same embedding model as embed_titles.py, reused here
for a different purpose (comparing short label strings rather than
titles).

Human review of every generated label against the actual member titles
is the next step after this one, done outside this script (the CITF
precedent's "reviewed and refined by the research team to ensure face
validity" step) — this script's output is a candidate label set for
that review, not a final result.

Input:  results/representatives.csv (from select_representatives.py)
Output: results/candidate_labels.csv
        (community_id, run_id, model, temperature, seed, label,
         description, stable)

Usage:
    ollama serve   # if not already running
    python label_clusters.py results/representatives.csv results/ \\
        --models qwen3:35b qwen3:8b --temperatures 0.3 0.7 --seeds 1 2
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import requests

LABEL_PROMPT = """You are analyzing a cluster of academic paper titles from a citation graph. These titles were grouped together by an automated clustering method based on semantic similarity, not by topic labels or any predefined taxonomy.

## Titles in this cluster

{titles_block}

## Task

Write a short thematic description of what unites these titles, in your own words. Do not assume the titles share a single narrow topic — describe the actual pattern you see, even if it is broad, mixed, or only loosely coherent.

Respond in JSON format with exactly these fields:
{{
  "label": "<a short phrase, 2-6 words, naming the theme>",
  "description": "<1-3 sentences elaborating on the theme and what, if anything, ties the titles together>"
}}

Respond ONLY with the JSON object. No preamble, no markdown fences."""


def load_representatives(path: Path) -> dict[str, list[str]]:
    titles_by_cluster: dict[str, list[str]] = defaultdict(list)
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            titles_by_cluster[row["community_id"]].append(row["title"])
    return titles_by_cluster


def query_ollama(
    base_url: str, model: str, prompt: str, temperature: float,
    seed: int, timeout: int = 300,
) -> str | None:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": temperature, "seed": seed, "num_predict": 512},
    }
    try:
        resp = requests.post(f"{base_url}/api/generate", json=payload, timeout=timeout)
    except requests.RequestException as e:
        print(f"  request failed: {e}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"  Ollama returned status {resp.status_code}: {resp.text[:300]}", file=sys.stderr)
        return None
    text = resp.json().get("response", "")
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    return text or None


def parse_label_json(raw: str) -> dict | None:
    # Strip markdown fences defensively, even though the prompt asks for none.
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if "label" not in obj or "description" not in obj:
        return None
    return obj


def check_stability(labels: list[str], embed_model, threshold: float = 0.6) -> bool:
    """True if every pairwise cosine similarity among this cluster's run
    labels meets the threshold. A single run trivially passes."""
    if len(labels) < 2:
        return True
    vecs = embed_model.encode(labels, normalize_embeddings=True)
    sims = vecs @ vecs.T
    n = len(labels)
    pairwise = [sims[i, j] for i in range(n) for j in range(i + 1, n)]
    return min(pairwise) >= threshold


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("representatives_csv", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--models", nargs="+", default=["qwen3:35b"])
    parser.add_argument("--temperatures", nargs="+", type=float, default=[0.3, 0.7])
    parser.add_argument("--seeds", nargs="+", type=int, default=[1, 2])
    parser.add_argument(
        "--stability-threshold", type=float, default=0.6,
        help="Minimum pairwise cosine similarity among a cluster's run "
             "labels to be treated as stable (default: 0.6)",
    )
    parser.add_argument(
        "--embed-model", default="paraphrase-multilingual-mpnet-base-v2",
        help="Model used only to compare label strings for stability, "
             "not for the original title embedding",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    titles_by_cluster = load_representatives(args.representatives_csv)
    print(f"{len(titles_by_cluster)} clusters to label.", file=sys.stderr)

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print(
            "sentence-transformers is not installed (needed for the "
            "stability check). Install it with:\n"
            "    pip install sentence-transformers",
            file=sys.stderr,
        )
        sys.exit(1)
    embed_model = SentenceTransformer(args.embed_model)

    runs = [
        (model, temp, seed)
        for model in args.models
        for temp in args.temperatures
        for seed in args.seeds
    ]
    print(f"{len(runs)} run(s) per cluster: {runs}", file=sys.stderr)

    all_rows = []
    for community_id, titles in titles_by_cluster.items():
        titles_block = "\n".join(f"- {t}" for t in titles)
        prompt = LABEL_PROMPT.format(titles_block=titles_block)

        cluster_labels = []
        cluster_rows = []
        for run_id, (model, temperature, seed) in enumerate(runs, start=1):
            print(f"cluster {community_id}, run {run_id}/{len(runs)} "
                  f"({model}, T={temperature}, seed={seed})...", file=sys.stderr)
            raw = query_ollama(args.base_url, model, prompt, temperature, seed)
            parsed = parse_label_json(raw) if raw else None
            if parsed is None:
                print(f"  no valid JSON label returned, skipping this run", file=sys.stderr)
                continue
            cluster_labels.append(parsed["label"])
            cluster_rows.append({
                "community_id": community_id,
                "run_id": run_id,
                "model": model,
                "temperature": temperature,
                "seed": seed,
                "label": parsed["label"],
                "description": parsed["description"],
            })

        stable = check_stability(cluster_labels, embed_model, args.stability_threshold) if cluster_labels else False
        for row in cluster_rows:
            row["stable"] = stable
        all_rows.extend(cluster_rows)

    out_path = args.output_dir / "candidate_labels.csv"
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "community_id", "run_id", "model", "temperature", "seed",
                "label", "description", "stable",
            ],
        )
        writer.writeheader()
        writer.writerows(all_rows)

    n_unstable = len({r["community_id"] for r in all_rows if not r["stable"]})
    print(f"Wrote {len(all_rows)} candidate labels to {out_path}. "
          f"{n_unstable} cluster(s) flagged unstable — review those labels "
          f"individually before accepting any of them.")


if __name__ == "__main__":
    main()
