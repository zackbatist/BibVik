"""
bibvik.biblatex_model — Completeness scoring for bibliographic records.

This module scores how complete a bibliography entry is relative to what's
expected for its entry type (article, book, incollection, etc.), producing
a 'completeness' field consumed by the audit report and coverage tooling.

Note: this module originally also defined the canonical record-building and
merge logic (normalize_record, merge_records, BIBLATEX_FIELD_ORDER) for
constructing bibliography entries from raw parsed references. That logic was
never wired into the actual pipeline — record construction and merging are
handled directly in graph.py (_merge_into, inline dict construction) and
normalize.py instead — and was removed as dead code during the 2026-07-02
codebase audit. See docs/Decision_log.md for that entry if the original
record-building design is ever needed for reference.
"""

import logging

logger = logging.getLogger(__name__)


# =============================================================================
# Completeness scoring
# =============================================================================

# Fields expected for each entry type, split into required and recommended.
# Required: the entry is meaningfully incomplete without these.
# Recommended: nice to have but not always extractable (e.g. pages for books).
_COMPLETENESS_SPEC: dict[str, dict[str, list[str]]] = {
    "article": {
        "required": ["title", "author", "date", "journaltitle"],
        "recommended": ["volume", "number", "pages", "doi"],
    },
    "incollection": {
        "required": ["title", "author", "date", "booktitle"],
        "recommended": ["editor", "pages", "publisher", "location"],
    },
    "inproceedings": {
        "required": ["title", "author", "date", "eventtitle"],
        "recommended": ["editor", "pages", "publisher", "location"],
    },
    "book": {
        "required": ["title", "author", "date"],
        "recommended": ["publisher", "location", "isbn"],
    },
    "misc": {
        "required": ["title", "author", "date"],
        "recommended": ["url", "note"],
    },
}
_DEFAULT_SPEC = {
    "required": ["title", "author", "date"],
    "recommended": [],
}


def compute_completeness(entry: dict) -> dict:
    """
    Compute a completeness score for a single bibliography entry.

    Scores are based on which biblatex fields are present and non-empty,
    relative to what is expected for the entry's type.

    Returns a dict with:
      "score":            Float 0.0–1.0. Required fields count double.
      "required_present": List of required fields that are present.
      "required_missing": List of required fields that are absent.
      "recommended_present": List of recommended fields that are present.
      "recommended_missing": List of recommended fields that are absent.
      "label":            "complete", "partial", or "minimal".
    """
    entry_type = entry.get("entry_type", "misc")
    spec = _COMPLETENESS_SPEC.get(entry_type, _DEFAULT_SPEC)

    required = spec["required"]
    recommended = spec["recommended"]

    def _present(field: str) -> bool:
        val = entry.get(field)
        if val is None:
            return False
        if isinstance(val, str):
            return bool(val.strip())
        if isinstance(val, list):
            return len(val) > 0
        return bool(val)

    req_present = [f for f in required if _present(f)]
    req_missing = [f for f in required if not _present(f)]
    rec_present = [f for f in recommended if _present(f)]
    rec_missing = [f for f in recommended if not _present(f)]

    # Score: required fields worth 2 points each, recommended worth 1.
    max_score = len(required) * 2 + len(recommended)
    earned = len(req_present) * 2 + len(rec_present)
    score = round(earned / max_score, 3) if max_score > 0 else 1.0

    if len(req_missing) == 0 and len(rec_missing) <= 1:
        label = "complete"
    elif len(req_present) >= len(required) // 2 + 1:
        label = "partial"
    else:
        label = "minimal"

    return {
        "score": score,
        "required_present": req_present,
        "required_missing": req_missing,
        "recommended_present": rec_present,
        "recommended_missing": rec_missing,
        "label": label,
    }


def add_completeness_scores(bibliography: dict[str, dict]) -> int:
    """
    Compute and add a 'completeness' field to every entry in the bibliography.

    Modifies entries in-place. Returns the number of entries updated.
    """
    updated = 0
    for citekey, entry in bibliography.items():
        if citekey.startswith("_") or not isinstance(entry, dict):
            continue
        entry["completeness"] = compute_completeness(entry)
        updated += 1
    return updated