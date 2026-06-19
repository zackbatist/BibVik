"""
postprocess.py -- Post-enrichment LLM cleaning passes for bibliography.json.

Runs after --enrich. Applies passes that require the full enriched bibliography
and benefit from LLM inference. Per-entry normalization (title cleanup, date/DOI/
pages, entry type for misc) happens inline in normalize.py during graph construction.

Passes:
    1. Entry type reclassification (all types, using enriched fields)
    2. LLM title recovery from raw citations (entries with _raw_citation but no title)
    2b. Footnote stub resolution (OCR merges, abbreviation expansion, CrossRef author+year)
    3. Near-duplicate resolution (LLM, title-rich entries only)

Usage:
    python3 run.py --postprocess
"""

import json
import logging
import re
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

# Titles too generic/short to be useful for near-duplicate matching
_TRIVIAL_TITLES = frozenset({
    "introduction", "conclusion", "conclusions", "preface", "foreword",
    "abstract", "summary", "notes", "bibliography", "references",
    "index", "appendix", "appendices", "chapter", "part", "section",
    "review", "overview", "discussion", "results", "methods", "methodology",
    "afterword", "epilogue", "prologue", "acknowledgements", "acknowledgments",
})


# ── Pass 1: Entry type reclassification (post-enrich) ────────────────────────

def fix_entry_types_post_enrich(bib: dict) -> int:
    """
    Reclassify entry types using enriched fields. Only reclassifies entries
    where the evidence is unambiguous. Does not touch incollection->inbook.
    """
    count = 0
    for entry in bib.values():
        old_type  = entry.get("entry_type", "")
        journal   = entry.get("journaltitle", "").strip()
        booktitle = entry.get("booktitle", "").strip()
        editors   = entry.get("editor", [])
        volume    = entry.get("volume", "").strip()
        number    = entry.get("number", "").strip()
        pages     = entry.get("pages", "").strip()
        title     = entry.get("title", "").strip()

        # Pages must be a range for article classification --
        # single numbers are often monograph series volume numbers
        pages_is_range = bool(re.search(r"\d+\s*[--]+\s*\d+", pages))

        if journal and (volume or number or pages_is_range):
            new_type = "article"
        elif booktitle and editors:
            new_type = "incollection"
        else:
            continue

        # Don't reclassify book->inbook/incollection when booktitle matches title
        if old_type == "book" and new_type in ("inbook", "incollection") and title and booktitle:
            t_norm  = re.sub(r"\W", "", title.lower())
            bt_norm = re.sub(r"\W", "", booktitle.lower())
            if t_norm and bt_norm and (t_norm == bt_norm or t_norm in bt_norm or bt_norm in t_norm):
                continue

        if new_type != old_type:
            entry["entry_type"] = new_type
            entry.setdefault("_entry_type_original", old_type)
            count += 1
    return count


# ── Pass 2: LLM title recovery from raw citations ────────────────────────────

_LLM_AUTHOR_RECOVERY = """You are an expert bibliographer. The following is a raw citation string extracted from a bibliography.
Extract only the author(s) of the cited work. Return them as a JSON array of objects with "family" and "given" keys.
If multiple authors, include all of them. If no author can be identified, return an empty array [].
Do not include editors. Do not include institutions unless no personal author is present.

Raw citation:
{raw}

Respond with ONLY the JSON array, nothing else. /no_think"""


