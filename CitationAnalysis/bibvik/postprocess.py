"""
postprocess.py — Post-processing and data cleaning for bibliography.json

Run after --enrich to fix known artifact patterns in the bibliography.
Each fix is a discrete, auditable pass that reports how many entries
it modified. Fixes are applied in order; later passes may depend on
earlier ones (e.g. type correction before field validation).

Usage:
    python3 -m bibvik.postprocess [--dry-run] [--input PATH] [--output PATH]

Passes (in order):
    1.  Strip letter prefix from titles        "a: Title" → "Title"
    2.  Join hyphenated line-break titles       "Conti-\nnuity" → "Continuity"
    3.  Flag oversized titles               Titles > N chars flagged as _title_too_long
    4.  Normalize DOI format                    "https://doi.org/10.x" → "10.x"
    5.  Normalize date to year                  "2016-01" → "2016"
    6.  Fix page range artifacts                "157--e168" → "157--168"
    7.  Extract volume from pages field         "87, pp. 6-30" → pages: 6-30, volume: 87
    8.  Fix ALL CAPS titles                     Slipped through normalize.py
    9.  Remove LLM placeholder titles           "Article by X", "Статья В. И. X"
    10. Reclassify entry types                  Heuristic re-classification
    11. Flag citekey suffix collisions          Same work with different suffix
    12. Flag compound citations                 Raw strings containing multiple works
    13. Flag cross-script duplicates            Cyrillic + romanised same work
    14. Flag citing paper not in corpus         cited_by citekeys not in bibliography
    15. Flag title contains publisher/location  Leaked raw string artifacts
    16. Flag near-duplicate entries             Same author+year, similar title
    17. Flag missing given names                author given: ""
    18. Flag editor/author confusion            "(ed.)" in author name field
    19. Flag unprocessed source PDFs            _source_pdf not in processed papers
"""

import argparse
import json
import logging
import re
from collections import defaultdict
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _report(pass_name: str, count: int, total: int):
    logger.info("%-50s  %4d / %d", pass_name, count, total)


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
    """'Conti-\nnuity' → 'Continuity'; trailing hyphen → strip"""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        fixed = re.sub(r"-\s*\n\s*", "", title)
        fixed = re.sub(r"-\s*$", "", fixed).strip()
        if fixed != title:
            entry["title"] = fixed
            count += 1
    return count


# ── Pass 3: Truncate oversized titles ────────────────────────────────────────

MAX_TITLE_LENGTH = 300

def fix_oversized_titles(bib: dict) -> int:
    """Flag titles longer than MAX_TITLE_LENGTH chars — likely raw citation blowout."""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        if len(title) > MAX_TITLE_LENGTH:
            entry["_title_too_long"] = True
            count += 1
    return count


# ── Pass 4: Normalize DOI format ─────────────────────────────────────────────

def fix_doi_format(bib: dict) -> int:
    """'https://doi.org/10.x' → '10.x'"""
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
        fixed = re.sub(r"e(\d)", r"\1", pages)
        fixed = re.sub(r"(?<!-)-(?!-)", "--", fixed)
        if fixed != pages:
            entry["pages"] = fixed
            count += 1
    return count


# ── Pass 7: Extract volume from pages field ───────────────────────────────────

def fix_volume_in_pages(bib: dict) -> int:
    """'87, pp. 6-30' → pages: '6--30', volume: '87'"""
    count = 0
    for entry in bib.values():
        pages = entry.get("pages", "")
        if not pages:
            continue
        m = re.match(r"^(\d+)\s*[,:]?\s*(?:pp?\.)?\s*(\d+\s*[-–]\s*\d+)$", pages)
        if m:
            vol, pg = m.group(1), m.group(2)
            if not entry.get("volume"):
                entry["volume"] = vol
            entry["pages"] = re.sub(r"(?<!-)-(?!-)", "--", pg).strip()
            count += 1
    return count


# ── Pass 8: Fix ALL CAPS titles ──────────────────────────────────────────────

def fix_allcaps_titles(bib: dict) -> int:
    """Title-case titles that are entirely uppercase."""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        if not title:
            continue
        alpha = [c for c in title if c.isalpha()]
        if alpha and all(c.isupper() for c in alpha):
            entry["title"] = title.title()
            count += 1
    return count


# ── Pass 9: Remove LLM placeholder titles ────────────────────────────────────

