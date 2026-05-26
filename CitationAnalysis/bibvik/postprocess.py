"""
postprocess.py — Post-processing and data cleaning for bibliography.json

Run after --enrich to fix known artifact patterns in the bibliography.
Each fix is a discrete, auditable pass that reports how many entries
it modified. Fixes are applied in order; later passes may depend on
earlier ones (e.g. type correction before field validation).

Usage:
    python3 postprocess.py [--dry-run] [--input PATH] [--output PATH]

Passes (in order):
    1.  Strip letter prefix from titles        "a: Title" → "Title"
    2.  Join hyphenated line-break titles       "Conti-\nnuity" → "Continuity"
    3.  Truncate oversized titles               Titles > N chars are likely garbage
    4.  Normalize DOI format                    "https://doi.org/10.x" → "10.x"
    5.  Normalize date to year                  "2016-01" → "2016"
    6.  Fix page range artifacts                "157--e168" → "157--168"
    7.  Strip location/publisher from title     Leaked raw string artifacts
    8.  Fix ALL CAPS titles                     Already handled in normalize.py but check
    9.  Remove LLM placeholder titles           "Article by X", "Статья В. И. X"
    10. Fix entry type for articles/books       Heuristic re-classification
    11. Flag compound citations                 Raw strings containing multiple works
    12. Flag cross-script duplicates            Cyrillic + romanised same work
    13. Flag orphaned cited_by                  cited_by citekeys not in bibliography
    14. Flag missing given names                author given: ""
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _report(pass_name: str, count: int, total: int):
    logger.info("%-45s  %4d / %d modified", pass_name, count, total)


# ── Pass 1: Strip letter prefix from titles ───────────────────────────────────

def fix_letter_prefix(bib: dict) -> int:
    """'a: Title' → 'Title' (year suffix artifact from GROBID parsing '2016a:')"""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        fixed = re.sub(r"^[a-z]\s*:\s*", "", title).strip()
        if fixed != title:
            entry["title"] = fixed
            count += 1
    return count


# ── Pass 2: Join hyphenated line-break titles ─────────────────────────────────

def fix_hyphenated_titles(bib: dict) -> int:
    """'Conti-\nnuity' → 'Continuity'; 'Conti-' (truncated) → flag"""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        # Join hyphenated line breaks
        fixed = re.sub(r"-\s*\n\s*", "", title)
        # Also catch trailing hyphen (truncated extraction)
        fixed = re.sub(r"-\s*$", "", fixed).strip()
        if fixed != title:
            entry["title"] = fixed
            count += 1
    return count


# ── Pass 3: Truncate oversized titles ────────────────────────────────────────

MAX_TITLE_LENGTH = 300

def fix_oversized_titles(bib: dict) -> int:
    """Titles longer than MAX_TITLE_LENGTH chars are likely raw citation blowout."""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        if len(title) > MAX_TITLE_LENGTH:
            entry["title"] = ""
            entry["_title_too_long"] = title[:200]  # preserve for inspection
            count += 1
    return count


# ── Pass 4: Normalize DOI format ─────────────────────────────────────────────

def fix_doi_format(bib: dict) -> int:
    """'https://doi.org/10.x' or 'http://dx.doi.org/10.x' → '10.x'"""
    count = 0
    for entry in bib.values():
        doi = entry.get("doi", "")
        if not doi:
            continue
        fixed = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi).strip()
        if fixed != doi:
            entry["doi"] = fixed
            count += 1
    return count


# ── Pass 5: Normalize date to year ───────────────────────────────────────────

def fix_date_format(bib: dict) -> int:
    """'2016-01' or '2016-01-15' → '2016'"""
    count = 0
    for entry in bib.values():
        date = entry.get("date", "")
        if not date:
            continue
        m = re.match(r"^(\d{4})", date)
        if m and date != m.group(1):
            entry["date"] = m.group(1)
            count += 1
    return count


# ── Pass 6: Fix page range artifacts ─────────────────────────────────────────

def fix_page_ranges(bib: dict) -> int:
    """'157--e168' → '157--168'; '6-30' → '6--30'"""
    count = 0
    for entry in bib.values():
        pages = entry.get("pages", "")
        if not pages:
            continue
        fixed = re.sub(r"e(\d)", r"\1", pages)   # remove spurious 'e'
        fixed = re.sub(r"(?<!-)-(?!-)", "--", fixed)  # normalize single dash
        if fixed != pages:
            entry["pages"] = fixed
            count += 1
    return count


# ── Pass 7: Remove LLM placeholder titles ────────────────────────────────────

LLM_PLACEHOLDER_PATTERNS = [
    r"^(article|статья|стаття)\s+(by|в\.?\s*и\.?|від)\b",
    r"^unknown\s+title",
    r"^\[untitled\]",
    r"^no\s+title",
]

def fix_llm_placeholder_titles(bib: dict) -> int:
    """Remove titles that are LLM-generated placeholders rather than real titles."""
    count = 0
    patterns = [re.compile(p, re.IGNORECASE) for p in LLM_PLACEHOLDER_PATTERNS]
    for entry in bib.values():
        title = entry.get("title", "")
        if any(p.match(title) for p in patterns):
            entry["_placeholder_title"] = title
            entry["title"] = ""
            count += 1
    return count


# ── Pass 8: Flag compound citations ──────────────────────────────────────────

def flag_compound_citations(bib: dict) -> int:
    """Flag entries where raw citation string appears to contain multiple works."""
    count = 0
    for entry in bib.values():
        raw = entry.get("_raw_citation", "")
        if not raw:
            continue
        # Heuristic: multiple year occurrences suggest compound citation
        years = re.findall(r"\b(1[89]\d{2}|20[012]\d)\b", raw)
        if len(set(years)) >= 3:
            entry["_possibly_compound"] = True
            count += 1
    return count


# ── Pass 9: Flag cross-script duplicates ─────────────────────────────────────

# ── Pass 9: Flag cross-script duplicates ─────────────────────────────────────

# Cyrillic → Latin transliteration table (covers Russian, Ukrainian, Bulgarian)
_CYRILLIC_TO_LATIN = str.maketrans({
    'а': 'a',  'б': 'b',  'в': 'v',  'г': 'g',  'д': 'd',
    'е': 'e',  'ё': 'yo', 'ж': 'zh', 'з': 'z',  'и': 'i',
    'й': 'y',  'к': 'k',  'л': 'l',  'м': 'm',  'н': 'n',
    'о': 'o',  'п': 'p',  'р': 'r',  'с': 's',  'т': 't',
    'у': 'u',  'ф': 'f',  'х': 'kh', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'shch','ъ': '',  'ы': 'y',  'ь': '',
    'э': 'e',  'ю': 'yu', 'я': 'ya',
    # Ukrainian
    'є': 'ye', 'і': 'i',  'ї': 'yi', 'ґ': 'g',
})

def _transliterate(s: str) -> str:
    return s.lower().translate(_CYRILLIC_TO_LATIN)

def _is_cyrillic(s: str) -> bool:
    return bool(re.search(r'[\u0400-\u04FF]', s))

def _author_key(authors: list) -> str:
    """First author family name, transliterated and lowercased."""
    if not authors:
        return ""
    return _transliterate(authors[0].get("family", "").lower())

def flag_cross_script_duplicates(bib: dict) -> int:
    """
    Flag entries that are likely the same work in Cyrillic and Latin script.
    Groups by (year, first_author_transliterated) and flags pairs where one
    entry has a Cyrillic title/author and the other has a Latin equivalent.
    """
    from collections import defaultdict

    # Build index: (year, author_key) → list of citekeys
    index: dict[tuple, list] = defaultdict(list)
    for ck, entry in bib.items():
        year = entry.get("date", "")[:4]
        ak   = _author_key(entry.get("author", []))
        if year and ak:
            index[(year, ak)].append(ck)

    count = 0
    for (year, ak), citekeys in index.items():
        if len(citekeys) < 2:
            continue
        # Check if any pair has one Cyrillic and one Latin title
        entries = [(ck, bib[ck]) for ck in citekeys]
        cyrillic = [(ck, e) for ck, e in entries if _is_cyrillic(e.get("title", "") + e.get("author", [{}])[0].get("family", ""))]
        latin    = [(ck, e) for ck, e in entries if not _is_cyrillic(e.get("title", "") + e.get("author", [{}])[0].get("family", ""))]
        if cyrillic and latin:
            for ck, e in cyrillic + latin:
                if "_cross_script_duplicate_of" not in e:
                    other_cks = [k for k, _ in (latin if (ck, e) in cyrillic else cyrillic)]
                    e["_cross_script_duplicate_candidate"] = other_cks
                    count += 1

    return count


# ── Pass 10: Flag orphaned cited_by ──────────────────────────────────────────

def flag_orphaned_cited_by(bib: dict) -> int:
    """Flag entries whose cited_by citekeys don't exist in the bibliography."""
    count = 0
    all_keys = set(bib.keys())
    for entry in bib.values():
        orphaned = [ck for ck in entry.get("cited_by", []) if ck not in all_keys]
        if orphaned:
            entry["_orphaned_cited_by"] = orphaned
            count += 1
    return count


