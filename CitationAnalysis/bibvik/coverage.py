"""
bibvik.coverage — PDF coverage reporting and open access acquisition.

This module:
1. Reports which bibliography entries have PDFs available for analysis
   and which are missing.
2. Checks open access availability via the Unpaywall API (free, no auth
   needed for polite use with an email).
3. Generates an acquisition plan listing where missing papers might be
   found (DOI-based OA lookup, institutional repositories, etc.).
4. Optionally downloads freely available open access PDFs.

This is useful for:
- Understanding how complete your F1 corpus is.
- Planning F2 corpus assembly by identifying which F2 references are
  freely available before committing to manual acquisition.
- Supplementing F1 coverage with OA copies.

Unpaywall API:
    Free tier requires only an email address (for polite usage tracking).
    Endpoint: https://api.unpaywall.org/v2/{doi}?email={email}
    Returns OA status and direct PDF links where available.
    Rate limit: ~100k requests/day (generous for our use case).
"""

import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import requests

from .metadata import build_coverage_metadata
from .utils import write_json

logger = logging.getLogger(__name__)


def generate_coverage_report(
    bibliography: dict[str, dict],
    processed_papers: dict[str, dict],
    f1_pdf_dir: str | Path,
    config: dict,
    output_dir: str | Path,
    email: str | None = None,
    check_oa: bool = True,
) -> dict:
    """
    Generate a comprehensive coverage report.

    Analyzes the bibliography to determine:
    - Which F1 references have PDFs (and were processed).
    - Which F1 references are missing PDFs.
    - Open access availability for missing papers (via Unpaywall).
    - Overall coverage statistics.
    - An F2 planning section showing coverage for second-generation refs.

    Args:
        bibliography:     The full bibliography dict.
        processed_papers: Dict of processed paper data.
        f1_pdf_dir:       Directory where F1 PDFs are stored.
        config:           Full config dict.
        output_dir:       Where to write the report.
        email:            Email for Unpaywall API (required for OA lookups).
        check_oa:         Whether to check open access status via Unpaywall.

    Returns:
        The coverage report dict.
    """
    output_dir = Path(output_dir)
    f1_pdf_dir = Path(f1_pdf_dir)

    # --- Categorize bibliography entries ---
    seed_entries = {}
    f1_entries = {}
    f2_entries = {}
    other_entries = {}

    for citekey, entry in bibliography.items():
        gen = entry.get("generation", "")
        if gen == "P":
            seed_entries[citekey] = entry
        elif gen == "F1":
            f1_entries[citekey] = entry
        elif gen == "F2":
            f2_entries[citekey] = entry
        else:
            other_entries[citekey] = entry

    # --- Determine which F1 entries have PDFs ---
    # A paper "has a PDF" if it has a _source_pdf field pointing to a
    # file that was processed, OR if we can find a matching file in the
    # F1 PDF directory.
    available_pdfs = set()
    if f1_pdf_dir.is_dir():
        available_pdfs = {p.name for p in f1_pdf_dir.glob("*.pdf")}

    processed_pdf_names = set(processed_papers.keys())

    # --- Determine which F1 entries have their own PDFs ---
    # An F1 entry "has a PDF" if its own PDF was processed during the F1
    # iteration stage. The _source_pdf field is ambiguous: for entries
    # extracted from the seed paper, it points to the seed PDF (the paper
    # they were extracted FROM, not their own PDF). For entries that were
    # matched during F1 processing, _source_pdf is updated to point to
    # their own PDF.
    #
    # The reliable signal is: was this entry's _source_pdf processed as an
    # F1 paper (i.e., it appears in processed_papers AND it's not the seed)?
    seed_pdf_name = Path(config.get("seed_paper", "")).name

    f1_with_pdf = {}
    f1_missing_pdf = {}

    for citekey, entry in f1_entries.items():
        source_pdf = entry.get("_source_pdf", "")
        has_own_pdf = False

        if source_pdf and source_pdf != seed_pdf_name:
            # _source_pdf points to something other than the seed paper,
            # meaning this entry was matched to its own PDF during F1 processing.
            if source_pdf in processed_pdf_names or source_pdf in available_pdfs:
                has_own_pdf = True

        if has_own_pdf:
            f1_with_pdf[citekey] = entry
        else:
            f1_missing_pdf[citekey] = entry

    # --- Open access lookup for missing papers ---
    oa_results = {}
    if check_oa and email and f1_missing_pdf:
        oa_results = _check_open_access(f1_missing_pdf, email)

    # Also check F2 OA availability for planning purposes.
    f2_oa_results = {}
    if check_oa and email and f2_entries:
        logger.info("Checking OA status for F2 references (for planning)...")
        f2_oa_results = _check_open_access(f2_entries, email)

    # --- Build report ---
    report = {
        "_metadata": build_coverage_metadata(config),
        "summary": {
            "total_bibliography_entries": len(bibliography),
            "seed_papers": len(seed_entries),
            "f1_references": len(f1_entries),
            "f1_with_pdf": len(f1_with_pdf),
            "f1_missing_pdf": len(f1_missing_pdf),
            "f1_coverage_percent": (
                round(100 * len(f1_with_pdf) / len(f1_entries), 1)
                if f1_entries else 0
            ),
            "f2_references": len(f2_entries),
            "f2_unique_references": len(f2_entries),
        },
        "f1_with_pdf": [
            _entry_summary(ck, entry)
            for ck, entry in sorted(f1_with_pdf.items())
        ],
        "f1_missing_pdf": [
            {
                **_entry_summary(ck, entry),
                "open_access": oa_results.get(ck, {}),
            }
            for ck, entry in sorted(f1_missing_pdf.items())
        ],
    }

    # --- F2 planning section ---
    f2_with_doi = {ck: e for ck, e in f2_entries.items() if e.get("doi")}
    f2_oa_available = {
        ck: info for ck, info in f2_oa_results.items()
        if info.get("is_oa")
    }

    report["f2_planning"] = {
        "description": (
            "Overview of F2 (second-generation) references to support "
            "planning for F2 corpus assembly. Shows which references have "
            "DOIs (enabling automated lookup) and which are openly accessible."
        ),
        "total_f2_references": len(f2_entries),
        "f2_with_doi": len(f2_with_doi),
        "f2_open_access_available": len(f2_oa_available),
        "f2_acquisition_estimate": {
            "freely_downloadable": len(f2_oa_available),
            "has_doi_but_not_oa": len(f2_with_doi) - len(f2_oa_available),
            "no_doi": len(f2_entries) - len(f2_with_doi),
        },
        "f2_open_access_list": [
            {
                "citekey": ck,
                "title": f2_entries[ck].get("title", ""),
                "doi": f2_entries[ck].get("doi", ""),
                "pdf_url": info.get("best_oa_url", ""),
                "oa_status": info.get("oa_status", ""),
            }
            for ck, info in sorted(f2_oa_available.items())
        ],
    }

    # --- Save report ---
    report_path = output_dir / "coverage_report.json"
    write_json(report, report_path)
    logger.info("Coverage report saved: %s", report_path)

    # --- Log summary ---
    logger.info(
        "F1 coverage: %d/%d (%.1f%%). Missing: %d. OA available: %d.",
        len(f1_with_pdf),
        len(f1_entries),
        report["summary"]["f1_coverage_percent"],
        len(f1_missing_pdf),
        sum(1 for r in oa_results.values() if r.get("is_oa")),
    )
    if f2_entries:
        logger.info(
            "F2 planning: %d total refs, %d with DOI, %d freely available.",
            len(f2_entries),
            len(f2_with_doi),
            len(f2_oa_available),
        )

    return report