LLM_PLACEHOLDER_PATTERNS = [
    r"^(article|статья|стаття)\s+(by|в\.?\s*и\.?|від)\b",
    r"^unknown\s+title",
    r"^\[untitled\]",
    r"^no\s+title",
]

def fix_llm_placeholder_titles(bib: dict) -> int:
    """Remove LLM-generated placeholder titles."""
    count = 0
    patterns = [re.compile(p, re.IGNORECASE) for p in LLM_PLACEHOLDER_PATTERNS]
    for entry in bib.values():
        title = entry.get("title", "")
        if any(p.match(title) for p in patterns):
            entry["_placeholder_title"] = title
            entry["title"] = ""
            count += 1
    return count


# ── Pass 10: Reclassify entry types ──────────────────────────────────────────

def fix_entry_types(bib: dict) -> int:
    """Heuristic entry type reclassification based on available fields.

    Article reclassification requires volume OR pages in addition to journaltitle,
    to avoid publisher/series names in journaltitle triggering false article classification.
    """
    count = 0
    for entry in bib.values():
        old_type  = entry.get("entry_type", "")
        journal   = entry.get("journaltitle", "").strip()
        booktitle = entry.get("booktitle", "").strip()
        editors   = entry.get("editor", [])
        isbn      = entry.get("isbn", "").strip()
        volume    = entry.get("volume", "").strip()
        pages     = entry.get("pages", "").strip()
        number    = entry.get("number", "").strip()

        # Only reclassify to article if journaltitle is accompanied by
        # volume, issue, or pages — publisher/series names in journaltitle
        # should not trigger article classification
        if journal and (volume or pages or number):
            new_type = "article"
        elif booktitle and editors:
            new_type = "incollection"
        elif booktitle and not editors:
            new_type = "inbook"
        elif isbn and not journal:
            new_type = "book"
        else:
            continue

        # Don't downgrade incollection to inbook — missing editor data
        # should not change the entry type since the booktitle structure
        # implies an edited volume
        if old_type == "incollection" and new_type == "inbook":
            continue

        if new_type != old_type:
            entry["entry_type"] = new_type
            entry["_entry_type_original"] = old_type
            count += 1
    return count


# ── Pass 11: Flag citekey suffix collisions ───────────────────────────────────

def flag_citekey_suffix_collisions(bib: dict) -> int:
    """
    Flag entries where the base citekey (without a/b/c suffix) matches another
    entry — e.g. 'bill2016' and 'bill2016a' may be the same work cited differently.
    """
    count = 0
    base_to_citekeys: dict[str, list] = defaultdict(list)
    for ck in bib:
        base = re.sub(r"[a-z]$", "", ck)  # strip trailing letter suffix
        base_to_citekeys[base].append(ck)

    for base, citekeys in base_to_citekeys.items():
        if len(citekeys) < 2:
            continue
        for ck in citekeys:
            if "_citekey_collision" not in bib[ck]:
                bib[ck]["_citekey_collision"] = [k for k in citekeys if k != ck]
                count += 1
    return count


# ── Pass 12: Flag compound citations ──────────────────────────────────────────

def flag_compound_citations(bib: dict) -> int:
    """Flag entries where raw citation contains multiple distinct works."""
    count = 0
    for entry in bib.values():
        raw = entry.get("_raw_citation", "")
        if not raw:
            continue
        years = re.findall(r"\b(1[89]\d{2}|20[012]\d)\b", raw)
        if len(set(years)) >= 3:
            entry["_possibly_compound"] = True
            count += 1
    return count


# ── Pass 13: Flag cross-script duplicates ────────────────────────────────────

_CYRILLIC_TO_LATIN = str.maketrans({
    'а': 'a',  'б': 'b',  'в': 'v',  'г': 'g',  'д': 'd',
    'е': 'e',  'ё': 'yo', 'ж': 'zh', 'з': 'z',  'и': 'i',
    'й': 'y',  'к': 'k',  'л': 'l',  'м': 'm',  'н': 'n',
    'о': 'o',  'п': 'p',  'р': 'r',  'с': 's',  'т': 't',
    'у': 'u',  'ф': 'f',  'х': 'kh', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y',  'ь': '',
    'э': 'e',  'ю': 'yu', 'я': 'ya',
    'є': 'ye', 'і': 'i',  'ї': 'yi', 'ґ': 'g',
})

