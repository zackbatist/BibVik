"""
bibvik.enricher — Post-hoc metadata enrichment for the citation graph.

This module runs after the citation graph has been fully built by --iterate-f1.
It enriches existing bibliography entries with additional metadata from external
sources, and enriches author records with canonical name forms and affiliations.

Design principle
----------------
Enrichment is strictly additive — it fills in missing fields on entries that
are already identified. It never changes an entry's citekey, generation,
cited_by relationships, or identity (author+year combination). The graph
structure is untouched; only metadata fields are updated.

This is distinct from resolution (which attempted to identify unknown entries
from bare author+year pairs). Resolution via CrossRef is no longer performed
during the main pipeline run because the author+year query is too weak to
produce reliable matches. See docs/methods/resolver-method.md.

Bibliography enrichment (CrossRef)
-----------------------------------
CrossRef is used strictly for enrichment — not identification. Two strategies:

  1. DOI lookup: for entries that already have a DOI (from GROBID extraction),
     fetch the full CrossRef record to fill in missing fields: volume, pages,
     issue, canonical journal name, publisher, full author given names.

  2. Title query: for entries with a title but no DOI, query CrossRef by
     title + author. Accept only if CrossRef returns a result with very high
     title similarity (≥0.85 by default). On acceptance, fill in missing
     fields including DOI. Does not overwrite existing fields.

Author enrichment (OpenAlex)
------------------------------
OpenAlex is used to enrich author records extracted from paper headers:

  - Expand initials to full given names (CrossRef also helps here)
  - Retrieve canonical institution names and ROR identifiers
  - Retrieve ORCID identifiers when available

OpenAlex integrates ORCID as a data source for its author disambiguation
(since July 2023), so querying OpenAlex gives access to ORCID-verified
author profiles without needing a separate ORCID API query. See:
  https://help.openalex.org/hc/en-us/articles/24347048891543-Author-disambiguation

Author enrichment is run as a separate pass over processed_papers headers,
not over bibliography entries. Affiliations are properties of the paper's
authors (the citing authors), not of cited works.

Usage
-----
    python run.py --enrich                    # Both bibliography and author enrichment
    python run.py --enrich --enrich-bib-only  # Bibliography enrichment only
    python run.py --enrich --enrich-auth-only # Author enrichment only
"""

import difflib
import logging
import re
import time
from pathlib import Path

import requests
from unidecode import unidecode

from .utils import norm_author

logger = logging.getLogger(__name__)

CROSSREF_BASE   = "https://api.crossref.org/works"
OPENALEX_BASE   = "https://api.openalex.org"
CROSSREF_DELAY  = 0.15   # Polite delay between CrossRef requests
OPENALEX_DELAY  = 0.10   # Polite delay between OpenAlex requests
TITLE_SIM_THRESHOLD = 0.85  # Minimum title similarity for CrossRef title queries


# =============================================================================
# Bibliography enrichment
# =============================================================================

def enrich_bibliography(
    bibliography: dict[str, dict],
    email: str = "",
    title_threshold: float = TITLE_SIM_THRESHOLD,
) -> dict[str, int]:
    """
    Enrich bibliography entries with metadata from CrossRef.

    Runs two passes:
      1. DOI lookup: entries with DOIs but missing metadata fields.
      2. Title query: entries with titles but no DOIs.

    Args:
        bibliography:     Full bibliography dict (modified in place).
        email:            Email for CrossRef polite pool.
        title_threshold:  Minimum title similarity for title-based matching.

    Returns:
        Dict with counts: doi_enriched, title_enriched, skipped.
    """
    counts = {"doi_enriched": 0, "title_enriched": 0, "skipped": 0}

    if not email:
        logger.warning(
            "No email provided for CrossRef enrichment. "
            "Pass --email to use the polite pool."
        )

    for ck, entry in bibliography.items():
        doi   = (entry.get("doi") or "").strip()
        title = (entry.get("title") or "").strip()

        if doi:
            # Pass 1: enrich by DOI — most reliable
            enriched = _crossref_by_doi(doi, email)
            if enriched:
                _apply_enrichment(entry, enriched)
                counts["doi_enriched"] += 1
                logger.debug("DOI-enriched: %s", ck)
            time.sleep(CROSSREF_DELAY)

        elif title:
            # Pass 2: enrich by title — requires high similarity confirmation
            author = ""
            authors = entry.get("author", [])
            if authors:
                author = authors[0].get("family", "")
            year = entry.get("date", entry.get("year", ""))[:4]

            enriched = _crossref_by_title(title, author, year, email, title_threshold)
            if enriched:
                _apply_enrichment(entry, enriched)
                counts["title_enriched"] += 1
                logger.debug("Title-enriched: %s", ck)
            time.sleep(CROSSREF_DELAY)

        else:
            counts["skipped"] += 1

    logger.info(
        "Bibliography enrichment complete: %d DOI lookups, %d title matches, %d skipped.",
        counts["doi_enriched"], counts["title_enriched"], counts["skipped"],
    )
    return counts


