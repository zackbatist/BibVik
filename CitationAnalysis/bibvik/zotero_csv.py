"""
bibvik.zotero_csv — Parse a Zotero CSV export to map PDF filenames to metadata.

When a Zotero CSV export is provided, this module builds an exact mapping from
PDF filenames to bibliographic metadata (author, year, title, DOI). This
mapping is used during F1 processing to reliably associate each PDF with its
corresponding bibliography entry, bypassing the fuzzy heuristics that can fail
on unusual filenames or GROBID parsing discrepancies.

The CSV is expected to have at minimum these columns (standard Zotero export):
- Author: semicolon-separated, "Family, Given" format
- Publication Year: 4-digit year
- Title: full title
- File Attachments: full path(s) to attached files
- DOI: (optional) digital object identifier

Citekey generation from the CSV:
    We generate citekeys using the same algorithm as the rest of BibVik
    (first author family name + year, with a/b/c disambiguation). However,
    as the user noted, disambiguation suffixes may not align between the
    Zotero export order and GROBID's extraction order. For entries that
    would get a suffix, we store ALL the metadata and rely on title/DOI
    matching to resolve the correct pairing.
"""

import csv
import logging
import os
import re
from pathlib import Path
from typing import Any

from unidecode import unidecode

logger = logging.getLogger(__name__)


def parse_zotero_csv(csv_path: str | Path) -> dict[str, dict]:
    """
    Parse a Zotero CSV export and build a PDF filename → metadata mapping.

    Args:
        csv_path: Path to the Zotero CSV file.

    Returns:
        Dict mapping PDF filenames (basename only, e.g., "Smith 2020 - Title.pdf")
        to metadata dicts with keys:
        - 'authors': list of {'family': ..., 'given': ...}
        - 'year': str
        - 'title': str
        - 'doi': str
        - 'base_citekey': str (author+year without disambiguation suffix)
        - 'normalized_title': str (for fuzzy matching)
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        logger.warning("Zotero CSV not found: %s", csv_path)
        return {}

    pdf_map: dict[str, dict] = {}

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        for row in reader:
            # --- Parse authors ---
            raw_authors = row.get("Author", "").strip()
            authors = _parse_zotero_authors(raw_authors)

            # --- Year ---
            year = row.get("Publication Year", "").strip()

            # --- Title ---
            title = row.get("Title", "").strip()

            # --- DOI ---
            doi = row.get("DOI", "").strip()

            # --- Base citekey (without disambiguation) ---
            base_citekey = _make_base_citekey(authors, year)

            # --- Normalized title for matching ---
            normalized_title = _normalize_title(title)

            # --- File attachments ---
            # Zotero separates multiple attachments with semicolons.
            attachments = row.get("File Attachments", "").strip()
            if not attachments:
                continue

            for filepath in attachments.split(";"):
                filepath = filepath.strip()
                if not filepath.lower().endswith(".pdf"):
                    continue

                filename = os.path.basename(filepath)

                pdf_map[filename] = {
                    "authors": authors,
                    "year": year,
                    "title": title,
                    "doi": doi,
                    "base_citekey": base_citekey,
                    "normalized_title": normalized_title,
                }

    logger.info("Parsed Zotero CSV: %d PDF mappings from %s", len(pdf_map), csv_path.name)
    return pdf_map


def match_pdf_to_bibliography(
    pdf_name: str,
    zotero_map: dict[str, dict],
    bibliography: dict[str, dict],
) -> str | None:
    """
    Use the Zotero CSV data to match a PDF filename to a bibliography entry.

    Matching strategy:
    1. Look up the PDF filename in the Zotero map to get its metadata.
    2. Try DOI match against the bibliography (most reliable).
    3. Try exact normalized title match.
    4. Try base_citekey + title token overlap (for disambiguated entries
       where a/b/c suffixes may differ).

    Args:
        pdf_name:    PDF filename (basename).
        zotero_map:  Output of parse_zotero_csv().
        bibliography: Current bibliography dict.

    Returns:
        Matching citekey from bibliography, or None.
    """
    zotero_entry = zotero_map.get(pdf_name)
    if not zotero_entry:
        return None

    z_doi = zotero_entry.get("doi", "").strip().lower()
    z_title = zotero_entry.get("normalized_title", "")
    z_base_key = zotero_entry.get("base_citekey", "")

    best_match = None
    best_score = 0.0

    for citekey, bib_entry in bibliography.items():
        # Skip the seed paper.
        if bib_entry.get("generation") == "P":
            continue

        # --- Tier 1: DOI match ---
        bib_doi = bib_entry.get("doi", "").strip().lower()
        if z_doi and bib_doi and z_doi == bib_doi:
            return citekey

        # --- Tier 2: Exact title match ---
        bib_title = _normalize_title(bib_entry.get("title", ""))
        if z_title and bib_title and z_title == bib_title and len(z_title) >= 20:
            return citekey

        # --- Tier 3: Base citekey + title overlap ---
        # Only for disambiguated entries (a/b/c suffixes) of the SAME author+year.
        # The base citekey must match exactly or the bib citekey must be
        # base_key + a single letter suffix.
        if z_base_key and (citekey == z_base_key or
            (citekey.startswith(z_base_key) and len(citekey) == len(z_base_key) + 1
             and citekey[-1].isalpha())):
            if z_title and bib_title:
                score = _token_overlap(z_title, bib_title)
                if score > best_score and score >= 0.7:
                    best_score = score
                    best_match = citekey

    return best_match


# =============================================================================
# Internal helpers
# =============================================================================

def _parse_zotero_authors(raw: str) -> list[dict]:
    """
    Parse Zotero's author format: "Family, Given; Family2, Given2"

    Returns list of {'family': ..., 'given': ...} dicts.
    """
    authors = []
    if not raw:
        return authors

    for author_str in raw.split(";"):
        author_str = author_str.strip()
        if not author_str:
            continue

        if "," in author_str:
            parts = author_str.split(",", 1)
            family = parts[0].strip()
            given = parts[1].strip() if len(parts) > 1 else ""
        else:
            # No comma — try splitting on last space.
            parts = author_str.rsplit(" ", 1)
            if len(parts) == 2:
                given, family = parts
            else:
                family = author_str
                given = ""

        authors.append({"family": family, "given": given})

    return authors


def _make_base_citekey(authors: list[dict], year: str) -> str:
    """
    Generate the base citekey (without disambiguation suffix).

    Same algorithm as utils.generate_citekey but without registry tracking.
    """
    if authors and authors[0].get("family"):
        family = unidecode(authors[0]["family"]).lower()
        family = re.sub(r"[^a-z]", "", family)
    else:
        family = "unknown"

    year_str = year.strip() if year else "nd"
    return f"{family}{year_str}"


def _normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and whitespace."""
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _token_overlap(text_a: str, text_b: str) -> float:
    """Token-level overlap score between two normalized strings."""
    stop_words = {
        "the", "and", "for", "with", "from", "that", "this", "its",
        "are", "was", "were", "been", "has", "have", "had", "not",
        "but", "can", "will", "into", "than", "also", "about",
    }

    def tokenize(text):
        tokens = set(re.split(r"\s+", text.lower()))
        return {t for t in tokens if len(t) >= 3 and t not in stop_words}

    tokens_a = tokenize(text_a)
    tokens_b = tokenize(text_b)

    if not tokens_a or not tokens_b:
        return 0.0

    intersection = tokens_a & tokens_b
    smaller = min(len(tokens_a), len(tokens_b))
    return len(intersection) / smaller if smaller > 0 else 0.0