def download_oa_papers(
    coverage_report: dict,
    download_dir: str | Path,
    generation: str = "F1",
) -> dict[str, bool]:
    """
    Download freely available open access PDFs.

    Reads the coverage report and downloads PDFs for entries that have
    OA PDF URLs available.

    Args:
        coverage_report: The coverage report dict (from generate_coverage_report).
        download_dir:    Directory to save downloaded PDFs.
        generation:      Which generation to download: "F1" or "F2".

    Returns:
        Dict mapping citekeys to success/failure booleans.
    """
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    if generation == "F1":
        entries = coverage_report.get("f1_missing_pdf", [])
    elif generation == "F2":
        entries = coverage_report.get("f2_planning", {}).get("f2_open_access_list", [])
    else:
        logger.error("Unknown generation: %s", generation)
        return {}

    # Filter to entries with OA PDF URLs.
    downloadable = []
    for entry in entries:
        oa_info = entry.get("open_access", entry)  # F1 vs F2 structure differs
        url = oa_info.get("best_oa_url", "") or oa_info.get("pdf_url", "")
        if url:
            downloadable.append((entry.get("citekey", ""), url))

    if not downloadable:
        logger.info("No OA PDFs available for download.")
        return {}

    logger.info("Downloading %d OA PDFs to %s...", len(downloadable), download_dir)
    results = {}

    n_dl = len(downloadable)
    for dl_idx, (citekey, url) in enumerate(downloadable, 1):
        logger.info("  Downloading [%d/%d]: %s", dl_idx, n_dl, citekey)
        success = _download_pdf(url, download_dir / f"{citekey}.pdf")
        results[citekey] = success
        # Polite delay between downloads.
        time.sleep(1)

    downloaded = sum(results.values())
    logger.info("Downloaded %d/%d PDFs.", downloaded, len(downloadable))

    return results


