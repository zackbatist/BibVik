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

Author enrichment (CrossRef DOI lookup)
----------------------------------------
Author enrichment uses the paper's own DOI record from CrossRef. For each
F1 paper with a header DOI, the CrossRef record is fetched and its author
list is matched to GROBID's author list by normalised family name. Matched
authors receive: expanded given names (when CrossRef has full names vs
GROBID's initials) and ORCID identifiers (when the publisher submitted them
to CrossRef).

This approach requires no disambiguation — the paper's DOI is a unique
identifier, and the author's identity is derived from the work they wrote
rather than from a name search. If a paper has no DOI, CrossRef has no
record for it, or an author cannot be matched by family name, nothing is
changed. No match is better than a wrong match.

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

CROSSREF_BASE       = "https://api.crossref.org/works"
CROSSREF_DELAY      = 0.15   # Polite delay between CrossRef requests
TITLE_SIM_THRESHOLD = 0.85   # Minimum title similarity for CrossRef title queries


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
# Author enrichment (CrossRef DOI lookup)
# =============================================================================

def enrich_authors(
    processed_papers: dict[str, dict],
    email: str = "",
) -> dict[str, int]:
    """
    Enrich author records in processed_papers headers using CrossRef.

    For each paper with a DOI in its header, fetches the CrossRef record
    for that DOI and uses the author list to expand initials to full given
    names and add ORCID identifiers when present.

    The author's identity is derived from the work they wrote — the paper's
    own DOI record is the most reliable source, requiring no disambiguation.
    If a paper has no DOI, CrossRef has no record for it, or an author cannot
    be matched by normalised family name, nothing is changed for that author.

    Args:
        processed_papers: Processed paper data dict (modified in place).
        email:            Email for CrossRef polite pool.

    Returns:
        Dict with counts: papers_found, authors_enriched,
        papers_no_doi, papers_not_found.
    """
    counts = {
        "papers_found":     0,
        "authors_enriched": 0,
        "papers_no_doi":    0,
        "papers_not_found": 0,
    }

    for pdf_name, data in processed_papers.items():
        header = data.get("header", {})
        doi    = (header.get("doi") or "").strip()

        if not doi:
            counts["papers_no_doi"] += 1
            logger.debug("No DOI in header for %s — skipping author enrichment", pdf_name)
            continue

        cr_record = _crossref_by_doi(doi, email)
        if not cr_record:
            counts["papers_not_found"] += 1
            logger.debug("CrossRef found no record for DOI %s (%s)", doi, pdf_name)
            time.sleep(CROSSREF_DELAY)
            continue

        counts["papers_found"] += 1
        cr_authors    = cr_record.get("author", [])
        entry_authors = header.get("author", [])

        if not cr_authors or not entry_authors:
            time.sleep(CROSSREF_DELAY)
            continue

        enriched_this_paper = 0
        for author in entry_authors:
            family_norm = norm_author(author.get("family", ""))
            if not family_norm:
                continue

            # Find matching CrossRef author by normalised family name.
            # If multiple authors share the same family name, take the first —
            # CrossRef and GROBID should list authors in the same order.
            cr_match = next(
                (a for a in cr_authors if norm_author(a.get("family", "")) == family_norm),
                None,
            )
            if not cr_match:
                continue

            changed = False

            # Expand initials to full given name
            given    = author.get("given", "")
            cr_given = cr_match.get("given", "")
            is_initial = not given or len(given.replace(".", "").replace(" ", "")) <= 2
            if is_initial and cr_given and len(cr_given) > len(given):
                author["given"] = cr_given
                changed = True

            # Add ORCID if present in CrossRef record and not already set
            cr_orcid = cr_match.get("ORCID", "")
            if cr_orcid and not author.get("orcid"):
                author["orcid"] = cr_orcid
                changed = True

            if changed:
                enriched_this_paper += 1

        counts["authors_enriched"] += enriched_this_paper
        logger.debug(
            "%s: enriched %d author(s) from CrossRef DOI %s",
            pdf_name, enriched_this_paper, doi,
        )
        time.sleep(CROSSREF_DELAY)

    logger.info(
        "Author enrichment complete: %d papers found in CrossRef, "
        "%d authors enriched, %d papers had no DOI, %d not found.",
        counts["papers_found"], counts["authors_enriched"],
        counts["papers_no_doi"], counts["papers_not_found"],
    )
    return counts


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