def recover_authors_from_raw(bib: dict, llm_config: dict | None = None) -> int:
    """
    For NOAUTHOR entries that have a _raw_citation but no author field,
    attempt to extract the author from the raw citation string using the LLM.

    These entries arise when GROBID failed to parse the author from a reference
    string despite the author being clearly present in the raw text. Examples:
    - All-caps author names (LUZ, B. and KOLODNY, Y.)
    - Author names run together with title (Ryan 1987a. Michael Ryan, Some...)
    - Institutional authors parsed incorrectly

    Sets _author_recovered: True on entries where an author was extracted.
    Returns the number of entries where an author was successfully recovered.
    """
    if not llm_config:
        return 0

    import json as _json
    import requests

    candidates = [
        (ck, entry) for ck, entry in bib.items()
        if ck.startswith('NOAUTHOR')
        and not entry.get('author')
        and entry.get('_raw_citation', '').strip()
        and entry.get('title')
        and not entry.get('_merged_into')
    ]

    if not candidates:
        return 0

    logger.info("Author recovery: %d NOAUTHOR entries with raw citation.", len(candidates))

    base_url = llm_config.get("base_url", "http://localhost:11434")
    model    = llm_config.get("model", "qwen2.5:7b")
    timeout  = llm_config.get("timeout", 30)
    backend  = llm_config.get("backend", "ollama")
    count    = 0

    for ck, entry in candidates:
        raw = entry["_raw_citation"].strip()
        prompt = _LLM_AUTHOR_RECOVERY.format(raw=raw)

        try:
            if backend == "ollama":
                resp = requests.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "think": False,
                        "options": {"temperature": 0.0, "num_predict": 200},
                    },
                    timeout=timeout,
                )
                text = resp.json().get("response", "").strip()
            else:
                resp = requests.post(
                    f"{base_url}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "temperature": 0.0,
                        "max_tokens": 200,
                    },
                    timeout=timeout,
                )
                text = resp.json()["choices"][0]["message"]["content"].strip()

            text = text.strip().lstrip("```json").rstrip("```").strip()
            authors = _json.loads(text)

            if isinstance(authors, list) and authors:
                valid = [a for a in authors if isinstance(a, dict) and a.get("family")]
                if valid:
                    entry["author"] = valid
                    entry["_author_recovered"] = True
                    count += 1
                    logger.debug("Recovered author for [%s]: %s", ck, valid[0].get("family", ""))

        except Exception as exc:
            logger.debug("Author recovery failed for [%s]: %s", ck, exc)
            continue

    return count


_LLM_TITLE_RECOVERY = """You are an expert bibliographer. The following is a raw citation string extracted from a bibliography.
Extract only the title of the cited work. Do not include author names, year, publisher, place of publication, volume, pages, or any other metadata.
If the string does not contain a recognisable title, respond with an empty string.

Raw citation:
{raw}

Respond with ONLY the title, nothing else. /no_think"""


def recover_titles_from_raw(bib: dict, llm_config: dict | None = None) -> int:
    """
    For entries that have a _raw_citation but no title field, attempt to extract
    the title from the raw citation string using the LLM.

    These entries arise when GROBID parsed author and year from a reference but
    failed to extract the title into the structured title field. The title is
    present in the raw citation text and can be recovered.

    Only entries with a non-empty _raw_citation and empty title are processed.
    Entries that lack a _raw_citation entirely (e.g. LLM body scan detections
    that returned author+year only) are skipped -- there is no text to recover
    from.

    The pass sets _title_recovered: True on entries where a title was extracted,
    for provenance tracking.

    Returns the number of entries where a title was successfully recovered.
    """
    if not llm_config:
        logger.debug("LLM not configured -- skipping title recovery pass.")
        return 0

    import requests

    candidates = [
        (ck, entry) for ck, entry in bib.items()
        if not entry.get("title")
        and entry.get("_raw_citation", "").strip()
        and entry.get("author")
        and entry.get("year")
    ]

    logger.info("Title recovery: %d candidates with raw citation but no title.", len(candidates))
    if not candidates:
        return 0

    base_url = llm_config.get("base_url", "http://localhost:11434")
    model    = llm_config.get("model", "qwen2.5:7b")
    timeout  = llm_config.get("timeout", 30)
    backend  = llm_config.get("backend", "ollama")
    count    = 0

    for ck, entry in candidates:
        raw = entry["_raw_citation"].strip()
        prompt = _LLM_TITLE_RECOVERY.format(raw=raw)

        try:
            if backend == "ollama":
                resp = requests.post(
                    f"{base_url}/api/generate",
                    json={
                        "model": model,
                        "prompt": prompt,
                        "stream": False,
                        "think": False,
                        "options": {"temperature": 0.0, "num_predict": 100},
                    },
                    timeout=timeout,
                )
                title = resp.json().get("response", "").strip()
            else:
                resp = requests.post(
                    f"{base_url}/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "temperature": 0.0,
                        "max_tokens": 100,
                    },
                    timeout=timeout,
                )
                title = resp.json()["choices"][0]["message"]["content"].strip()

            # Strip surrounding quotes if the LLM added them
            title = title.strip('"\'').strip()

            if title and len(title) > 3:
                entry["title"] = title
                entry["_title_recovered"] = True
                count += 1
                logger.debug("Recovered title for [%s]: %s", ck, repr(title[:60]))

        except Exception as exc:
            logger.debug("Title recovery failed for [%s]: %s", ck, exc)
            continue

    return count