# =============================================================================
# Internal helpers
# =============================================================================

def _entry_summary(citekey: str, entry: dict) -> dict:
    """Build a concise summary of a bibliography entry for the report."""
    authors = entry.get("author", [])
    author_str = ""
    if authors:
        first = authors[0]
        author_str = first.get("family", "")
        if len(authors) > 1:
            author_str += " et al."

    return {
        "citekey": citekey,
        "title": entry.get("title", ""),
        "author": author_str,
        "year": entry.get("year", ""),
        "doi": entry.get("doi", ""),
        "source_pdf": entry.get("_source_pdf", ""),
    }


def _check_open_access(
    entries: dict[str, dict],
    email: str,
) -> dict[str, dict]:
    """
    Check open access status via the Unpaywall API.

    Only checks entries that have a DOI. Returns a dict mapping citekeys
    to OA info dicts.
    """
    results = {}
    entries_with_doi = {
        ck: e for ck, e in entries.items() if e.get("doi")
    }

    if not entries_with_doi:
        logger.info("No DOIs available for OA lookup.")
        return results

    logger.info(
        "Checking OA status for %d entries with DOIs (via Unpaywall)...",
        len(entries_with_doi),
    )

    n_oa = len(entries_with_doi)
    for oa_idx, (citekey, entry) in enumerate(entries_with_doi.items(), 1):
        if oa_idx % max(1, n_oa // 5) == 0 or oa_idx == n_oa:
            logger.debug("  OA lookup: %d/%d", oa_idx, n_oa)
        doi = entry["doi"]
        oa_info = _unpaywall_lookup(doi, email)
        if oa_info:
            results[citekey] = oa_info

        # Polite rate limiting (Unpaywall asks for max 100k/day,
        # but we add a small delay to be courteous).
        time.sleep(0.1)

    oa_count = sum(1 for r in results.values() if r.get("is_oa"))
    logger.info(
        "OA lookup complete: %d/%d are open access.",
        oa_count,
        len(entries_with_doi),
    )

    return results


def _unpaywall_lookup(doi: str, email: str) -> dict | None:
    """
    Query the Unpaywall API for a single DOI.

    Returns a dict with:
    - is_oa: bool
    - oa_status: str (gold, green, hybrid, bronze, closed)
    - best_oa_url: str (direct PDF URL if available)
    - oa_location: str (description of where the OA copy is)
    """
    # --- Robust DOI cleaning ---
    # GROBID sometimes extracts DOIs with URL prefixes, trailing punctuation,
    # embedded spaces, or other artifacts.
    doi = doi.strip()

    # Strip URL prefix variants.
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)

    # Strip trailing punctuation that isn't part of the DOI.
    # DOIs can contain most characters, but don't end in . , ; )
    # unless the ) is balanced with a ( in the DOI.
    doi = re.sub(r"[.,;]+$", "", doi)
    # Handle trailing ) only if unbalanced.
    if doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi.rstrip(")")

    # Strip whitespace that may have crept in.
    doi = doi.strip()

    if not doi:
        return None

    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"

    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            logger.debug("Unpaywall 404 for DOI: '%s' (URL: %s)", doi, url)
            return {"is_oa": False, "oa_status": "not_found", "best_oa_url": "", "doi_queried": doi}
        if resp.status_code == 422:
            logger.debug("Unpaywall 422 (invalid DOI) for: '%s'", doi)
            return {"is_oa": False, "oa_status": "invalid_doi", "best_oa_url": "", "doi_queried": doi}
        if resp.status_code != 200:
            logger.debug("Unpaywall HTTP %d for DOI: '%s'", resp.status_code, doi)
            return None

        data = resp.json()

        is_oa = data.get("is_oa", False)
        oa_status = data.get("oa_status", "closed")

        # Find the best OA location (prefer PDF URLs).
        best_url = ""
        oa_location = ""
        best_loc = data.get("best_oa_location")
        if best_loc:
            best_url = best_loc.get("url_for_pdf", "") or best_loc.get("url", "")
            oa_location = best_loc.get("host_type", "")
            if best_loc.get("repository_institution"):
                oa_location += f" ({best_loc['repository_institution']})"

        # Also check all OA locations if best_oa_location has no PDF URL.
        # Some green OA copies only appear in the oa_locations list.
        if is_oa and not best_url:
            for loc in data.get("oa_locations", []):
                pdf_url = loc.get("url_for_pdf", "")
                if pdf_url:
                    best_url = pdf_url
                    oa_location = loc.get("host_type", "")
                    break
            # If still no PDF URL, use any landing page URL.
            if not best_url:
                for loc in data.get("oa_locations", []):
                    page_url = loc.get("url", "")
                    if page_url:
                        best_url = page_url
                        oa_location = loc.get("host_type", "") + " (landing page)"
                        break

        return {
            "is_oa": is_oa,
            "oa_status": oa_status,
            "best_oa_url": best_url,
            "oa_location": oa_location,
            "doi_queried": doi,
        }

    except (requests.Timeout, requests.ConnectionError) as e:
        logger.debug("Unpaywall lookup failed for %s: %s", doi, e)
        return None
    except Exception as e:
        logger.debug("Unpaywall error for %s: %s", doi, e)
        return None


def _download_pdf(url: str, dest_path: Path) -> bool:
    """Download a PDF from a URL."""
    try:
        resp = requests.get(
            url,
            timeout=60,
            headers={"User-Agent": "BibVik-CitationAnalysis/0.1 (academic research)"},
            stream=True,
        )
        if resp.status_code != 200:
            logger.debug("Download failed (HTTP %d): %s", resp.status_code, url)
            return False

        # Check content type.
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type and "octet-stream" not in content_type:
            logger.debug("Not a PDF (Content-Type: %s): %s", content_type, url)
            return False

        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        logger.debug("Downloaded: %s", dest_path.name)
        return True

    except Exception as e:
        logger.debug("Download error for %s: %s", url, e)
        return False
