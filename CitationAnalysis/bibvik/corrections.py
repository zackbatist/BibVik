"""
bibvik.corrections — Manual curation corrections for bibliography.json.

One file: corrections.yaml in the project root.

The pipeline appends draft candidates (marked _draft: true) after each
--postprocess run. The researcher reviews, removes _draft: true to confirm,
and deletes rejected entries. Confirmed entries are applied on the next run.

Actions:

    - action: merge
      keep: citekey_a
      discard: citekey_b
      note: "reason (required)"

    - action: delete
      citekey: citekey_a
      note: "reason (required)"

    - action: set
      citekey: citekey_a
      field: author
      value: [{family: Sindbæk, given: Søren Michael}]
      note: "reason (required)"

Draft candidates have _draft: true and additional context fields (_source,
_confidence, _keep_title, etc.) to aid review. Remove _draft: true and
fill in the note to confirm. Delete the entry to reject.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CORRECTIONS_FILENAME = "corrections.yaml"


# ── YAML loading ──────────────────────────────────────────────────────────────

def load_yaml(path: Path) -> list[dict]:
    """Load a YAML file as a list. Returns [] if absent or empty."""
    if not path.exists():
        return []
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; cannot load %s. pip install pyyaml", path)
        return []
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data:
        return []
    if not isinstance(data, list):
        logger.error("%s must be a YAML list; got %s", path, type(data).__name__)
        return []
    return data


def save_yaml(path: Path, data: list[dict]) -> None:
    """Write a list of dicts to a YAML file."""
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed; cannot write %s", path)
        return
    with path.open("w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)


# ── Apply corrections ─────────────────────────────────────────────────────────

def apply_corrections(bib: dict, corrections: list[dict]) -> dict:
    """
    Apply confirmed corrections (those without _draft: true) to the
    bibliography in place. Returns counts: {merge, delete, set, skipped}.
    """
    counts = {"merge": 0, "delete": 0, "set": 0, "skipped": 0}

    for i, corr in enumerate(corrections):
        if corr.get("_draft"):
            continue

        action = corr.get("action", "").lower()
        note   = corr.get("note", "")

        if not note:
            logger.warning("Correction %d (%s): missing note — skipping", i, action)
            counts["skipped"] += 1
            continue

        if action == "merge":
            keep    = corr.get("keep", "")
            discard = corr.get("discard", "")

            if not keep or not discard:
                logger.warning("Correction %d (merge): missing keep or discard", i)
                counts["skipped"] += 1
                continue
            if keep not in bib:
                logger.warning("Correction %d (merge): keep citekey %r not found", i, keep)
                counts["skipped"] += 1
                continue
            if discard not in bib:
                logger.debug("Correction %d (merge): discard %r already absent", i, discard)
                continue

            for cb in bib[discard].get("cited_by", []):
                if cb not in bib[keep].get("cited_by", []):
                    bib[keep].setdefault("cited_by", []).append(cb)

            bib[discard]["_deleted"]         = True
            bib[discard]["_merged_into"]     = keep
            bib[discard]["_correction_note"] = note

            logger.info("Merged %r into %r", discard, keep)
            counts["merge"] += 1

        elif action == "delete":
            citekey = corr.get("citekey", "")

            if not citekey:
                logger.warning("Correction %d (delete): missing citekey", i)
                counts["skipped"] += 1
                continue
            if citekey not in bib:
                logger.debug("Correction %d (delete): %r already absent", i, citekey)
                continue

            bib[citekey]["_deleted"]         = True
            bib[citekey]["_correction_note"] = note

            logger.info("Deleted %r", citekey)
            counts["delete"] += 1

        elif action == "set":
            citekey = corr.get("citekey", "")
            field   = corr.get("field", "")
            value   = corr.get("value")

            if not citekey or not field:
                logger.warning("Correction %d (set): missing citekey or field", i)
                counts["skipped"] += 1
                continue
            if citekey not in bib:
                logger.warning("Correction %d (set): citekey %r not found", i, citekey)
                counts["skipped"] += 1
                continue
            if value is None:
                logger.warning("Correction %d (set): missing value", i)
                counts["skipped"] += 1
                continue

            bib[citekey][field] = value
            bib[citekey].setdefault("_corrections_applied", []).append(field)
            bib[citekey]["_correction_note"] = note

            logger.info("Set %r.%s", citekey, field)
            counts["set"] += 1

        else:
            logger.warning("Correction %d: unknown action %r", i, action)
            counts["skipped"] += 1

    return counts


# ── Draft generation ──────────────────────────────────────────────────────────

def append_draft_corrections(
    bib: dict,
    corrections_path: Path,
    existing_corrections: list[dict] | None = None,
) -> int:
    """
    Scan the bibliography for pipeline-flagged issues and append draft
    candidates to corrections.yaml (marked _draft: true).

    Skips candidates already present in corrections.yaml (confirmed or draft).

    Sources:
        _near_duplicate_candidate         — near-duplicate pairs, LLM inconclusive
        _cross_script_duplicate_candidate — cross-script pairs, titles don't overlap enough
        _author_recovery_failed           — NOAUTHOR entries where LLM couldn't extract author
                                            (flag must be set by postprocess.recover_authors_from_raw())
        _ocr_candidate                    — entries flagged as likely OCR garbage
                                            (flag must be set by grobid_client alternate OCR path)

    Returns number of draft entries appended.
    """
    existing_corrections = existing_corrections or []

    existing_merge_pairs = {
        frozenset([c.get("keep"), c.get("discard")])
        for c in existing_corrections
        if c.get("action") == "merge"
    }
    existing_set_targets = {
        (c.get("citekey"), c.get("field"))
        for c in existing_corrections
        if c.get("action") == "set"
    }
    existing_delete_keys = {
        c.get("citekey")
        for c in existing_corrections
        if c.get("action") == "delete"
    } | {
        c.get("discard")
        for c in existing_corrections
        if c.get("action") == "merge"
    }

    drafts = []
    seen_pairs: set[frozenset] = set()

    for ck, entry in bib.items():
        if entry.get("_deleted"):
            continue

        # Near-duplicate candidates
        for partner in entry.get("_near_duplicate_candidate", []):
            pair = frozenset([ck, partner])
            if pair in seen_pairs or pair in existing_merge_pairs:
                continue
            seen_pairs.add(pair)
            partner_entry = bib.get(partner, {})
            drafts.append({
                "action":         "merge",
                "keep":           ck,
                "discard":        partner,
                "note":           "",
                "_draft":         True,
                "_source":        "near_duplicate",
                "_confidence":    entry.get("_near_duplicate_score", 0.0),
                "_keep_title":    entry.get("title", ""),
                "_discard_title": partner_entry.get("title", ""),
            })

        # Cross-script duplicate candidates
        # Only generate drafts when at least one entry has a Cyrillic author —
        # Latin-only pairs are false positives from the transliteration comparison.
        for partner in entry.get("_cross_script_duplicate_candidate", []):
            pair = frozenset([ck, partner])
            if pair in seen_pairs or pair in existing_merge_pairs:
                continue
            seen_pairs.add(pair)
            partner_entry = bib.get(partner, {})

            def _has_cyrillic(e: dict) -> bool:
                for a in e.get("author", []):
                    name = a.get("family", "") + a.get("given", "")
                    if any("\u0400" <= c <= "\u04ff" for c in name):
                        return True
                return False

            if not (_has_cyrillic(entry) or _has_cyrillic(partner_entry)):
                continue

            drafts.append({
                "action":         "merge",
                "keep":           ck,
                "discard":        partner,
                "note":           "",
                "_draft":         True,
                "_source":        "cross_script",
                "_confidence":    0.5,
                "_keep_title":    entry.get("title", ""),
                "_discard_title": partner_entry.get("title", ""),
            })

        # Failed NOAUTHOR author recovery
        # Flag set by postprocess.recover_authors_from_raw() when LLM returns no author
        if entry.get("_author_recovery_failed"):
            if (ck, "author") not in existing_set_targets:
                drafts.append({
                    "action":        "set",
                    "citekey":       ck,
                    "field":         "author",
                    "value":         [],
                    "note":          "",
                    "_draft":        True,
                    "_source":       "noauthor_recovery",
                    "_confidence":   0.0,
                    "_raw_citation": entry.get("_raw_citation", ""),
                    "_title":        entry.get("title", ""),
                })

        # OCR garbage candidates
        # Flag set by grobid_client when alternate OCR still yields unresolvable entry
        if entry.get("_ocr_candidate"):
            if ck not in existing_delete_keys:
                drafts.append({
                    "action":        "delete",
                    "citekey":       ck,
                    "note":          "",
                    "_draft":        True,
                    "_source":       "ocr_candidate",
                    "_confidence":   0.3,
                    "_raw_citation": entry.get("_raw_citation", ""),
                    "_title":        entry.get("title", ""),
                })

    if drafts:
        all_corrections = existing_corrections + drafts
        save_yaml(corrections_path, all_corrections)
        logger.info("Appended %d draft corrections to %s", len(drafts), corrections_path)

    return len(drafts)


# ── Main entry point ──────────────────────────────────────────────────────────

def run_corrections(bib_path: Path, project_root: Path | None = None) -> dict:
    """
    Load bibliography.json, apply confirmed corrections from corrections.yaml,
    write back. Returns counts.
    """
    bib_path         = Path(bib_path)
    project_root     = Path(project_root) if project_root else Path.cwd()
    corrections_path = project_root / CORRECTIONS_FILENAME

    bib         = json.loads(bib_path.read_text(encoding="utf-8"))
    corrections = load_yaml(corrections_path)

    if not corrections:
        return {"merge": 0, "delete": 0, "set": 0, "skipped": 0}

    counts = apply_corrections(bib, corrections)
    bib_path.write_text(json.dumps(bib, ensure_ascii=False, indent=2), encoding="utf-8")
    return counts


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--bib",  default="output/bibliography.json")
    parser.add_argument("--root", default=".", help="Project root (where corrections.yaml lives)")
    args = parser.parse_args()
    counts = run_corrections(Path(args.bib), Path(args.root))
    print(f"merge={counts['merge']}  delete={counts['delete']}  "
          f"set={counts['set']}  skipped={counts['skipped']}")