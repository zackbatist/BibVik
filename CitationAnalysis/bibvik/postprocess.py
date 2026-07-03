"""
postprocess.py — Post-enrichment LLM cleaning passes for bibliography.json.

Runs after --enrich. Applies passes that require the full enriched bibliography
and benefit from LLM inference. Per-entry normalization (title cleanup, date/DOI/
pages, entry type for misc) happens inline in normalize.py during graph construction.

Passes:
    1. Entry type reclassification (all types, using enriched fields)
    2. LLM entry type classification (ambiguous cases)
    3. LLM compound citation splitting
    4. Near-duplicate resolution (LLM, title-rich entries only)

Usage:
    python3 run.py --postprocess
"""

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

# Titles too generic/short to be useful for near-duplicate matching
_TRIVIAL_TITLES = frozenset({
    "introduction", "conclusion", "conclusions", "preface", "foreword",
    "abstract", "summary", "notes", "bibliography", "references",
    "index", "appendix", "appendices", "chapter", "part", "section",
    "review", "overview", "discussion", "results", "methods", "methodology",
    "afterword", "epilogue", "prologue", "acknowledgements", "acknowledgments",
})


# ── Pass 1: Entry type reclassification (post-enrich) ────────────────────────

def fix_entry_types_post_enrich(bib: dict) -> int:
    """
    Reclassify entry types using enriched fields. Only reclassifies entries
    where the evidence is unambiguous. Does not touch incollection→inbook.
    """
    count = 0
    for entry in bib.values():
        old_type  = entry.get("entry_type", "")
        journal   = entry.get("journaltitle", "").strip()
        booktitle = entry.get("booktitle", "").strip()
        editors   = entry.get("editor", [])
        volume    = entry.get("volume", "").strip()
        number    = entry.get("number", "").strip()
        pages     = entry.get("pages", "").strip()
        title     = entry.get("title", "").strip()

        # Pages must be a range for article classification —
        # single numbers are often monograph series volume numbers
        pages_is_range = bool(re.search(r"\d+\s*[-–]+\s*\d+", pages))

        if journal and (volume or number or pages_is_range):
            new_type = "article"
        elif booktitle and editors:
            new_type = "incollection"
        else:
            continue

        # Don't reclassify book→inbook/incollection when booktitle matches title
        if old_type == "book" and new_type in ("inbook", "incollection") and title and booktitle:
            t_norm  = re.sub(r"\W", "", title.lower())
            bt_norm = re.sub(r"\W", "", booktitle.lower())
            if t_norm and bt_norm and (t_norm == bt_norm or t_norm in bt_norm or bt_norm in t_norm):
                continue

        if new_type != old_type:
            entry["entry_type"] = new_type
            entry.setdefault("_entry_type_original", old_type)
            count += 1
    return count


# ── Pass 2: Near-duplicate flagging and LLM resolution ───────────────────────

def _title_tokens(title: str) -> set:
    return set(re.findall(r"\b\w{4,}\b", title.lower()))

def _is_trivial_title(title: str) -> bool:
    return title.strip().lower() in _TRIVIAL_TITLES or len(title.strip()) < 20

def _first_author_key(entry: dict) -> str:
    from unidecode import unidecode
    authors = entry.get("author", [])
    if not authors:
        return ""
    return unidecode(authors[0].get("family", "")).lower().strip()


def flag_near_duplicates(bib: dict, llm_config: dict | None = None) -> int:
    """
    Flag and optionally LLM-resolve near-duplicate entries.
    Only operates on title-rich pairs (both have substantive titles).
    Trivial titles are skipped.
    """
    count = 0
    index: dict[tuple, list] = defaultdict(list)

    for ck, entry in bib.items():
        year = entry.get("date", entry.get("year", ""))[:4]
        ak   = _first_author_key(entry)
        if year and ak:
            index[(year, ak)].append(ck)

    for citekeys in index.values():
        if len(citekeys) < 2:
            continue
        pairs = [
            (citekeys[i], citekeys[j])
            for i in range(len(citekeys))
            for j in range(i + 1, len(citekeys))
        ]
        for ck_a, ck_b in pairs:
            ta = bib[ck_a].get("title", "")
            tb = bib[ck_b].get("title", "")

            # Skip trivial or missing titles
            if not ta or not tb or _is_trivial_title(ta) or _is_trivial_title(tb):
                continue

            ta_tokens = _title_tokens(ta)
            tb_tokens = _title_tokens(tb)
            if not ta_tokens or not tb_tokens:
                continue

            overlap = len(ta_tokens & tb_tokens) / min(len(ta_tokens), len(tb_tokens))
            if overlap >= 0.7:
                if llm_config:
                    # LLM resolution: ask if they're the same work
                    same = _llm_same_work(ta, tb, bib[ck_a], bib[ck_b], llm_config)
                    if same is True:
                        # Merge: keep ck_a, update cited_by
                        for cb in bib[ck_b].get("cited_by", []):
                            if cb not in bib[ck_a].get("cited_by", []):
                                bib[ck_a].setdefault("cited_by", []).append(cb)
                        bib[ck_b]["_merged_into"] = ck_a
                        count += 1
                        continue
                    elif same is False:
                        continue
                # LLM unavailable or inconclusive — flag for human review
                bib[ck_a].setdefault("_near_duplicate_candidate", [])
                if ck_b not in bib[ck_a]["_near_duplicate_candidate"]:
                    bib[ck_a]["_near_duplicate_candidate"].append(ck_b)
                    count += 1
                bib[ck_b].setdefault("_near_duplicate_candidate", [])
                if ck_a not in bib[ck_b]["_near_duplicate_candidate"]:
                    bib[ck_b]["_near_duplicate_candidate"].append(ck_a)

    return count


