"""
bibvik.detector — Unified multi-method citation detection.

For each paper, we throw every available method at the text and merge the
results. The goal is the most complete record of every work cited anywhere
in the document — bibliography section, body text, footnotes, or prose.

Detection methods (all applied to every paper):

    1. GROBID bibliography
       Structured entries from the PDF's reference list. GROBID uses ML
       models to parse these. Our TEI parser post-processes the output
       to split compound references (dash-abbreviated authors, merged
       entries). This is the richest source of metadata when it works,
       but fails on non-standard layouts, footnote-only papers, and
       humanities citation conventions.

    2. GROBID inline markers
       GROBID annotates <ref type="bibr"> elements in the body text,
       linking inline citations to bibliography entries. We extract
       (author, year) pairs from marker text independently of whether
       the bibliography entry was successfully parsed.

    3. Regex patterns
       Pattern matching for parenthetical and narrative citation forms
       in the raw body text, independent of GROBID. Catches standard
       author-year styles that GROBID missed. Handles non-English
       connectors (og, und, och, et). Cannot detect discursive or
       informal references.

    4. LLM body scan
       The local LLM reads each paragraph and identifies all referenced
       works, including discursive references ("as Smith argued in her
       2020 monograph"), organizational authors, and anything regex
       cannot capture. Slowest method but most flexible.

    5. LLM footnote extraction
       The local LLM reads each footnote and extracts full structured
       bibliographic metadata — not just (author, year) but title,
       journal, volume, pages, etc. Essential for papers that embed
       their bibliography in footnotes (e.g., Chicago footnote style).

After detection, results from all methods are merged and deduplicated
into a unified set of citations. Each citation is tagged with which
methods found it (provenance tracking).

The output is a list of CitationRecord dicts, each representing one
unique cited work with all available metadata and provenance.
"""

import json
import logging
import re
from typing import Any

import requests
from unidecode import unidecode

from .utils import extract_year, norm_author
from .tei_parser import (
    parse_tei_references,
    parse_tei_body,
    parse_tei_header,
    parse_tei_footnotes,
    get_body_text,
    parse_tei_xml,
    TEI_NAMESPACE,
)

logger = logging.getLogger(__name__)


# =============================================================================
# Regex patterns (Method 3)
# =============================================================================