# ── Pass 2b: Footnote stub resolution ────────────────────────────────────────

# Known series/journal abbreviations used in footnote citations.
# Maps normalised abbreviation (lowercase, no punctuation) to full title.
# These are unambiguous in the context of Viking Age and medieval archaeology.
_ABBREVIATION_TABLE = {
    "aud":    "Arkæologiske Udgravninger i Danmark",
    "kag":    "Kuml: Årbog for Jysk Arkæologisk Selskab",
    "fmst":   "Frühmittelalterliche Studien",
    "acta":   "Acta Archaeologica",
    "ms":     "Medieval Scandinavia",
}

# Confirmed OCR/normalisation merge pairs: (source_citekey, target_citekey).
# Each pair was verified manually -- the source is an OCR or normalisation
# corruption of the target, same author, same year, same work.
# The source entry's cited_by is merged into the target and source is marked
# _merged_into. Only entries confirmed as safe are listed here.
_OCR_MERGE_PAIRS = [
    ("brucemicford2005",    "brucemitford2005"),   # Micford -> Mitford (OCR t/c)
    ("wamets1985",          "wamers1985"),           # Wamets -> Wamers (OCR t/r)
    ("rsnes1966",           "orsnes1966"),           # missing initial O
    ("ocarragain2010",      "carragain2010"),        # Ó prefix stripped
    ("ofloinn2013",         "floinn2013"),           # Ó prefix stripped
    ("ofloinn2015",         "floinn2015"),           # Ó prefix stripped
    ("kalming2010",         "kalmring2010a"),        # Kalming -> Kalmring (OCR)
    ("sampson1991",         "samson1991"),           # double p
    ("gurevic1968a",        "gurevich1968"),         # transliteration variant
    ("tenharkel2013",       "harkel2013"),           # Ten prefix stripped
    ("stolpenda",           "stolpendd"),            # garbled Unicode Björkö (Bj€ork€o)
    ("NOAUTHOR771",         "pentz2009a"),           # author string absorbed into title field
]


