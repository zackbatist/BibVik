"""
bibvik.coverage — PDF coverage reporting and open access acquisition.

Answers two practical questions:

  1. Which F1 references in the bibliography don't have PDFs yet?
     (So you know what to acquire.)

  2. Which of those are freely available open access?
     (So you know which are freely acquirable.)

Output is a plain Markdown file (`output/coverage.md`) with two lists,
designed to be read directly rather than processed downstream.

Unpaywall API
-------------
Free tier requires only an email address (for polite usage tracking).
Endpoint: https://api.unpaywall.org/v2/{doi}?email={email}
Returns OA status and direct PDF links where available.
Rate limit: ~100k requests/day.


"""

import logging
import re
import time
from pathlib import Path

import requests

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
    Write a Markdown coverage report to output_dir/coverage.md.

    Reports which F1 references are missing PDFs and which of those
    are openly available. Returns a summary dict for logging.

    Args:
        bibliography:     Full bibliography dict from graph state.
        processed_papers: Processed paper data from graph state.
        f1_pdf_dir:       Directory where F1 PDFs are stored.
        config:           Full config dict.
        output_dir:       Where to write coverage.md.
        email:            Email for Unpaywall API (required for OA lookups).
        check_oa:         Whether to check open access status via Unpaywall.

    Returns:
        Summary dict with counts for logging.
    """
    output_dir = Path(output_dir)
    f1_pdf_dir = Path(f1_pdf_dir)
    seed_pdf_name = Path(config.get("seed_paper", "")).name

    # ── Separate F1 entries ───────────────────────────────────────────────────
    f1_entries = {
        ck: e for ck, e in bibliography.items()
        if e.get("generation") == "F1"
    }

    processed_pdf_names = set(processed_papers.keys())
    available_pdfs = set()
    if f1_pdf_dir.is_dir():
        available_pdfs = {p.name for p in f1_pdf_dir.glob("*.pdf")}

    f1_with_pdf = {}
    f1_missing_pdf = {}

    for ck, entry in f1_entries.items():
        source_pdf = entry.get("_source_pdf", "")
        has_own_pdf = (
            source_pdf
            and source_pdf != seed_pdf_name
            and (source_pdf in processed_pdf_names or source_pdf in available_pdfs)
        )
        if has_own_pdf:
            f1_with_pdf[ck] = entry
        else:
            f1_missing_pdf[ck] = entry

    # ── OA lookup for missing papers ──────────────────────────────────────────
    oa_results: dict[str, dict] = {}
    if check_oa and email and f1_missing_pdf:
        oa_results = _check_open_access(f1_missing_pdf, email)

    # ── Write Markdown report ─────────────────────────────────────────────────
    _write_report(
        output_dir     = output_dir,
        f1_with_pdf    = f1_with_pdf,
        f1_missing_pdf = f1_missing_pdf,
        oa_results     = oa_results,
        check_oa       = check_oa and bool(email),
    )

    summary = {
        "f1_total":        len(f1_entries),
        "f1_with_pdf":     len(f1_with_pdf),
        "f1_missing_pdf":  len(f1_missing_pdf),
        "f1_coverage_pct": (
            round(100 * len(f1_with_pdf) / len(f1_entries), 1)
            if f1_entries else 0
        ),
        "oa_available": sum(1 for r in oa_results.values() if r.get("is_oa")),
    }

    logger.info(
        "F1 coverage: %d/%d (%.1f%%). Missing: %d. OA available: %d.",
        summary["f1_with_pdf"],
        summary["f1_total"],
        summary["f1_coverage_pct"],
        summary["f1_missing_pdf"],
        summary["oa_available"],
    )

    return summary


def download_oa_papers(
    bibliography: dict[str, dict],
    download_dir: str | Path,
    email: str,
) -> dict[str, bool]:
    """
    Download freely available OA PDFs for F1 papers with DOIs.

    Args:
        bibliography: Full bibliography dict.
        download_dir: Directory to save downloaded PDFs.
        email:        Email for Unpaywall API.

    Returns:
        Dict mapping citekeys to success/failure booleans.
    """
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    entries_with_doi = {
        ck: e for ck, e in bibliography.items()
        if e.get("generation") == "F1" and e.get("doi")
    }

    if not entries_with_doi:
        logger.info("No F1 entries with DOIs available for OA download.")
        return {}

    logger.info("Checking OA status for %d entries with DOIs...", len(entries_with_doi))
    oa_results = _check_open_access(entries_with_doi, email)

    downloadable = [
        (ck, info["best_oa_url"])
        for ck, info in oa_results.items()
        if info.get("is_oa") and info.get("best_oa_url")
    ]

    if not downloadable:
        logger.info("No OA PDFs available for download.")
        return {}

    logger.info("Downloading %d OA PDFs to %s...", len(downloadable), download_dir)
    results = {}
    for ck, url in downloadable:
        logger.info("  Downloading: %s", ck)
        results[ck] = _download_pdf(url, download_dir / f"{ck}.pdf")
        time.sleep(1)

    logger.info("Downloaded %d/%d PDFs.", sum(results.values()), len(downloadable))
    return results


# =============================================================================
# Report rendering
# =============================================================================

def _write_report(
    output_dir: Path,
    f1_with_pdf: dict[str, dict],
    f1_missing_pdf: dict[str, dict],
    oa_results: dict[str, dict],
    check_oa: bool,
) -> None:
    """Write coverage.md to output_dir."""
    lines = [
        "# BibVik — PDF Coverage Report",
        "",
        f"F1 references with PDFs: {len(f1_with_pdf)}  ",
        f"F1 references missing PDFs: {len(f1_missing_pdf)}  ",
        "",
        "## Missing PDFs",
        "",
        "The following F1 references do not have PDFs in the corpus. "
        "These papers have not been processed for citations.",
        "",
    ]

    if not f1_missing_pdf:
        lines += ["*All F1 references have PDFs.*", ""]
    else:
        if check_oa:
            oa_count = sum(1 for r in oa_results.values() if r.get("is_oa"))
            lines += [f"{oa_count} of these are openly available (marked below).", ""]

        for ck, entry in sorted(f1_missing_pdf.items()):
            authors = _format_authors(entry.get("author", []))
            year = entry.get("date", entry.get("year", "n.d."))
            title = entry.get("title", "*(no title)*")
            doi = entry.get("doi", "")
            oa = oa_results.get(ck, {})

            line = f"- **{ck}** — {authors} ({year}). {title}."
            if doi:
                line += f" DOI: {doi}."
            if oa.get("is_oa") and oa.get("best_oa_url"):
                line += f" **OA: {oa['best_oa_url']}**"
            elif oa.get("is_oa"):
                line += " *(OA available, no direct PDF URL)*"
            lines += [line]

        lines += [""]

    if check_oa and oa_results:
        oa_available = {
            ck: info for ck, info in oa_results.items()
            if info.get("is_oa") and info.get("best_oa_url")
        }
        if oa_available:
            lines += [
                "## Freely available (open access)",
                "",
                f"{len(oa_available)} missing papers have direct OA PDF links "
                "and can be downloaded automatically with `--download-oa`.",
                "",
            ]
            for ck, info in sorted(oa_available.items()):
                entry = f1_missing_pdf.get(ck, {})
                title = entry.get("title", "*(no title)*")
                lines += [
                    f"- **{ck}** — {title}  ",
                    f"  {info['best_oa_url']}",
                ]
            lines += [""]

    if not check_oa or not oa_results:
        lines += [
            "## Open access",
            "",
            "*OA lookup not run. Pass `--email your@email.com` to enable.*",
            "",
        ]

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "coverage.md").write_text("\n".join(lines), encoding="utf-8")
    logger.info("Coverage report written: %s", output_dir / "coverage.md")


def _format_authors(authors: list[dict]) -> str:
    if not authors:
        return "*(unknown)*"
    name = authors[0].get("family", "")
    if len(authors) > 1:
        name += " et al."
    return name


# =============================================================================
# Unpaywall
# =============================================================================

def _check_open_access(
    entries: dict[str, dict],
    email: str,
) -> dict[str, dict]:
    """Check OA status via Unpaywall for all entries that have a DOI."""
    results = {}
    entries_with_doi = {ck: e for ck, e in entries.items() if e.get("doi")}

    if not entries_with_doi:
        logger.info("No DOIs available for OA lookup.")
        return results

    logger.info(
        "Checking OA status for %d entries with DOIs (via Unpaywall)...",
        len(entries_with_doi),
    )

    for ck, entry in entries_with_doi.items():
        oa_info = _unpaywall_lookup(entry["doi"], email)
        if oa_info:
            results[ck] = oa_info
        time.sleep(0.1)

    oa_count = sum(1 for r in results.values() if r.get("is_oa"))
    logger.info("OA lookup complete: %d/%d are open access.", oa_count, len(entries_with_doi))
    return results


def _unpaywall_lookup(doi: str, email: str) -> dict | None:
    """Query Unpaywall for a single DOI. Returns OA info dict or None."""
    doi = doi.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"[.,;]+$", "", doi)
    if doi.endswith(")") and doi.count("(") < doi.count(")"):
        doi = doi.rstrip(")")
    doi = doi.strip()
    if not doi:
        return None

    url = f"https://api.unpaywall.org/v2/{doi}?email={email}"
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 404:
            return {"is_oa": False, "oa_status": "not_found", "best_oa_url": ""}
        if resp.status_code == 422:
            return {"is_oa": False, "oa_status": "invalid_doi", "best_oa_url": ""}
        if resp.status_code != 200:
            logger.debug("Unpaywall HTTP %d for DOI: %s", resp.status_code, doi)
            return None

        data = resp.json()
        is_oa = data.get("is_oa", False)
        oa_status = data.get("oa_status", "closed")
        best_url = ""

        best_loc = data.get("best_oa_location")
        if best_loc:
            best_url = best_loc.get("url_for_pdf", "") or best_loc.get("url", "")

        if is_oa and not best_url:
            for loc in data.get("oa_locations", []):
                if loc.get("url_for_pdf"):
                    best_url = loc["url_for_pdf"]
                    break
            if not best_url:
                for loc in data.get("oa_locations", []):
                    if loc.get("url"):
                        best_url = loc["url"]
                        break

        return {"is_oa": is_oa, "oa_status": oa_status, "best_oa_url": best_url}

    except (requests.Timeout, requests.ConnectionError) as e:
        logger.debug("Unpaywall lookup failed for %s: %s", doi, e)
        return None


def _download_pdf(url: str, dest_path: Path) -> bool:
    """Download a PDF from a URL."""
    try:
        resp = requests.get(
            url, timeout=60,
            headers={"User-Agent": "BibVik-CitationAnalysis/0.1 (academic research)"},
            stream=True,
        )
        if resp.status_code != 200:
            return False
        content_type = resp.headers.get("Content-Type", "")
        if "pdf" not in content_type and "octet-stream" not in content_type:
            return False
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        return True
    except Exception:
        return False