_NAME = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:[-'][A-ZÀ-ÖØ-Þa-zà-öø-ÿ]+)*"
_PARTICLE = r"(?:(?:de|van|von|di|el|al-|La|Le|Mc|Mac|O')\s+)?"
_FULL_NAME = _PARTICLE + _NAME
_AND = r"(?:and|&|og|und|och|et|ja)"
_YEAR = r"(?:19|20)\d{2}[a-c]?"

_SINGLE_CITE = (
    r"(?:" + _FULL_NAME + r")"
    r"(?:\s+" + _AND + r"\s+" + _FULL_NAME + r")?"
    r"(?:\s+et\s+al\.?)?"
    r"\s+"
    r"(?:" + _YEAR + r")"
    r"(?:\s*[,:]\s*(?:(?:pp?\.\s*)?\d+(?:\s*[-\u2013\u2014]\s*\d+)?|fig\.\s*\d+|table\s*\d+))?"
)

_PAREN_PATTERN = r"\(" + _SINGLE_CITE + r"(?:\s*;\s*" + _SINGLE_CITE + r")*" + r"\)"
_NARRATIVE_PATTERN = (
    r"(?:" + _FULL_NAME + r")"
    r"(?:\s+" + _AND + r"\s+" + _FULL_NAME + r")?"
    r"(?:\s+et\s+al\.?)?"
    r"\s*\(\s*" + _YEAR
    + r"(?:\s*[,:]\s*(?:(?:pp?\.\s*)?\d+(?:\s*[-\u2013\u2014]\s*\d+)?))?"
    + r"\s*\)"
)
_AY_PATTERN = (
    r"(" + _FULL_NAME + r")"
    r"(?:\s+" + _AND + r"\s+" + _FULL_NAME + r")?"
    r"(?:\s+et\s+al\.?)?"
    r"\s+(" + _YEAR + r")"
)
_NAY_PATTERN = (
    r"(" + _FULL_NAME + r")"
    r"(?:\s+" + _AND + r"\s+" + _FULL_NAME + r")?"
    r"(?:\s+et\s+al\.?)?"
    r"\s*\(\s*(" + _YEAR + r")"
)

try:
    _PAREN_RE = re.compile(_PAREN_PATTERN, re.UNICODE)
    _NARRATIVE_RE = re.compile(_NARRATIVE_PATTERN, re.UNICODE)
    _AY_RE = re.compile(_AY_PATTERN, re.UNICODE)
    _NAY_RE = re.compile(_NAY_PATTERN, re.UNICODE)
except re.error:
    # Simplified fallbacks for strict regex engines (Python 3.14+)
    _PAREN_RE = re.compile(
        r'\([A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+(?:\s+(?:and|&|og|und|och|et)\s+'
        r'[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)?(?:\s+et\s+al\.?)?\s+'
        r'(?:19|20)\d{2}[a-c]?[^)]*\)', re.UNICODE)
    _NARRATIVE_RE = re.compile(
        r'[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+(?:\s+(?:and|&|og|und|och|et)\s+'
        r'[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)?(?:\s+et\s+al\.?)?\s*\(\s*'
        r'(?:19|20)\d{2}[a-c]?[^)]*\)', re.UNICODE)
    _AY_RE = re.compile(
        r'([A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)\s+(?:(?:and|&|og|und|och|et)\s+'
        r'[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+\s+)?(?:et\s+al\.?\s+)?'
        r'((?:19|20)\d{2}[a-c]?)', re.UNICODE)
    _NAY_RE = re.compile(
        r'([A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)\s*(?:(?:and|&|og|und|och|et)\s+'
        r'[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+\s*)?(?:et\s+al\.?\s*)?\(\s*'
        r'((?:19|20)\d{2}[a-c]?)', re.UNICODE)


# =============================================================================
# LLM prompts (Methods 4 and 5)
# =============================================================================

_LLM_BODY_DETECT = """You are an expert at identifying bibliographic references in academic text. Find ALL works cited or referenced in this passage.

## Passage
---
{text}
---

Include: formal citations (Smith 2020), narrative (Smith (2020) argued...), discursive ("as Smith argued in her 2020 monograph"), organizational authors (UNESCO 2019), non-English styles.

For each work: {{"first_author": "<family name>", "year": "<4 digits>"}}
Respond ONLY with a JSON array. If none: []
/no_think"""


_LLM_FOOTNOTE_EXTRACT = """You are an expert at extracting bibliographic references from academic footnotes. This footnote may contain one or more references to published works embedded in prose.

## Footnote text
---
{text}
---

For EACH distinct published work referenced, extract as much metadata as you can:
- first_author_family: family/surname of the first author
- first_author_given: given name(s) or initials of the first author
- additional_authors: list of {{"family": "...", "given": "..."}} for co-authors (empty list if sole author)
- year: publication year (4 digits)
- title: title of the article, chapter, or book
- container_title: journal name, book title (for chapters), or series name (empty string if standalone book)
- volume: volume number (empty string if n/a)
- pages: page range (empty string if n/a)
- doi: DOI if mentioned (empty string if not)
- entry_type: one of "article", "book", "incollection", "inproceedings", "thesis", "misc"

Respond ONLY with a JSON array. If no references: []
Example: [{{"first_author_family": "Sindbæk", "first_author_given": "Søren M.", "additional_authors": [], "year": "2007", "title": "The Small World of the Vikings", "container_title": "Norwegian Archaeological Review", "volume": "40", "pages": "59-74", "doi": "", "entry_type": "article"}}]
/no_think"""


# =============================================================================
# Main detection function
# =============================================================================

def detect_all_citations(
    tei_xml: str,
    source_pdf: str = "",
    llm_config: dict | None = None,
    grobid_refs: list[dict] | None = None,
    paragraphs: list[dict] | None = None,
) -> dict:
    """
    Apply all detection methods to one paper and return merged results.

    Args:
        tei_xml:      Raw TEI-XML string from GROBID.
        source_pdf:   PDF filename (for logging and provenance).
        llm_config:   LLM config dict. If None, methods 4+5 are skipped.
        grobid_refs:  Pre-parsed GROBID bibliography entries (optional).
        paragraphs:   Pre-parsed body paragraphs (optional).

    Returns:
        Dict with:
        - citations: dict mapping (author, year) → CitationRecord
        - rich_entries: list of dicts with full metadata (from GROBID bib
          and LLM footnote extraction) — these have more than just author+year
        - method_counts: dict mapping method name → number of unique citations
        - source_pdf: the PDF filename
    """
    stem = source_pdf.replace(".pdf", "")[:50]

    # Parse TEI if we haven't already
    if grobid_refs is None:
        grobid_refs = parse_tei_references(tei_xml)
    if paragraphs is None:
        paragraphs = parse_tei_body(tei_xml)

    root = parse_tei_xml(tei_xml)
    raw_text = get_body_text(tei_xml)

    # ── Method 1: GROBID bibliography ──
    m1_citations, m1_rich = _method_grobid_bibliography(grobid_refs)
    logger.debug("%s: reference list extraction: %d entries", stem, len(m1_citations))

    # ── Method 2: GROBID inline markers ──
    m2_citations = _method_grobid_inline(root) if root is not None else {}
    logger.debug("%s: inline citation markers: %d citations", stem, len(m2_citations))

    # ── Method 3: Regex ──
    m3_citations = _method_regex(raw_text) if raw_text else {}
    logger.debug("%s: text pattern matching: %d citations", stem, len(m3_citations))

    # ── Method 4: LLM body scan ──
    m4_citations = {}
    if llm_config and paragraphs:
        m4_citations = _method_llm_body(paragraphs, llm_config)
        logger.debug("%s: LLM body scan: %d citations", stem, len(m4_citations))

    # ── Method 5: LLM footnote extraction ──
    m5_citations = {}
    m5_rich = []
    footnotes = parse_tei_footnotes(tei_xml) if tei_xml else []
    if llm_config and footnotes:
        m5_citations, m5_rich = _method_llm_footnotes(footnotes, llm_config)
        logger.debug("%s: LLM footnote extraction: %d citations, %d rich entries",
                     stem, len(m5_citations), len(m5_rich))

    # ── Merge ──
    merged = _merge_all(
        ("grobid_bib", m1_citations),
        ("grobid_inline", m2_citations),
        ("regex", m3_citations),
        ("llm_body", m4_citations),
        ("llm_footnote", m5_citations),
    )

    rich_entries = m1_rich + m5_rich

    method_counts = {
        "reference_list": len(m1_citations),
        "inline_markers": len(m2_citations),
        "text_patterns": len(m3_citations),
        "llm_body_scan": len(m4_citations),
        "llm_footnotes": len(m5_citations),
        "merged_total": len(merged),
    }

    logger.info(
        "%s: %d unique citations  "
        "(reference list: %d, inline markers: %d, text patterns: %d, "
        "LLM body: %d, LLM footnotes: %d)",
        stem, len(merged),
        len(m1_citations), len(m2_citations), len(m3_citations),
        len(m4_citations), len(m5_citations),
    )

    return {
        "citations": merged,
        "rich_entries": rich_entries,
        "method_counts": method_counts,
        "source_pdf": source_pdf,
    }


# =============================================================================
# Method 1: GROBID bibliography
# =============================================================================

def _method_grobid_bibliography(refs: list[dict]) -> tuple[dict, list[dict]]:
    """Extract (author, year) pairs and rich metadata from GROBID bib entries."""
    citations = {}
    rich = []

    for ref in refs:
        authors = ref.get("author", [])
        family = authors[0].get("family", "") if authors else ""
        year = _extract_year(ref.get("date", ""))

        if family and year:
            key = (_norm(family), year)
            if key not in citations:
                citations[key] = {
                    "author": family,
                    "year": year,
                    "methods": ["grobid_bib"],
                    "occurrences": 1,
                    "contexts": [],
                }

        # Rich entry: full metadata for graph building
        if ref.get("title") or ref.get("_raw_citation"):
            rich.append(ref)

    return citations, rich


# =============================================================================
# Method 2: GROBID inline markers
# =============================================================================

def _method_grobid_inline(root) -> dict:
    """Extract (author, year) pairs from GROBID's <ref type="bibr"> markers."""
    citations = {}
    body = root.find(f".//{{{TEI_NAMESPACE}}}body")
    if body is None:
        return citations

    for ref in body.iter(f"{{{TEI_NAMESPACE}}}ref"):
        if ref.get("type") != "bibr":
            continue
        marker = "".join(ref.itertext()).strip()
        if not marker:
            continue

        for author, year in _extract_author_year(marker):
            key = (_norm(author), year)
            if key not in citations:
                citations[key] = {
                    "author": author,
                    "year": year,
                    "methods": ["grobid_inline"],
                    "occurrences": 0,
                    "contexts": [],
                }
            citations[key]["occurrences"] += 1

    return citations


# =============================================================================
# Method 3: Regex
# =============================================================================

def _method_regex(text: str) -> dict:
    """Detect citations via regex in raw body text."""
    citations = {}

    for match in _PAREN_RE.finditer(text):
        full = match.group(0)
        for ay in _AY_RE.finditer(full):
            author, year = ay.group(1).strip(), ay.group(2).strip()
            key = (_norm(author), year)
            ctx = _context(text, match.start(), match.end())
            if key not in citations:
                citations[key] = {
                    "author": author, "year": year,
                    "methods": ["regex"], "occurrences": 0, "contexts": [],
                }
            citations[key]["occurrences"] += 1
            if len(citations[key]["contexts"]) < 3:
                citations[key]["contexts"].append(ctx)

    for match in _NAY_RE.finditer(text):
        author, year = match.group(1).strip(), match.group(2).strip()
        key = (_norm(author), year)
        ctx = _context(text, match.start(), match.end())
        if key not in citations:
            citations[key] = {
                "author": author, "year": year,
                "methods": ["regex"], "occurrences": 0, "contexts": [],
            }
        citations[key]["occurrences"] += 1
        if len(citations[key]["contexts"]) < 3:
            citations[key]["contexts"].append(ctx)

    return citations


# =============================================================================
# Method 4: LLM body scan
# =============================================================================

# In-memory cache: hash(paragraph_text) → list of (author, year) pairs
_llm_cache: dict[str, list[tuple[str, str]]] = {}


def _method_llm_body(paragraphs: list[dict], llm_config: dict) -> dict:
    """Send paragraphs to the LLM for citation detection, with batching and caching."""
    citations = {}
    base_url = llm_config.get("base_url", "http://localhost:11434")
    det_model = llm_config.get("detection_model", "")
    model = det_model if det_model else llm_config.get("model", "qwen3.5:35b")
    timeout = llm_config.get("timeout", 120)
    batch_size = max(1, llm_config.get("detection_batch_size", 4))
    backend = llm_config.get("backend", "ollama")

    # Clean and filter paragraphs
    substantive = []
    for p in paragraphs:
        text = re.sub(r"\{\{CITE:\w*\}\}", "", p.get("text", "")).strip()
        if len(text) > 50:
            substantive.append(text)

    if not substantive:
        return citations

    # Split into batches
    batches = [substantive[i:i + batch_size] for i in range(0, len(substantive), batch_size)]
    cache_hits = 0
    llm_calls = 0

    logger.debug("    LLM body scan: %d paragraphs in %d batches (model: %s)",
                 len(substantive), len(batches), model)

    for batch_idx, batch in enumerate(batches):
        if len(batches) > 10 and batch_idx % 10 == 0:
            logger.debug("    LLM body scan: batch %d/%d (%d cache hits so far)",
                         batch_idx + 1, len(batches), cache_hits)

        for text in batch:
            text_hash = _hash_text(text)

            # Check cache
            if text_hash in _llm_cache:
                cache_hits += 1
                for author, year in _llm_cache[text_hash]:
                    key = (_norm(author), year)
                    if key not in citations:
                        citations[key] = {
                            "author": author, "year": year,
                            "methods": ["llm_body"], "occurrences": 0, "contexts": [],
                        }
                    citations[key]["occurrences"] += 1
                    if len(citations[key]["contexts"]) < 3:
                        citations[key]["contexts"].append(text[:200])
                continue

            # Send to LLM
            llm_calls += 1
            prompt = _LLM_BODY_DETECT.format(text=text)
            parsed = _llm_query_array(base_url, model, timeout, prompt, backend=backend)

            pairs = []
            if parsed:
                for item in parsed:
                    if not isinstance(item, dict):
                        logger.debug("Skipping non-dict LLM response item: %r", item)
                        continue
                    author = str(item.get("first_author", "")).strip()
                    year = str(item.get("year", "")).strip()
                    if not author or not re.match(r"^(19|20)\d{2}[a-c]?$", year):
                        continue
                    pairs.append((author, year))
                    key = (_norm(author), year)
                    if key not in citations:
                        citations[key] = {
                            "author": author, "year": year,
                            "methods": ["llm_body"], "occurrences": 0, "contexts": [],
                        }
                    citations[key]["occurrences"] += 1
                    if len(citations[key]["contexts"]) < 3:
                        citations[key]["contexts"].append(text[:200])

            # Only cache non-empty results — don't poison cache with failures
            if pairs:
                _llm_cache[text_hash] = pairs

    if cache_hits:
        logger.debug("    LLM body scan: %d LLM calls, %d cache hits", llm_calls, cache_hits)

    return citations


def _hash_text(text: str) -> str:
    """Hash paragraph text for caching."""
    import hashlib
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# =============================================================================
# Method 5: LLM footnote extraction
# =============================================================================

def _method_llm_footnotes(
    footnotes: list[dict],
    llm_config: dict,
) -> tuple[dict, list[dict]]:
    """Extract full bibliographic metadata from footnotes via LLM."""
    citations = {}
    rich_entries = []
    base_url = llm_config.get("base_url", "http://localhost:11434")
    model = llm_config.get("model", "qwen3.5:35b")
    timeout = llm_config.get("timeout", 120)
    backend = llm_config.get("backend", "ollama")

    for fn in footnotes:
        text = fn.get("text", "")
        if len(text) < 40:
            continue
        if not re.search(r"\b(19|20)\d{2}\b", text):
            continue

        parsed = _llm_query_array(
            base_url, model, timeout,
            _LLM_FOOTNOTE_EXTRACT.format(text=text),
            backend=backend,
        )
        if not parsed:
            continue

        for item in parsed:
            family = str(item.get("first_author_family", "")).strip()
            given = str(item.get("first_author_given", "")).strip()
            year = str(item.get("year", "")).strip()
            title = str(item.get("title", "")).strip()

            if not family or not year:
                continue

            key = (_norm(family), year[:4])
            if key not in citations:
                citations[key] = {
                    "author": family, "year": year,
                    "methods": ["llm_footnote"], "occurrences": 0, "contexts": [],
                }
            citations[key]["occurrences"] += 1
            if len(citations[key]["contexts"]) < 3:
                citations[key]["contexts"].append(text[:200])

            # Build a rich entry with full metadata
            authors = [{"family": family, "given": given}]
            for add_auth in item.get("additional_authors", []):
                if isinstance(add_auth, dict) and add_auth.get("family"):
                    authors.append({
                        "family": add_auth["family"],
                        "given": add_auth.get("given", ""),
                    })

            rich_entry = {
                "author": authors,
                "date": year,
                "title": title,
                "entry_type": item.get("entry_type", "misc"),
                "_resolution_method": "llm_from_footnote",
                "_source_footnote": text[:300],
            }
            container = item.get("container_title", "")
            if container:
                if rich_entry["entry_type"] == "article":
                    rich_entry["journaltitle"] = container
                else:
                    rich_entry["booktitle"] = container
            vol = item.get("volume", "")
            if vol:
                rich_entry["volume"] = vol
            pages = item.get("pages", "")
            if pages:
                rich_entry["pages"] = pages
            doi = item.get("doi", "")
            if doi:
                rich_entry["doi"] = doi

            rich_entries.append(rich_entry)

    return citations, rich_entries


# =============================================================================
# Merge
# =============================================================================

def _merge_all(*method_results: tuple[str, dict]) -> dict:
    """Merge citation dicts from all methods into one deduplicated dict."""
    merged = {}

    for method_name, cites in method_results:
        for key, info in cites.items():
            # Normalize key
            norm_key = (_norm(key[0]), key[1][:4])

            if norm_key not in merged:
                merged[norm_key] = {
                    "author": info.get("author", key[0]),
                    "year": key[1][:4],
                    "methods": [],
                    "occurrences": 0,
                    "contexts": [],
                }

            if method_name not in merged[norm_key]["methods"]:
                merged[norm_key]["methods"].append(method_name)

            merged[norm_key]["occurrences"] += info.get("occurrences", 1)

            for ctx in info.get("contexts", []):
                if len(merged[norm_key]["contexts"]) < 5 and ctx not in merged[norm_key]["contexts"]:
                    merged[norm_key]["contexts"].append(ctx)

    return merged


# =============================================================================
# Helpers
# =============================================================================

def _norm(author: str) -> str:
    """Normalise author surname — delegates to utils.norm_author."""
    return norm_author(author)


def _extract_year(date_str: str) -> str:
    """Extract 4-digit year — delegates to utils.extract_year."""
    return extract_year(date_str)


def _extract_author_year(text: str) -> list[tuple[str, str]]:
    """Extract (author, year) pairs from citation marker text."""
    pairs = []
    for m in _AY_RE.finditer(text):
        pairs.append((m.group(1).strip(), m.group(2).strip()))
    if not pairs:
        for m in _NAY_RE.finditer(text):
            pairs.append((m.group(1).strip(), m.group(2).strip()))
    return pairs


def _context(text: str, start: int, end: int, window: int = 100) -> str:
    """Extract surrounding context for a regex match."""
    s = max(0, start - window)
    e = min(len(text), end + window)
    ctx = text[s:e].strip()
    if s > 0:
        ctx = "..." + ctx
    if e < len(text):
        ctx += "..."
    return ctx


def _llm_query_array(
    base_url: str, model: str, timeout: int, prompt: str,
    backend: str = "ollama",
) -> list | None:
    """Send a prompt to the LLM and parse a JSON array response."""
    try:
        if backend == "llama_server":
            resp = requests.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.2,
                    "max_tokens": 1024,
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                return None
            choices = resp.json().get("choices", [])
            raw = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        else:
            resp = requests.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "options": {"temperature": 0.2, "num_predict": 1024},
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                return None
            raw = resp.json().get("response", "").strip()

        raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
        raw = re.sub(r"<think>[\s\S]*$", "", raw).strip()

        # Try direct parse
        try:
            result = json.loads(raw)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        # Strip markdown fences
        if raw.startswith("```"):
            lines = raw.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            try:
                result = json.loads("\n".join(lines))
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        # Find JSON array
        m = re.search(r"\[[\s\S]*\]", raw)
        if m:
            try:
                result = json.loads(m.group(0))
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        return None

    except (requests.Timeout, requests.ConnectionError):
        return None
    except Exception as e:
        logger.debug("LLM query error: %s", e)
        return None