def _llm_same_work(
    title_a: str, title_b: str,
    entry_a: dict, entry_b: dict,
    llm_config: dict,
) -> bool | None:
    """Ask LLM if two entries are the same work. Returns True/False/None."""
    import requests

    prompt = (
        "You are an expert bibliographer. Are the following two bibliography entries "
        "the same published work? Consider title, author, and year. "
        "Respond with only 'yes' or 'no'.\n\n"
        f"Entry A:\n  Title: {title_a}\n  Author: {entry_a.get('author', [{}])[0].get('family', '')}\n  Year: {entry_a.get('date', '')}\n\n"
        f"Entry B:\n  Title: {title_b}\n  Author: {entry_b.get('author', [{}])[0].get('family', '')}\n  Year: {entry_b.get('date', '')}\n\n"
        "/no_think"
    )

    try:
        base_url = llm_config.get("base_url", "http://localhost:11434")
        model    = llm_config.get("model", "qwen2.5:7b")
        timeout  = llm_config.get("timeout", 30)
        backend  = llm_config.get("backend", "ollama")

        if backend == "ollama":
            resp = requests.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "think": False, "options": {"temperature": 0.0, "num_predict": 10}},
                timeout=timeout,
            )
            raw = resp.json().get("response", "").strip().lower()
        else:
            resp = requests.post(
                f"{base_url}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "temperature": 0.0, "max_tokens": 10},
                timeout=timeout,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip().lower()

        if raw.startswith("yes"):
            return True
        if raw.startswith("no"):
            return False
    except Exception:
        pass
    return None




# ── Pass 2: Author recovery from raw citation string ─────────────────────────

_LLM_AUTHOR_RECOVERY = (
    "You are an expert bibliographer. The following is a raw citation string "
    "from an academic bibliography. Extract the author name(s) as structured data.\n\n"
    "Raw citation:\n{raw}\n\n"
    "Respond with a JSON array of author objects, each with \'family\' and \'given\' keys.\n"
    "Example: [{{\"family\": \"Sindbæk\", \"given\": \"Søren Michael\"}}]\n"
    "If you cannot identify any authors, respond with an empty array: []\n"
    "Respond with only the JSON array, no other text.\n"
    "/no_think"
)


def recover_authors_from_raw(bib: dict, llm_config: dict | None = None) -> int:
    import json as _json
    import requests
    if not llm_config:
        return 0
    count = 0
    for ck, entry in bib.items():
        if entry.get("_deleted"):
            continue
        if entry.get("author"):
            continue
        if not ck.startswith("NOAUTHOR"):
            continue
        raw = entry.get("_raw_citation", "").strip()
        if not raw or not entry.get("title", "").strip():
            continue
        prompt = _LLM_AUTHOR_RECOVERY.format(raw=raw)
        try:
            base_url = llm_config.get("base_url", "http://localhost:11434")
            model    = llm_config.get("model", "qwen2.5:7b")
            timeout  = llm_config.get("timeout", 60)
            backend  = llm_config.get("backend", "ollama")
            if backend == "ollama":
                resp = requests.post(f"{base_url}/api/generate",
                    json={"model": model, "prompt": prompt, "stream": False,
                          "think": False, "options": {"temperature": 0.0, "num_predict": 200}},
                    timeout=timeout)
                raw_resp = resp.json().get("response", "").strip()
            else:
                resp = requests.post(f"{base_url}/v1/chat/completions",
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "stream": False, "temperature": 0.0, "max_tokens": 200},
                    timeout=timeout)
                raw_resp = resp.json()["choices"][0]["message"]["content"].strip()
            raw_resp = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_resp, flags=re.MULTILINE).strip()
            authors = _json.loads(raw_resp)
            if isinstance(authors, list) and authors:
                valid = [a for a in authors if isinstance(a, dict) and a.get("family", "").strip()]
                if valid:
                    entry["author"] = valid
                    entry["_author_recovered"] = True
                    count += 1
                    logger.info("Recovered author(s) for %s: %s", ck, valid)
                    continue
        except Exception as exc:
            logger.debug("Author recovery LLM error for %s: %s", ck, exc)
        entry["_author_recovery_failed"] = True
    return count