# ── Main ─────────────────────────────────────────────────────────────────────

PASSES = [
    ("Strip letter prefix from titles",     fix_letter_prefix),
    ("Join hyphenated line-break titles",   fix_hyphenated_titles),
    ("Truncate oversized titles",           fix_oversized_titles),
    ("Normalize DOI format",               fix_doi_format),
    ("Normalize date to year",             fix_date_format),
    ("Fix page range artifacts",           fix_page_ranges),
    ("Remove LLM placeholder titles",      fix_llm_placeholder_titles),
    ("Flag compound citations",            flag_compound_citations),
    ("Flag cross-script duplicates",       flag_cross_script_duplicates),
    ("Flag orphaned cited_by",             flag_orphaned_cited_by),
]


def run_postprocess(input_path: Path, output_path: Path | None = None) -> dict:
    """
    Run all post-processing passes on bibliography.json.
    Returns dict of {pass_name: count_modified}.
    """
    input_path = Path(input_path)
    output_path = Path(output_path) if output_path else input_path

    logger.info("Loading %s ...", input_path)
    bib = json.loads(input_path.read_text(encoding="utf-8"))
    total = len(bib)
    logger.info("Loaded %d entries.", total)

    results = {}
    for name, fn in PASSES:
        count = fn(bib)
        _report(name, count, total)
        results[name] = count

    output_path.write_text(
        json.dumps(bib, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("Written to %s", output_path)
    return results


def main():
    parser = argparse.ArgumentParser(description="Post-process bibliography.json")
    parser.add_argument("--input",   default="/home/zack/models/BibVik_output/bibliography.json")
    parser.add_argument("--output",  default=None, help="Output path (default: overwrite input)")
    parser.add_argument("--dry-run", action="store_true", help="Report without modifying")
    args = parser.parse_args()

    input_path  = Path(args.input)
    output_path = Path(args.output) if args.output else input_path

    bib = json.loads(input_path.read_text(encoding="utf-8"))
    total = len(bib)
    logger.info("Loaded %d entries.", total)

    for name, fn in PASSES:
        count = fn(bib)
        _report(name, count, total)

    if args.dry_run:
        logger.info("Dry run — no changes written.")
    else:
        output_path.write_text(
            json.dumps(bib, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Written to %s", output_path)


if __name__ == "__main__":
    main()