def _transliterate(s: str) -> str:
    return s.lower().translate(_CYRILLIC_TO_LATIN)

def _is_cyrillic(s: str) -> bool:
    return bool(re.search(r'[\u0400-\u04FF]', s))

def _author_key(authors: list) -> str:
    if not authors:
        return ""
    return _transliterate(authors[0].get("family", "").lower())

def flag_cross_script_duplicates(bib: dict) -> int:
    """
    Flag entries that are likely the same work in Cyrillic and Latin script.
    Groups by (year, transliterated first author) and flags pairs where one
    entry is Cyrillic and the other Latin.
    """
    index: dict[tuple, list] = defaultdict(list)
    for ck, entry in bib.items():
        year = entry.get("date", "")[:4]
        ak   = _author_key(entry.get("author", []))
        if year and ak:
            index[(year, ak)].append(ck)

    count = 0
    for citekeys in index.values():
        if len(citekeys) < 2:
            continue
        entries  = [(ck, bib[ck]) for ck in citekeys]
        cyrillic = [(ck, e) for ck, e in entries
                    if _is_cyrillic(e.get("title", "") + (e.get("author") or [{}])[0].get("family", ""))]
        latin    = [(ck, e) for ck, e in entries
                    if not _is_cyrillic(e.get("title", "") + (e.get("author") or [{}])[0].get("family", ""))]
        if cyrillic and latin:
            for ck, e in cyrillic + latin:
                if "_cross_script_duplicate_candidate" not in e:
                    other = [k for k, _ in (latin if (ck, e) in cyrillic else cyrillic)]
                    e["_cross_script_duplicate_candidate"] = other
                    count += 1
    return count


# ── Pass 14: Flag citing paper not in corpus ─────────────────────────────────

def flag_citing_paper_not_in_corpus(bib: dict) -> int:
    """
    Flag entries whose cited_by citekeys don't exist in the bibliography.
    This signals that the citing paper was detected but not processed —
    a gap in corpus coverage rather than a data error.
    """
    count = 0
    all_keys = set(bib.keys())
    for entry in bib.values():
        missing = [ck for ck in entry.get("cited_by", []) if ck not in all_keys]
        if missing:
            entry["_citing_paper_not_in_corpus"] = missing
            count += 1
    return count


# ── Pass 15: Flag title contains publisher/location ──────────────────────────

# Common publisher/location strings that leak into titles from raw citation parsing
_PUBLISHER_PATTERNS = re.compile(
    r"\b(Ashgate|Routledge|Brill|Springer|Cambridge University Press|Oxford University Press"
    r"|De Gruyter|Wiley|Blackwell|MIT Press|University of Chicago Press"
    r"|Stockholm|Copenhagen|Oslo|Uppsala|Aarhus|Helsinki|London|Oxford|Cambridge"
    r"|New York|Amsterdam|Berlin|Paris)\b",
    re.IGNORECASE,
)

def flag_title_contains_publisher(bib: dict) -> int:
    """Flag entries where the title appears to contain publisher or location names."""
    count = 0
    for entry in bib.values():
        title = entry.get("title", "")
        if title and _PUBLISHER_PATTERNS.search(title):
            entry["_title_may_contain_publisher"] = True
            count += 1
    return count


# ── Pass 16: Flag near-duplicate entries ─────────────────────────────────────

def flag_near_duplicates(bib: dict) -> int:
    """
    Flag entries with the same year and first author that have similar titles.
    Uses simple token overlap as a fast similarity proxy.
    """
    count = 0

    def _title_tokens(title: str) -> set:
        return set(re.findall(r"\b\w{4,}\b", title.lower()))

    # Group by (year, author_key)
    index: dict[tuple, list] = defaultdict(list)
    for ck, entry in bib.items():
        year = entry.get("date", "")[:4]
        ak   = _author_key(entry.get("author", []))
        if year and ak:
            index[(year, ak)].append(ck)

    for citekeys in index.values():
        if len(citekeys) < 2:
            continue
        pairs = [(citekeys[i], citekeys[j])
                 for i in range(len(citekeys))
                 for j in range(i + 1, len(citekeys))]
        for ck_a, ck_b in pairs:
            ta = _title_tokens(bib[ck_a].get("title", ""))
            tb = _title_tokens(bib[ck_b].get("title", ""))
            if not ta or not tb:
                continue
            overlap = len(ta & tb) / min(len(ta), len(tb))
            if overlap >= 0.7:
                bib[ck_a].setdefault("_near_duplicate_candidate", [])
                if ck_b not in bib[ck_a]["_near_duplicate_candidate"]:
                    bib[ck_a]["_near_duplicate_candidate"].append(ck_b)
                    count += 1
                bib[ck_b].setdefault("_near_duplicate_candidate", [])
                if ck_a not in bib[ck_b]["_near_duplicate_candidate"]:
                    bib[ck_b]["_near_duplicate_candidate"].append(ck_a)
    return count