def resolve_footnote_stubs(bib: dict, email: str = "") -> int:
    """
    Resolve stub entries that have author and year but no title.

    Targets two entry types:
    - Method 5 (llm_from_footnote): footnote shorthand citations where the LLM
      extracted author+year but had no title to extract
    - Method 6 (llm_bib_reparse): entries where the LLM re-parsed the reference
      list but returned no title for a given entry

    Three mechanisms are applied in order:

    1. Abbreviation expansion: entries whose author field matches a known series
       or journal abbreviation (e.g. AUD -> Arkæologiske Udgravninger i Danmark)
       have their title set from the abbreviation table.

    2. OCR/normalisation merges: entries that are confirmed OCR or normalisation
       corruptions of an existing titled entry are merged into that entry. The
       cited_by list is combined and the source is marked _merged_into.

    3. CrossRef author+year query: for remaining stub entries, queries CrossRef
       by author name and year. Accepts only if the returned record's year
       matches exactly and the author name similarity is >= 0.7. This is weaker
       than a title query but reasonable for entries with no title at all.

    Returns the total number of entries resolved (title set or merged).
    """
    import re as _re
    import unicodedata as _ud
    import difflib
    import requests

    def _norm(s):
        if not s: return ''
        s = _ud.normalize('NFD', s)
        s = ''.join(c for c in s if _ud.category(c) != 'Mn')
        return _re.sub(r'[^a-z0-9]', '', s.lower())

    count = 0

    # Identify footnote stub entries and Method 6 no-title entries
    stubs = {
        ck: e for ck, e in bib.items()
        if e.get('_resolution_method') in ('llm_from_footnote', 'llm_bib_reparse')
        and not e.get('title')
        and e.get('author')
        and e.get('year')
    }

    if not stubs:
        return 0

    logger.info("Footnote stub resolution: %d stubs to resolve.", len(stubs))

    # ── Mechanism 1: Abbreviation expansion ──────────────────────────────────
    for ck, entry in list(stubs.items()):
        family = (entry.get('author') or [{}])[0].get('family', '')
        family_norm = _norm(family)
        if family_norm in _ABBREVIATION_TABLE:
            entry['title'] = _ABBREVIATION_TABLE[family_norm]
            entry['_title_from_abbreviation'] = True
            logger.debug("Abbreviation resolved [%s]: %s -> %s", ck, family, entry['title'])
            del stubs[ck]
            count += 1

    # ── Mechanism 2: OCR/normalisation merges ────────────────────────────────
    for src_ck, tgt_ck in _OCR_MERGE_PAIRS:
        if src_ck not in bib or tgt_ck not in bib:
            continue
        src = bib[src_ck]
        tgt = bib[tgt_ck]
        # Merge cited_by
        for cb in src.get('cited_by', []):
            if cb not in tgt.get('cited_by', []):
                tgt.setdefault('cited_by', []).append(cb)
        src['_merged_into'] = tgt_ck
        if src_ck in stubs:
            del stubs[src_ck]
        logger.debug("OCR merge: [%s] -> [%s] (%s)", src_ck, tgt_ck, tgt.get('title', '')[:50])
        count += 1

    # ── Mechanism 3: CrossRef author+year query ───────────────────────────────
    CROSSREF_BASE = "https://api.crossref.org/works"
    AUTHOR_SIM_THRESHOLD = 0.70

    for ck, entry in list(stubs.items()):
        family = (entry.get('author') or [{}])[0].get('family', '')
        year = str(entry.get('year', ''))
        if not family or not year:
            continue

        params = {
            'query.author': family,
            'filter': f'from-pub-date:{year},until-pub-date:{year}',
            'rows': 3,
        }
        if email:
            params['mailto'] = email

        try:
            resp = requests.get(CROSSREF_BASE, params=params, timeout=15)
            if resp.status_code != 200:
                continue
            items = resp.json().get('message', {}).get('items', [])
            for item in items:
                # Verify year
                issued = item.get('issued', {}).get('date-parts', [[None]])
                item_year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ''
                if item_year != year:
                    continue
                # Verify author similarity
                cr_authors = item.get('author', [])
                if not cr_authors:
                    continue
                cr_family = cr_authors[0].get('family', '')
                sim = difflib.SequenceMatcher(None, _norm(family), _norm(cr_family)).ratio()
                if sim < AUTHOR_SIM_THRESHOLD:
                    continue
                # Accept
                cr_title = ' '.join(item.get('title', []))
                if cr_title:
                    entry['title'] = cr_title
                    entry['_title_from_crossref_author_year'] = True
                    if item.get('DOI'):
                        entry['doi'] = item['DOI']
                    logger.debug(
                        "CrossRef author+year resolved [%s]: %s %s -> %s",
                        ck, family, year, cr_title[:60]
                    )
                    count += 1
                    break
        except Exception as exc:
            logger.debug("CrossRef author+year failed for [%s]: %s", ck, exc)
            continue

    # ── Mechanism 4: CrossRef title lookup for NOAUTHOR entries ──────────────
    # NOAUTHOR entries that have a title but no author and no raw citation
    # can be looked up in CrossRef by title. This recovers author, DOI,
    # and other metadata for entries like edited volumes and software packages
    # where the author was not extracted by GROBID.
    TITLE_SIM_THRESHOLD = 0.85

    noauthor_title = {
        ck: e for ck, e in bib.items()
        if ck.startswith('NOAUTHOR')
        and not e.get('author')
        and e.get('title')
        and not e.get('_raw_citation')
        and not e.get('_merged_into')
    }

    for ck, entry in noauthor_title.items():
        title = entry.get('title', '')
        year = entry.get('year', '')
        params = {
            'query.title': title,
            'rows': 3,
        }
        if year:
            params['filter'] = f'from-pub-date:{year},until-pub-date:{year}'
        if email:
            params['mailto'] = email

        try:
            resp = requests.get(CROSSREF_BASE, params=params, timeout=15)
            if resp.status_code != 200:
                continue
            items = resp.json().get('message', {}).get('items', [])
            for item in items:
                cr_title = ' '.join(item.get('title', []))
                if not cr_title:
                    continue
                sim = difflib.SequenceMatcher(None, _norm(title), _norm(cr_title)).ratio()
                if sim < TITLE_SIM_THRESHOLD:
                    continue
                # Accept -- populate author/editor from CrossRef
                cr_authors = item.get('author', [])
                cr_editors = item.get('editor', [])
                if cr_authors:
                    entry['author'] = [{'family': a.get('family', ''), 'given': a.get('given', '')}
                                       for a in cr_authors if a.get('family')]
                    entry['_author_from_crossref_title'] = True
                elif cr_editors:
                    entry['editor'] = [{'family': a.get('family', ''), 'given': a.get('given', '')}
                                       for a in cr_editors if a.get('family')]
                    entry['_author_from_crossref_title'] = True
                if item.get('DOI'):
                    entry['doi'] = item['DOI']
                # Update title to CrossRef version if better
                if cr_title and len(cr_title) > len(title):
                    entry['title'] = cr_title
                logger.debug(
                    "CrossRef title lookup resolved [%s]: %s -> %s",
                    ck, title[:50], cr_title[:50]
                )
                count += 1
                break
        except Exception as exc:
            logger.debug("CrossRef title lookup failed for [%s]: %s", ck, exc)
            continue

    logger.info("Footnote stub resolution: %d entries resolved.", count)
    return count


