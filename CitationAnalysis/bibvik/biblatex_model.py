"""
bibvik.biblatex_model — Biblatex-conformant data model for bibliographic records.

This module defines the canonical representation of a bibliographic entry in
BibVik's JSON output. The field names and semantics follow the biblatex
specification (§2.1–2.3 of the biblatex manual) as closely as possible, with
extensions for citation-graph metadata.

Biblatex field mapping:
    Standard fields: title, subtitle, author, editor, date, journaltitle,
        booktitle, volume, number, pages, publisher, location, doi, url,
        isbn, issn, eprint, eprinttype, series, eventtitle, note, langid,
        abstract
    Custom fields (prefixed with _ or non-biblatex names):
        - citekey: generated key (lastnameyear format)
        - entry_type: article, book, incollection, inproceedings, misc, etc.
        - generation: F1, F2, ... indicating distance from seed paper
        - cited_by: list of citing-paper records with contexts
        - _grobid_id: GROBID's internal reference ID (for cross-referencing)
        - _raw_citation: original unparsed citation string from GROBID
        - _source_pdf: filename of the PDF from which this record was extracted

Why not just use dicts?
    We use a builder/normalizer pattern rather than raw dicts for two reasons:
    1. Consistent output format: every record has the same structure, even if
       some fields are empty. This simplifies downstream processing.
    2. Validation: we catch common issues (missing authors, malformed dates)
       early rather than propagating them silently.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Ordered list of biblatex fields for output serialization.
# Fields appear in this order in the JSON output.
BIBLATEX_FIELD_ORDER = [
    "citekey",
    "entry_type",
    "title",
    "subtitle",
    "author",
    "editor",
    "translator",
    "date",
    "year",
    "journaltitle",
    "booktitle",
    "series",
    "volume",
    "number",
    "pages",
    "publisher",
    "location",
    "eventtitle",
    "doi",
    "url",
    "eprint",
    "eprinttype",
    "isbn",
    "issn",
    "langid",
    "abstract",
    "note",
    # --- BibVik extensions ---
    "generation",
    "cited_by",
    # --- Internal / debugging ---
    "_grobid_id",
    "_raw_citation",
    "_source_pdf",
]


def normalize_record(raw: dict, citekey: str, generation: str, source_pdf: str) -> dict:
    """
    Normalize a raw parsed reference dict into the canonical biblatex structure.

    This function:
    1. Assigns the generated citekey.
    2. Sets the generation label (e.g., "F1").
    3. Cleans up field values (strip whitespace, normalize page ranges, etc.).
    4. Ensures all expected fields exist (even if empty/None).
    5. Removes GROBID-internal fields that aren't needed in the output.

    Args:
        raw:        Dict from tei_parser.parse_tei_references(), with fields
                    like 'title', 'author', 'date', '_grobid_id', etc.
        citekey:    Generated citekey string (e.g., "smith2020a").
        generation: Generation label (e.g., "F1", "F2").
        source_pdf: Filename of the PDF this reference was extracted from.

    Returns:
        Normalized dict with all biblatex fields in canonical order.
    """
    record = {}

    # --- Core identification ---
    record["citekey"] = citekey
    record["entry_type"] = raw.get("entry_type", "misc")

    # --- Title ---
    record["title"] = _clean_str(raw.get("title", ""))
    record["subtitle"] = _clean_str(raw.get("subtitle", ""))

    # --- People ---
    # Authors and editors are lists of dicts with 'family' and 'given' keys.
    # We preserve the original Unicode characters.
    record["author"] = _clean_names(raw.get("author", []))
    record["editor"] = _clean_names(raw.get("editor", []))
    record["translator"] = _clean_names(raw.get("translator", []))

    # --- Date ---
    # Biblatex uses 'date' as the primary field (ISO 8601 format preferred).
    # We also populate 'year' for convenience/backward compatibility.
    date_str = _clean_str(raw.get("date", ""))
    record["date"] = date_str
    record["year"] = _extract_year(date_str)

    # --- Container (journal / book / series) ---
    record["journaltitle"] = _clean_str(raw.get("journaltitle", ""))
    record["booktitle"] = _clean_str(raw.get("booktitle", ""))
    record["series"] = _clean_str(raw.get("series", ""))

    # --- Numeric fields ---
    record["volume"] = _clean_str(raw.get("volume", ""))
    record["number"] = _clean_str(raw.get("number", ""))
    record["pages"] = _normalize_pages(raw.get("pages", ""))

    # --- Publisher info ---
    record["publisher"] = _clean_str(raw.get("publisher", ""))
    record["location"] = _clean_str(raw.get("location", ""))

    # --- Event (for conference proceedings) ---
    record["eventtitle"] = _clean_str(raw.get("eventtitle", ""))

    # --- Identifiers ---
    record["doi"] = _clean_str(raw.get("doi", ""))
    record["url"] = _clean_str(raw.get("url", ""))
    record["eprint"] = _clean_str(raw.get("eprint", ""))
    record["eprinttype"] = _clean_str(raw.get("eprinttype", ""))
    record["isbn"] = _clean_str(raw.get("isbn", ""))
    record["issn"] = _clean_str(raw.get("issn", ""))

    # --- Other ---
    record["langid"] = _clean_str(raw.get("langid", ""))
    record["abstract"] = _clean_str(raw.get("abstract", ""))
    record["note"] = _clean_str(raw.get("note", ""))

    # --- BibVik extensions ---
    record["generation"] = generation
    record["cited_by"] = []  # Will be populated by citation_graph module.

    # --- Internal ---
    record["_grobid_id"] = raw.get("_grobid_id", "")
    record["_raw_citation"] = _clean_str(raw.get("_raw_citation", ""))
    record["_source_pdf"] = source_pdf

    # --- Remove empty optional fields to keep output clean ---
    record = _prune_empty(record)

    return record


def merge_records(existing: dict, new: dict) -> dict:
    """
    Merge a new reference record into an existing one.

    When the same reference appears in multiple papers' bibliographies, we
    want to combine the information rather than overwrite. Strategy:
    - For scalar fields: prefer the more complete (non-empty) value.
    - For 'cited_by': concatenate (each paper's citation info is additive).
    - For 'generation': keep the lowest generation (closest to seed paper).

    This is important because different papers may cite the same source with
    varying levels of metadata completeness. A well-formatted journal article
    might include DOI and full page ranges, while a book chapter might omit
    some of these details. Merging lets us build the most complete record.

    Args:
        existing: The current record in the bibliography.
        new:      A newly extracted record for the same reference.

    Returns:
        The merged record (modifies existing in place and returns it).
    """
    # --- Merge scalar fields: prefer non-empty ---
    # Special rule for title: once a title is set, never overwrite it.
    # GROBID's structured title parse from one PDF may be wrong (e.g. picking
    # up a nearby heading), while the existing title came from the seed paper's
    # reference list and is more likely correct. The _raw_citation field is the
    # ground truth; title discrepancies should be resolved via _raw_citation
    # validation, not by silently overwriting with a longer string.
    scalar_fields = [
        "subtitle", "journaltitle", "booktitle", "series",
        "volume", "number", "pages", "publisher", "location", "eventtitle",
        "doi", "url", "eprint", "eprinttype", "isbn", "issn",
        "langid", "abstract", "note", "date", "year",
    ]
    # Title: only fill in if empty; never replace an existing title.
    if not existing.get("title") and new.get("title"):
        existing["title"] = new["title"]

    for field in scalar_fields:
        existing_val = existing.get(field, "")
        new_val = new.get(field, "")
        # Prefer whichever is longer / more complete.
        if new_val and (not existing_val or len(str(new_val)) > len(str(existing_val))):
            existing[field] = new_val

    # --- Merge authors/editors: prefer the list with more entries ---
    for people_field in ["author", "editor", "translator"]:
        existing_list = existing.get(people_field, [])
        new_list = new.get(people_field, [])
        if len(new_list) > len(existing_list):
            existing[people_field] = new_list

    # --- Merge cited_by: additive ---
    existing.setdefault("cited_by", [])
    existing["cited_by"].extend(new.get("cited_by", []))

    # --- Generation: keep lowest ---
    existing_gen = existing.get("generation", "")
    new_gen = new.get("generation", "")
    if existing_gen and new_gen:
        # Compare numerically (F1 < F2 < F3...)
        try:
            existing_num = int(existing_gen.replace("F", ""))
            new_num = int(new_gen.replace("F", ""))
            if new_num < existing_num:
                existing["generation"] = new_gen
        except ValueError:
            pass
    elif new_gen and not existing_gen:
        existing["generation"] = new_gen

    return existing


# =============================================================================
# Internal helpers
# =============================================================================

def _clean_str(value: Any) -> str:
    """Strip whitespace and normalize a string value."""
    if value is None:
        return ""
    return str(value).strip()


def _clean_names(names: list) -> list[dict]:
    """
    Clean a list of name dicts, removing entries with no usable data.
    """
    cleaned = []
    for name in names:
        if isinstance(name, dict):
            family = name.get("family", "").strip()
            given = name.get("given", "").strip()
            if family or given:
                cleaned.append({"family": family, "given": given})
    return cleaned


def _extract_year(date_str: str) -> str:
    """
    Extract a 4-digit year from a date string.

    Handles ISO dates ("2020-06-15"), year-only ("2020"), and various
    natural-language date formats.
    """
    import re
    if not date_str:
        return ""
    match = re.search(r"\b(\d{4})\b", date_str)
    return match.group(1) if match else ""


def _normalize_pages(pages: Any) -> str:
    """
    Normalize page ranges to biblatex format: "start--end".

    Biblatex convention uses en-dash (--) for page ranges in .bib files.
    We accept various input formats: "45-67", "45–67", "45—67", "45 - 67".
    """
    if not pages:
        return ""
    pages_str = str(pages).strip()
    # Replace various dash types with biblatex en-dash
    import re
    pages_str = re.sub(r"\s*[–—-]+\s*", "--", pages_str)
    return pages_str


def _prune_empty(record: dict) -> dict:
    """
    Remove fields with empty string values to keep JSON output clean.

    We keep fields that have meaningful defaults (like empty lists for
    cited_by, or the generation label) but remove fields like empty
    'subtitle' or 'isbn' to avoid clutter.

    Fields that are always kept regardless of value:
    - citekey, entry_type, title, author, generation, cited_by
    """
    always_keep = {"citekey", "entry_type", "title", "author", "generation", "cited_by"}
    pruned = {}
    for key, value in record.items():
        if key in always_keep:
            pruned[key] = value
        elif isinstance(value, str) and value == "":
            continue
        elif isinstance(value, list) and len(value) == 0:
            continue
        else:
            pruned[key] = value
    return pruned


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