# ── Pass 17: Flag missing given names ────────────────────────────────────────

def flag_missing_given_names(bib: dict) -> int:
    """Flag entries where any author has an empty given name."""
    count = 0
    for entry in bib.values():
        authors = entry.get("author", [])
        if any(not a.get("given", "").strip() for a in authors if a.get("family", "").strip()):
            entry["_missing_given_names"] = True
            count += 1
    return count


# ── Pass 18: Flag editor/author confusion ────────────────────────────────────

def flag_editor_author_confusion(bib: dict) -> int:
    """Flag entries where an author name contains editor indicators like '(ed.)'"""
    count = 0
    ed_pattern = re.compile(r"\(\s*(?:ed|hrsg|red|dir)[\.\)]", re.IGNORECASE)
    for entry in bib.values():
        for a in entry.get("author", []):
            name = f"{a.get('family', '')} {a.get('given', '')}"
            if ed_pattern.search(name):
                entry["_possible_editor_as_author"] = True
                count += 1
                break
    return count


# ── Pass 19: Flag unprocessed source PDFs ────────────────────────────────────

def flag_unprocessed_source_pdfs(bib: dict, processed_pdfs: set | None = None) -> int:
    """
    Flag entries whose _source_pdf was not successfully processed.
    If processed_pdfs is not provided, this pass is skipped.
    """
    if processed_pdfs is None:
        return 0
    count = 0
    for entry in bib.values():
        src = entry.get("_source_pdf", "")
        if src and src not in processed_pdfs:
            entry["_source_pdf_not_processed"] = True
            count += 1
    return count


# ── Main ─────────────────────────────────────────────────────────────────────

PASSES = [
    ("Strip letter prefix from titles",           fix_letter_prefix),
    ("Join hyphenated line-break titles",         fix_hyphenated_titles),
    ("Flag oversized titles",                     fix_oversized_titles),
    ("Normalize DOI format",                      fix_doi_format),
    ("Normalize date to year",                    fix_date_format),
    ("Fix page range artifacts",                  fix_page_ranges),
    ("Extract volume from pages field",           fix_volume_in_pages),
    ("Fix ALL CAPS titles",                       fix_allcaps_titles),
    ("Remove LLM placeholder titles",             fix_llm_placeholder_titles),
    ("Reclassify entry types",                    fix_entry_types),
    ("Flag citekey suffix collisions",            flag_citekey_suffix_collisions),
    ("Flag compound citations",                   flag_compound_citations),
    ("Flag cross-script duplicates",              flag_cross_script_duplicates),
    ("Flag citing paper not in corpus",           flag_citing_paper_not_in_corpus),
    ("Flag title contains publisher/location",    flag_title_contains_publisher),
    ("Flag near-duplicate entries",               flag_near_duplicates),
    ("Flag missing given names",                  flag_missing_given_names),
    ("Flag editor/author confusion",              flag_editor_author_confusion),
    ("Flag unprocessed source PDFs",              lambda bib: flag_unprocessed_source_pdfs(bib, None)),
]


def run_postprocess(
    input_path: Path,
    output_path: Path | None = None,
    processed_pdfs: set | None = None,
) -> dict:
    """
    Run all post-processing passes on bibliography.json.
    Returns dict of {pass_name: count_modified}.
    """
    input_path  = Path(input_path)
    output_path = Path(output_path) if output_path else input_path

    logger.info("Loading %s ...", input_path)
    bib = json.loads(input_path.read_text(encoding="utf-8"))
    total = len(bib)
    logger.info("Loaded %d entries.", total)

    results = {}
    for name, fn in PASSES:
        if fn is PASSES[-1][1]:  # unprocessed source PDFs needs extra arg
            count = flag_unprocessed_source_pdfs(bib, processed_pdfs)
        else:
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