# ── Pass 3: Near-duplicate flagging and LLM resolution ───────────────────────

def _title_tokens(title: str) -> set:
    from unidecode import unidecode
    # Normalise through unidecode before tokenising to handle OCR Unicode variants
    # e.g. "Kongsga˚rd" -> "Kongsgard", "InsularerMetallschmuck" -> tokens
    normalised = unidecode(title).lower()
    return set(re.findall(r"\b\w{4,}\b", normalised))

def _is_trivial_title(title: str) -> bool:
    return title.strip().lower() in _TRIVIAL_TITLES or len(title.strip()) < 20

def _first_author_key(entry: dict) -> str:
    from unidecode import unidecode
    authors = entry.get("author", [])
    if not authors:
        return ""
    family = unidecode(authors[0].get("family", "")).lower().strip()
    # Use the last word of the family name to handle compound surnames:
    # "Hallans Stenholm" -> "stenholm", "de Vries" -> "vries"
    # This ensures compound-surname entries pair with single-surname variants
    # of the same person.
    parts = re.sub(r"[^a-z ]", "", family).split()
    return parts[-1] if parts else family


def flag_near_duplicates(bib: dict, llm_config: dict | None = None) -> int:
    """
    Flag and optionally LLM-resolve near-duplicate entries.
    Only operates on title-rich pairs (both have substantive titles).
    Trivial titles are skipped.
    """
    count = 0
    index: dict[tuple, list] = defaultdict(list)

    for ck, entry in bib.items():
        # Skip entries already merged by a previous pass
        if entry.get("_merged_into"):
            continue
        year = entry.get("date", entry.get("year", ""))[:4]
        ak   = _first_author_key(entry)
        if year and ak:
            index[(year, ak)].append(ck)

    for citekeys in index.values():
        if len(citekeys) < 2:
            continue
        pairs = [
            (citekeys[i], citekeys[j])
            for i in range(len(citekeys))
            for j in range(i + 1, len(citekeys))
        ]
        for ck_a, ck_b in pairs:
            ta = bib[ck_a].get("title", "")
            tb = bib[ck_b].get("title", "")

            # Skip trivial or missing titles
            if not ta or not tb or _is_trivial_title(ta) or _is_trivial_title(tb):
                continue

            # Send all same-author same-year title pairs directly to the LLM.
            # Token overlap was previously used as a gate but failed on:
            # - Titles in different languages (Danish vs Norwegian phrasing)
            # - OCR variants where key tokens differ due to character corruption
            # - Compound surnames where the index key differs from the entry
            # The LLM is the appropriate judge for all of these cases.
            if llm_config:
                same = _llm_same_work(ta, tb, bib[ck_a], bib[ck_b], llm_config)
                if same is True:
                    for cb in bib[ck_b].get("cited_by", []):
                        if cb not in bib[ck_a].get("cited_by", []):
                            bib[ck_a].setdefault("cited_by", []).append(cb)
                    bib[ck_b]["_merged_into"] = ck_a
                    count += 1
                    continue
                elif same is False:
                    continue
            else:
                # No LLM -- use token overlap as fallback gate
                ta_tokens = _title_tokens(ta)
                tb_tokens = _title_tokens(tb)
                if not ta_tokens or not tb_tokens:
                    continue
                overlap = len(ta_tokens & tb_tokens) / min(len(ta_tokens), len(tb_tokens))
                if overlap < 0.7:
                    continue

            # LLM unavailable or inconclusive -- flag for human review
                bib[ck_a].setdefault("_near_duplicate_candidate", [])
                if ck_b not in bib[ck_a]["_near_duplicate_candidate"]:
                    bib[ck_a]["_near_duplicate_candidate"].append(ck_b)
                    count += 1
                bib[ck_b].setdefault("_near_duplicate_candidate", [])
                if ck_a not in bib[ck_b]["_near_duplicate_candidate"]:
                    bib[ck_b]["_near_duplicate_candidate"].append(ck_a)

    return count