def _crossref_by_doi(doi: str, email: str) -> dict | None:
    """Fetch a CrossRef record by DOI. Returns metadata dict or None."""
    doi = _clean_doi(doi)
    if not doi:
        return None

    url = f"{CROSSREF_BASE}/{doi}"
    params = {}
    if email:
        params["mailto"] = email

    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.debug("CrossRef DOI lookup HTTP %d for %s", resp.status_code, doi)
            return None
        return resp.json().get("message", {})
    except (requests.Timeout, requests.ConnectionError) as e:
        logger.debug("CrossRef DOI lookup failed for %s: %s", doi, e)
        return None


def _crossref_by_title(
    title: str,
    author: str,
    year: str,
    email: str,
    threshold: float,
) -> dict | None:
    """
    Query CrossRef by title + author. Accept only if title similarity ≥ threshold.

    This is much more precise than the author+year query used in the old
    resolver. A title query with high similarity confirmation rarely produces
    false positives because titles are distinctive.
    """
    params = {
        "query.title": title,
        "rows": 3,
    }
    if author:
        params["query.author"] = author
    if email:
        params["mailto"] = email

    try:
        resp = requests.get(CROSSREF_BASE, params=params, timeout=15)
        if resp.status_code != 200:
            return None

        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None

        title_norm = _norm_title(title)

        for item in items:
            cr_title = " ".join(item.get("title", []))
            if not cr_title:
                continue

            sim = difflib.SequenceMatcher(
                None, title_norm, _norm_title(cr_title)
            ).ratio()

            if sim < threshold:
                logger.debug(
                    "CrossRef title rejected (sim=%.2f < %.2f): '%s' → '%s'",
                    sim, threshold, title[:60], cr_title[:60],
                )
                continue

            # Optionally verify year if we have one
            if year:
                issued = item.get("issued", {}).get("date-parts", [[None]])
                item_year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
                if item_year and item_year != year:
                    continue

            logger.debug("CrossRef title match (sim=%.2f): '%s'", sim, cr_title[:60])
            return item

    except (requests.Timeout, requests.ConnectionError) as e:
        logger.debug("CrossRef title query failed: %s", e)
        return None

    return None


def _apply_enrichment(entry: dict, cr_item: dict) -> None:
    """
    Apply CrossRef metadata to an existing bibliography entry.

    Only fills in missing fields — never overwrites existing values.
    Updates: doi, volume, number, pages, publisher, journaltitle/booktitle,
    entry_type, and expands author given names from initials.
    """
    # DOI
    if not entry.get("doi") and cr_item.get("DOI"):
        entry["doi"] = cr_item["DOI"]

    # Volume, number, pages
    if not entry.get("volume") and cr_item.get("volume"):
        entry["volume"] = cr_item["volume"]
    if not entry.get("number") and cr_item.get("issue"):
        entry["number"] = cr_item["issue"]
    if not entry.get("pages") and cr_item.get("page"):
        entry["pages"] = cr_item["page"]

    # Publisher
    if not entry.get("publisher") and cr_item.get("publisher"):
        entry["publisher"] = cr_item["publisher"]

    # Container title
    container = " ".join(cr_item.get("container-title", []))
    cr_type = cr_item.get("type", "")
    if container:
        if cr_type in ("journal-article",) and not entry.get("journaltitle"):
            entry["journaltitle"] = container
        elif cr_type in ("book-chapter",) and not entry.get("booktitle"):
            entry["booktitle"] = container

    # Expand author given names from initials
    cr_authors = cr_item.get("author", [])
    entry_authors = entry.get("author", [])
    if cr_authors and entry_authors:
        _expand_author_given_names(entry_authors, cr_authors)

    # Tag enrichment source
    entry["_enriched_via"] = "crossref"


def _expand_author_given_names(
    entry_authors: list[dict],
    cr_authors: list[dict],
) -> None:
    """
    Expand initials to full given names using CrossRef author data.

    Matches by normalised family name and replaces initial-only given
    names with the full form from CrossRef. Does not overwrite existing
    full names.
    """
    cr_by_family = {
        norm_author(a.get("family", "")): a
        for a in cr_authors
        if a.get("family")
    }

    for author in entry_authors:
        family_norm = norm_author(author.get("family", ""))
        given = author.get("given", "")

        # Only expand if current given name looks like initials (≤3 chars or dots)
        is_initial = not given or len(given.replace(".", "").replace(" ", "")) <= 2

        if is_initial and family_norm in cr_by_family:
            cr_given = cr_by_family[family_norm].get("given", "")
            if cr_given and len(cr_given) > len(given):
                author["given"] = cr_given


