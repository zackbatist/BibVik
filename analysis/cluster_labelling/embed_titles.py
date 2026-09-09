"""
embed_titles.py — Embed every active entry's title with a multilingual
sentence-embedding model.

Standalone step, not part of the Quarto render chain (same convention as
gn_analysis/: this can take a while on the full corpus and has its own
Python dependency, sentence-transformers, that the main bibvik package
does not carry).

Input:  data/bibliography.json
Output: analysis/cluster_labelling/results/title_embeddings.npz
        (order-aligned arrays: name, title, embedding)

Titles only, no abstract text mixed in — F1 and F2 entries are embedded
on equal footing rather than giving F1 entries an unfair richer signal
purely because more of them happen to have an abstract (abstract exists
on only 232 of 14,328 active entries, 1.6% of the corpus).

Usage:
    cd analysis/cluster_labelling
    python embed_titles.py ../../data/bibliography.json results/
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

MODEL_NAME = "paraphrase-multilingual-mpnet-base-v2"


def load_active_titles(bibliography_path: Path) -> tuple[list[str], list[str]]:
    """Return (names, titles) for active (non-deleted) entries with a
    non-empty title. Ghost nodes have no bibliographic metadata and are
    not in this file at all, so no separate filtering is needed for them."""
    with open(bibliography_path, "r", encoding="utf-8") as f:
        bib = json.load(f)

    names, titles = [], []
    for name, entry in bib.items():
        if entry.get("_deleted"):
            continue
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        names.append(name)
        titles.append(title)
    return names, titles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bibliography_json", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--model", default=MODEL_NAME,
        help=f"sentence-transformers model name (default: {MODEL_NAME})",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    names, titles = load_active_titles(args.bibliography_json)
    print(f"Loaded {len(names)} active entries with a usable title.", file=sys.stderr)
    if not names:
        print("No titles to embed — aborting.", file=sys.stderr)
        sys.exit(1)

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print(
            "sentence-transformers is not installed. Install it with:\n"
            "    pip install sentence-transformers",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Loading model {args.model}...", file=sys.stderr)
    model = SentenceTransformer(args.model)

    print("Encoding titles...", file=sys.stderr)
    embeddings = model.encode(
        titles, show_progress_bar=True, normalize_embeddings=True
    )

    out_path = args.output_dir / "title_embeddings.npz"
    np.savez_compressed(
        out_path,
        names=np.array(names, dtype=object),
        titles=np.array(titles, dtype=object),
        embeddings=embeddings.astype(np.float32),
        model=np.array(args.model),
    )
    print(f"Wrote {len(names)} embeddings ({embeddings.shape[1]}-dim) to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