def _llm_same_work(
    title_a: str, title_b: str,
    entry_a: dict, entry_b: dict,
    llm_config: dict,
) -> bool | None:
    """Ask LLM if two entries are the same work. Returns True/False/None."""
    import requests

    prompt = (
        "You are an expert bibliographer. Are the following two bibliography entries "
        "the same published work? Consider title, author, and year. "
        "Respond with only 'yes' or 'no'.\n\n"
        f"Entry A:\n  Title: {title_a}\n  Author: {entry_a.get('author', [{}])[0].get('family', '')}\n  Year: {entry_a.get('date', '')}\n\n"
        f"Entry B:\n  Title: {title_b}\n  Author: {entry_b.get('author', [{}])[0].get('family', '')}\n  Year: {entry_b.get('date', '')}\n\n"
        "/no_think"
    )

    try:
        base_url = llm_config.get("base_url", "http://localhost:11434")
        model    = llm_config.get("model", "qwen2.5:7b")
        timeout  = llm_config.get("timeout", 30)
        backend  = llm_config.get("backend", "ollama")

        if backend == "ollama":
            resp = requests.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "think": False, "options": {"temperature": 0.0, "num_predict": 10}},
                timeout=timeout,
            )
            raw = resp.json().get("response", "").strip().lower()
        else:
            resp = requests.post(
                f"{base_url}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "temperature": 0.0, "max_tokens": 10},
                timeout=timeout,
            )
            raw = resp.json()["choices"][0]["message"]["content"].strip().lower()

        if raw.startswith("yes"):
            return True
        if raw.startswith("no"):
            return False
    except Exception:
        pass
    return None


# ── Main ─────────────────────────────────────────────────────────────────────

PASSES = [
    ("Entry type reclassification (enriched)", fix_entry_types_post_enrich, False),
    ("Title recovery from raw citations (LLM)", recover_titles_from_raw, True),
    ("Author recovery for NOAUTHOR entries (LLM)", recover_authors_from_raw, True),
    ("Footnote stub resolution", resolve_footnote_stubs, False),
    ("Near-duplicate flagging / LLM resolution", flag_near_duplicates, True),
]


def run_postprocess(
    input_path: Path,
    output_path: Path | None = None,
    llm_config: dict | None = None,
    email: str = "",
) -> dict:
    """Run all post-enrichment passes on bibliography.json."""
    input_path  = Path(input_path)
    output_path = Path(output_path) if output_path else input_path

    bib = json.loads(input_path.read_text(encoding="utf-8"))
    total = len(bib)
    logger.info("Loaded %d entries from %s.", total, input_path)

    results = {}
    for name, fn, needs_llm in PASSES:
        if fn is resolve_footnote_stubs:
            count = fn(bib, email=email)
        elif needs_llm:
            count = fn(bib, llm_config)
        else:
            count = fn(bib)
        logger.info("%-50s  %4d / %d", name, count, total)
        results[name] = count

    output_path.write_text(
        json.dumps(bib, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("Written to %s", output_path)
    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",   default="bibliography.json")
    parser.add_argument("--output",  default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run_postprocess(Path(args.input), Path(args.output) if args.output else None)