# =============================================================================
# Author enrichment (OpenAlex)
# =============================================================================

def enrich_authors(
    processed_papers: dict[str, dict],
    email: str = "",
) -> dict[str, int]:
    """
    Enrich author records in processed_papers headers using OpenAlex.

    For each paper's authors, queries OpenAlex to find the canonical
    author profile: full name, ORCID, and current institution (ROR).

    OpenAlex integrates ORCID as a data source for author disambiguation,
    so this provides access to ORCID-verified profiles without a separate
    ORCID API query.

    Args:
        processed_papers: Processed paper data dict (modified in place).
        email:            Email for OpenAlex polite pool.

    Returns:
        Dict with counts: authors_enriched, not_found, skipped.
    """
    counts = {"authors_enriched": 0, "not_found": 0, "skipped": 0}
    headers = {"User-Agent": f"BibVik/0.1 (mailto:{email})" if email else "BibVik/0.1"}

    for pdf_name, data in processed_papers.items():
        header = data.get("header", {})
        authors = header.get("author", [])

        for author in authors:
            family = author.get("family", "")
            given  = author.get("given", "")

            if not family:
                counts["skipped"] += 1
                continue

            # Skip if already enriched
            if author.get("openalex_id") or author.get("orcid"):
                counts["skipped"] += 1
                continue

            profile = _openalex_author_lookup(family, given, headers)
            if profile:
                _apply_author_enrichment(author, profile)
                counts["authors_enriched"] += 1
                logger.debug("OpenAlex-enriched: %s, %s", family, given)
            else:
                counts["not_found"] += 1

            time.sleep(OPENALEX_DELAY)

    logger.info(
        "Author enrichment complete: %d enriched, %d not found, %d skipped.",
        counts["authors_enriched"], counts["not_found"], counts["skipped"],
    )
    return counts


def _openalex_author_lookup(
    family: str,
    given: str,
    headers: dict,
) -> dict | None:
    """
    Query OpenAlex for an author by name.

    Returns the best-matching author profile dict, or None if no confident
    match is found.
    """
    name = f"{given} {family}".strip() if given else family

    try:
        resp = requests.get(
            f"{OPENALEX_BASE}/authors",
            params={
                "search": name,
                "per_page": 3,
            },
            headers=headers,
            timeout=15,
        )
        if resp.status_code != 200:
            return None

        results = resp.json().get("results", [])
        if not results:
            return None

        # Take the first result if the display name is a plausible match
        best = results[0]
        display = best.get("display_name", "")

        # Verify family name appears in the display name
        if norm_author(family) not in norm_author(display):
            return None

        return best

    except (requests.Timeout, requests.ConnectionError) as e:
        logger.debug("OpenAlex lookup failed for %s: %s", name, e)
        return None


def _apply_author_enrichment(author: dict, profile: dict) -> None:
    """
    Apply OpenAlex profile data to an author dict.

    Fills in: full given name (if currently initials), ORCID, OpenAlex ID,
    and last known institution name and ROR ID.
    """
    # Full name
    display_name = profile.get("display_name", "")
    if display_name:
        parts = display_name.strip().split()
        if len(parts) >= 2:
            cr_given = " ".join(parts[:-1])
            given = author.get("given", "")
            is_initial = not given or len(given.replace(".", "").replace(" ", "")) <= 2
            if is_initial and cr_given:
                author["given"] = cr_given

    # ORCID
    orcid = profile.get("orcid", "")
    if orcid and not author.get("orcid"):
        author["orcid"] = orcid

    # OpenAlex ID
    openalex_id = profile.get("id", "")
    if openalex_id and not author.get("openalex_id"):
        author["openalex_id"] = openalex_id

    # Last known institution
    affiliations = profile.get("affiliations", [])
    if affiliations:
        # affiliations is sorted by relevance/recency in OpenAlex
        inst = affiliations[0].get("institution", {})
        if inst:
            if not author.get("affiliation"):
                author["affiliation"] = {}
            aff = author["affiliation"]
            if not aff.get("institution") and inst.get("display_name"):
                aff["institution"] = inst["display_name"]
            if not aff.get("ror") and inst.get("ror"):
                aff["ror"] = inst["ror"]
            if not aff.get("country") and inst.get("country_code"):
                aff["country"] = inst["country_code"]


# =============================================================================
# Helpers
# =============================================================================

def _clean_doi(doi: str) -> str:
    """Clean a DOI string of URL prefixes and trailing punctuation."""
    doi = doi.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"[.,;]+$", "", doi)
    if doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi.rstrip(")")
    return doi.strip()


def _norm_title(title: str) -> str:
    """Normalise a title string for similarity comparison."""
    return unidecode(title).lower().strip()
