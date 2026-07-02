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
import re
from pathlib import Path

logger = logging.getLogger(__name__)

CORRECTIONS_FILENAME = "corrections.yaml"


# ── Citekey regeneration ──────────────────────────────────────────────────────
#
# generate_citekey() in utils.py relies on a module-level, in-process
# disambiguation registry (_citekey_registry) that is populated during
# ingestion (--iterate-f1) and does not persist across runs. It is not
# available here, and calling it fresh from corrections.py would have no
# knowledge of which lastnameyear keys already exist in the bibliography —
# risking a silent collision with a real, unrelated entry. Instead this
# regenerates a citekey using the same lastnameyear + a/b/c scheme, but
# checks uniqueness directly against the live bibliography dict.

def _slugify_family(family: str) -> str:
    """Match utils.generate_citekey's family-name slugification."""
    from unidecode import unidecode
    slug = unidecode(family).lower()
    return re.sub(r"[^a-z]", "", slug)


def _disambiguation_suffix(count: int) -> str:
    """Match utils.generate_citekey's a, b, ..., z, aa, ab, ... suffix scheme."""
    if count <= 26:
        return chr(ord("a") + count - 1)
    first = chr(ord("a") + (count - 27) // 26)
    second = chr(ord("a") + (count - 27) % 26)
    return first + second


def regenerate_citekey_for_author(bib: dict, old_citekey: str) -> str | None:
    """
    Given an entry whose author field was just corrected, regenerate a
    lastnameyear-style citekey if the entry currently has a NOAUTHOR-style
    key and now has a usable family name. Returns the new citekey, or None
    if regeneration isn't applicable (no NOAUTHOR key, or still no usable
    family name after the correction).

    Does not mutate bib — the caller (apply_corrections) is responsible
    for performing the rename and remapping cited_by references, since
    only it knows whether the rename should proceed (e.g. after checking
    the entry actually exists).
    """
    if not old_citekey.startswith("NOAUTHOR"):
        return None

    entry = bib.get(old_citekey)
    if entry is None:
        return None

    authors = entry.get("author", [])
    if not authors or not authors[0].get("family"):
        return None

    family = _slugify_family(authors[0]["family"])
    if not family:
        return None

    year = str(entry.get("year", "")).strip()[:4]
    base = f"{family}{year}" if year else f"{family}nd"

    if base not in bib:
        return base

    # base collides with an existing citekey — disambiguate against
    # every existing key that starts with base (base, basea, baseb, ...)
    count = 1
    while True:
        candidate = base if count == 1 else f"{base}{_disambiguation_suffix(count - 1)}"
        if candidate not in bib:
            return candidate
        count += 1


def _rename_citekey(bib: dict, old_citekey: str, new_citekey: str, note: str) -> None:
    """
    Rename a bibliography entry's citekey in place: move the entry to the
    new key and remap every cited_by reference to the old key across the
    whole bibliography, so no edges are silently orphaned (same remapping
    pattern used for merge).
    """
    entry = bib.pop(old_citekey)
    entry["_renamed_from"] = old_citekey
    entry["_rename_note"] = note
    bib[new_citekey] = entry

    for other in bib.values():
        cb_list = other.get("cited_by", [])
        if old_citekey in cb_list:
            other["cited_by"] = [new_citekey if x == old_citekey else x for x in cb_list]

    logger.info("Renamed %r to %r", old_citekey, new_citekey)


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
    counts = {"merge": 0, "delete": 0, "set": 0, "skipped": 0, "citekey_regenerated": 0}

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
                if keep.startswith("NOAUTHOR"):
                    logger.warning(
                        "Correction %d (merge): keep citekey %r not found — if this "
                        "was renamed by an earlier author-set correction in this same "
                        "run, update this correction to use the new citekey", i, keep
                    )
                else:
                    logger.warning("Correction %d (merge): keep citekey %r not found", i, keep)
                counts["skipped"] += 1
                continue
            if discard not in bib:
                if discard.startswith("NOAUTHOR"):
                    logger.warning(
                        "Correction %d (merge): discard citekey %r not found — if this "
                        "was renamed by an earlier author-set correction in this same "
                        "run, update this correction to use the new citekey", i, discard
                    )
                else:
                    logger.debug("Correction %d (merge): discard %r already absent", i, discard)
                continue

            for cb in bib[discard].get("cited_by", []):
                if cb not in bib[keep].get("cited_by", []):
                    bib[keep].setdefault("cited_by", []).append(cb)

            # Remap all cited_by references from discard to keep
            for entry in bib.values():
                cb_list = entry.get("cited_by", [])
                if discard in cb_list:
                    entry["cited_by"] = [keep if x == discard else x for x in cb_list]

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

            # If this set corrected the author field on a NOAUTHOR entry,
            # regenerate its citekey so it stops being stuck as NOAUTHOR*
            # forever. Must happen after the field is set (regeneration
            # reads the new author value) and must remap cited_by
            # references, since other entries may already point at the
            # old citekey.
            if field == "author":
                new_citekey = regenerate_citekey_for_author(bib, citekey)
                if new_citekey:
                    _rename_citekey(
                        bib, citekey, new_citekey,
                        note=f"Citekey regenerated after author correction: {note}",
                    )
                    counts["citekey_regenerated"] = counts.get("citekey_regenerated", 0) + 1

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
        return {"merge": 0, "delete": 0, "set": 0, "skipped": 0, "citekey_regenerated": 0}

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
          f"set={counts['set']}  skipped={counts['skipped']}  "
          f"citekey_regenerated={counts['citekey_regenerated']}")