# ── Pass 3: Title recovery from raw citation string ───────────────────────────

_LLM_TITLE_RECOVERY = (
    "You are an expert bibliographer. The following is a raw citation string "
    "from an academic bibliography. Extract the title of the work being cited.\n\n"
    "Raw citation:\n{raw}\n\n"
    "Respond with only the title as plain text, exactly as it appears in the "
    "citation. Do not include authors, year, journal, or any other fields. "
    "If you cannot identify a title, respond with an empty string.\n"
    "/no_think"
)


def recover_titles_from_raw(bib: dict, llm_config: dict | None = None) -> int:
    import requests
    if not llm_config:
        return 0
    count = 0
    for ck, entry in bib.items():
        if entry.get("_deleted"):
            continue
        if entry.get("title", "").strip():
            continue
        raw = entry.get("_raw_citation", "").strip()
        if not raw:
            continue
        prompt = _LLM_TITLE_RECOVERY.format(raw=raw)
        try:
            base_url = llm_config.get("base_url", "http://localhost:11434")
            model    = llm_config.get("model", "qwen2.5:7b")
            timeout  = llm_config.get("timeout", 60)
            backend  = llm_config.get("backend", "ollama")
            if backend == "ollama":
                resp = requests.post(f"{base_url}/api/generate",
                    json={"model": model, "prompt": prompt, "stream": False,
                          "think": False, "options": {"temperature": 0.0, "num_predict": 100}},
                    timeout=timeout)
                title = resp.json().get("response", "").strip()
            else:
                resp = requests.post(f"{base_url}/v1/chat/completions",
                    json={"model": model, "messages": [{"role": "user", "content": prompt}],
                          "stream": False, "temperature": 0.0, "max_tokens": 100},
                    timeout=timeout)
                title = resp.json()["choices"][0]["message"]["content"].strip()
            title = title.strip('"\'').strip()
            if title and len(title) > 3:
                entry["title"] = title
                entry["_title_recovered"] = True
                count += 1
                logger.info("Recovered title for %s: %s", ck, title[:60])
        except Exception as exc:
            logger.debug("Title recovery LLM error for %s: %s", ck, exc)
    return count


# ── Main ─────────────────────────────────────────────────────────────────────

PASSES = [
    ("Entry type reclassification (enriched)", fix_entry_types_post_enrich, False),
    ("Author recovery from raw citation string",  recover_authors_from_raw,  True),
    ("Title recovery from raw citation string",   recover_titles_from_raw,   True),
]


def run_postprocess(
    input_path: Path,
    output_path: Path | None = None,
    llm_config: dict | None = None,
    project_root: Path | None = None,
) -> dict:
    """Run all post-enrichment passes on bibliography.json."""
    from .corrections import run_corrections, CORRECTIONS_FILENAME

    input_path  = Path(input_path)
    output_path = Path(output_path) if output_path else input_path

    bib = json.loads(input_path.read_text(encoding="utf-8"))
    total = len(bib)
    logger.info("Loaded %d entries from %s.", total, input_path)

    results = {}

    # Pass 0: manual corrections (merge, delete, set) from corrections.yaml
    # corrections.yaml lives in the project root, passed explicitly or inferred
    project_root = Path(project_root) if project_root else input_path.parent.parent
    correction_counts = run_corrections(input_path, project_root)
    n_corrections = correction_counts["merge"] + correction_counts["delete"] + correction_counts["set"] + correction_counts.get("split", 0)
    if n_corrections or (project_root / CORRECTIONS_FILENAME).exists():
        results["Manual corrections"] = n_corrections
        logger.info("Manual corrections: %d applied", n_corrections)
        # Reload bib after corrections were written back
        bib = json.loads(input_path.read_text(encoding="utf-8"))

    for name, fn, needs_llm in PASSES:
        if needs_llm:
            count = fn(bib, llm_config)
        else:
            count = fn(bib)
        logger.info("%-50s  %4d / %d", name, count, total)
        results[name] = count

    output_path.write_text(
        json.dumps(bib, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Written to %s", output_path)

    # Append pipeline-generated draft candidates to corrections.yaml
    from .corrections import append_draft_corrections, load_yaml, CORRECTIONS_FILENAME
    corrections_path = project_root / CORRECTIONS_FILENAME
    existing = load_yaml(corrections_path)
    n_drafts = append_draft_corrections(bib, corrections_path, existing)
    if n_drafts:
        results["Draft corrections appended"] = n_drafts

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",   default="bibliography.json")
    parser.add_argument("--output",  default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_postprocess(Path(args.input), Path(args.output) if args.output else None)