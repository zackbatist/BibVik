"""
bibvik.reference_audit — Detect and reconcile in-text citations against the
extracted bibliography.

The problem:
    GROBID's bibliography extraction can miss references, especially in
    humanities-style documents with non-standard layouts, dash-abbreviated
    authors, or unusual formatting. We need to know what's missing.

The approach uses three detection layers, each catching what the previous missed:

    Layer 1 — GROBID's own citation markers:
        GROBID annotates inline citations as <ref type="bibr"> elements in the
        TEI-XML. These are ML-detected and linked to bibliography entries. This
        is our most reliable source, but GROBID sometimes misses citations or
        fails to link them to bibliography entries.

    Layer 2 — Regex pattern matching:
        We scan the raw body text for citation-like patterns (parenthetical and
        narrative forms) using regex. This catches formally styled citations that
        GROBID missed, including multi-citation parenthetical groups and
        non-English author connectors. It will miss informal/discursive
        references and numbered citation styles.

    Layer 3 — LLM detection:
        We send each paragraph to the local LLM and ask it to list all works
        cited or referenced in the passage. This catches discursive references
        ("as Smith argued in her 2020 monograph"), organizational authors,
        informal references, and edge cases that no regex can handle. This is
        the slowest layer but the most flexible.

    After detection, all results are deduplicated, reconciled against the
    bibliography, and reported with match/unmatched status and hints for
    recovering missing references.

Each detected citation is tagged with its detection source(s), so you can see
which layer found it and assess the reliability of each detection.
"""

import json
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Any

import requests
from unidecode import unidecode

from .utils import write_json

logger = logging.getLogger(__name__)


# =============================================================================
# Regex patterns (Layer 2)
# =============================================================================

# Name pattern: handles diacritics, hyphenated surnames, etc.
_NAME = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+(?:[-'][A-ZÀ-ÖØ-Þa-zà-öø-ÿ]+)*"
_PARTICLE = r"(?:(?:de|van|von|di|el|al-|La|Le|Mc|Mac|O')\s+)?"
_FULL_NAME = _PARTICLE + _NAME
_AND = r"(?:and|&|og|und|och|et|ja)"
_YEAR = r"(?:19|20)\d{2}[a-c]?"

# Build complete regex patterns as strings, then compile.
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

# Compile with fallbacks for older/stricter regex engines.
try:
    _PAREN_CITATION_RE = re.compile(_PAREN_PATTERN, re.UNICODE)
    _NARRATIVE_CITATION_RE = re.compile(_NARRATIVE_PATTERN, re.UNICODE)
    _AUTHOR_YEAR_RE = re.compile(_AY_PATTERN, re.UNICODE)
    _NARRATIVE_AUTHOR_YEAR_RE = re.compile(_NAY_PATTERN, re.UNICODE)
except re.error as e:
    logger.warning("Complex citation regex failed: %s. Using simplified patterns.", e)
    _PAREN_CITATION_RE = re.compile(
        r'\([A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+(?:\s+(?:and|&|og|und|och|et)\s+[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)?'
        r'(?:\s+et\s+al\.?)?\s+(?:19|20)\d{2}[a-c]?[^)]*\)',
        re.UNICODE
    )
    _NARRATIVE_CITATION_RE = re.compile(
        r'[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+(?:\s+(?:and|&|og|und|och|et)\s+[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)?'
        r'(?:\s+et\s+al\.?)?\s*\(\s*(?:19|20)\d{2}[a-c]?[^)]*\)',
        re.UNICODE
    )
    _AUTHOR_YEAR_RE = re.compile(
        r'([A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)\s+(?:(?:and|&|og|und|och|et)\s+[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+\s+)?'
        r'(?:et\s+al\.?\s+)?((?:19|20)\d{2}[a-c]?)',
        re.UNICODE
    )
    _NARRATIVE_AUTHOR_YEAR_RE = re.compile(
        r'([A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+)\s*(?:(?:and|&|og|und|och|et)\s+[A-Z\u00C0-\u00D6][a-z\u00E0-\u00F6]+\s*)?'
        r'(?:et\s+al\.?\s*)?\(\s*((?:19|20)\d{2}[a-c]?)',
        re.UNICODE
    )


# =============================================================================
# LLM prompt for citation detection (Layer 3)
# =============================================================================

LLM_CITATION_DETECT_PROMPT = """You are an expert at identifying bibliographic references in academic text. Your task is to find ALL works that are cited or referenced in the following passage.

## Passage

---
{paragraph_text}
---

## Task

List every distinct work that is cited or referenced in this passage. Include:
- Formal parenthetical citations like (Smith 2020)
- Narrative citations like "Smith (2020) argued..."
- Discursive references like "as Smith argued in her 2020 monograph"
- Organizational authors like (UNESCO 2019)
- Any other form of reference to a specific published work

For each work, extract:
- first_author: The family name of the first author (or organizational name)
- year: The publication year (4 digits, with optional a/b/c suffix)

Respond ONLY with a JSON array. If no citations are found, respond with an empty array [].
Example: [{{"first_author": "Smith", "year": "2020"}}, {{"first_author": "Barrett", "year": "2010"}}]

Do not include page numbers, figure numbers, or other locators. Do not include references to figures, tables, or sections within the same document. Only include references to OTHER published works."""


# =============================================================================
# Main functions
# =============================================================================

def audit_references(
    tei_xml: str,
    bibliography: list[dict],
    source_pdf: str = "",
    llm_config: dict | None = None,
    paragraphs: list[dict] | None = None,
) -> dict:
    """
    Detect in-text citations using three layers and reconcile against bibliography.

    Args:
        tei_xml:      Raw TEI-XML string from GROBID.
        bibliography: List of reference dicts (from tei_parser.parse_tei_references).
        source_pdf:   Filename of the source PDF (for reporting).
        llm_config:   Optional dict with 'base_url', 'model', 'temperature', 'timeout'
                      for Layer 3 LLM detection. If None, Layer 3 is skipped.
        paragraphs:   Optional list of paragraph dicts (from tei_parser.parse_tei_body).
                      Used for Layer 3 LLM detection. If None and llm_config is provided,
                      paragraphs are re-parsed from tei_xml.

    Returns:
        Audit report dict.
    """
    from .tei_parser import _parse_xml, _get_text, TEI_NS, NS, parse_tei_body

    root = _parse_xml(tei_xml)
    if root is None:
        return {"error": "Failed to parse TEI-XML"}

    body = root.find(f".//{{{TEI_NS}}}body")
    if body is None:
        return {"error": "No body element in TEI-XML"}

    raw_text = _get_text(body)

    # --- Layer 1: GROBID's own citation markers ---
    grobid_citations = _detect_grobid_citations(root, TEI_NS, NS)
    logger.info(
        "Layer 1 (GROBID markers): %d unique citations in %s.",
        len(grobid_citations), source_pdf or "document",
    )

    # --- Layer 2: Regex detection ---
    regex_citations = _detect_regex_citations(raw_text)
    logger.info(
        "Layer 2 (regex): %d unique citations in %s.",
        len(regex_citations), source_pdf or "document",
    )

    # --- Layer 3: LLM detection (optional) ---
    llm_citations = {}
    if llm_config:
        if paragraphs is None:
            paragraphs = parse_tei_body(tei_xml)
        llm_citations = _detect_llm_citations(paragraphs, llm_config)
        logger.info(
            "Layer 3 (LLM): %d unique citations in %s.",
            len(llm_citations), source_pdf or "document",
        )

    # --- Merge all detections ---
    all_citations = _merge_detections(grobid_citations, regex_citations, llm_citations)
    logger.info(
        "Combined: %d unique citations after deduplication in %s.",
        len(all_citations), source_pdf or "document",
    )

    # --- Build bibliography lookup ---
    bib_lookup = _build_bib_lookup(bibliography)

    # --- Reconcile ---
    matched = []
    unmatched = []

    for key, info in all_citations.items():
        author, year = key
        bib_match = _find_in_bibliography(author, year, bib_lookup)
        entry = {
            "first_author": author,
            "year": year,
            "occurrences": info["occurrences"],
            "detected_by": info["detected_by"],
            "example_context": info["example_context"][:300] if info["example_context"] else "",
        }
        if bib_match:
            entry["matched_citekey"] = bib_match
            matched.append(entry)
        else:
            entry["hint"] = _build_hint(author, year, info.get("contexts", []))
            unmatched.append(entry)

    # Sort unmatched by occurrence count.
    unmatched.sort(key=lambda x: x["occurrences"], reverse=True)

    match_rate = len(matched) / len(all_citations) if all_citations else 1.0

    report = {
        "source_pdf": source_pdf,
        "detection_layers": {
            "layer_1_grobid": {
                "description": "Citations detected by GROBID's ML-based <ref> annotation.",
                "unique_citations": len(grobid_citations),
            },
            "layer_2_regex": {
                "description": (
                    "Citations detected by regex patterns for parenthetical and "
                    "narrative citation forms, independent of GROBID."
                ),
                "unique_citations": len(regex_citations),
            },
            "layer_3_llm": {
                "description": (
                    "Citations detected by sending each paragraph to the local LLM, "
                    "which identifies all referenced works including discursive and "
                    "informal references that regex cannot capture."
                ),
                "unique_citations": len(llm_citations),
                "status": "ran" if llm_config else "skipped (no llm_config provided)",
            },
        },
        "total_unique_citations": len(all_citations),
        "total_bibliography_entries": len(bibliography),
        "matched": len(matched),
        "unmatched": len(unmatched),
        "match_rate": round(match_rate, 3),
        "matched_citations": matched,
        "unmatched_citations": unmatched,
    }

    if unmatched:
        logger.warning(
            "%s: %d of %d in-text citations have no matching bibliography entry.",
            source_pdf or "document",
            len(unmatched),
            len(all_citations),
        )

    return report


# =============================================================================
# Layer 1: GROBID citation markers
# =============================================================================

def _detect_grobid_citations(root, TEI_NS: str, NS: dict) -> dict[tuple, dict]:
    """
    Extract citations from GROBID's <ref type="bibr"> elements.

    GROBID links each inline citation to a bibliography entry via a target
    attribute. We extract the marker text and, where possible, parse an
    (author, year) pair from it.
    """
    citations = {}

    body = root.find(f".//{{{TEI_NS}}}body")
    if body is None:
        return citations

    for ref in body.iter(f"{{{TEI_NS}}}ref"):
        if ref.get("type") != "bibr":
            continue

        marker_text = "".join(ref.itertext()).strip()
        if not marker_text:
            continue

        # Try to extract (author, year) from the marker text.
        pairs = _extract_author_year_pairs(marker_text)
        for author, year in pairs:
            key = (author, year)
            if key not in citations:
                citations[key] = {
                    "occurrences": 0,
                    "detected_by": ["grobid"],
                    "example_context": marker_text,
                    "contexts": [marker_text],
                }
            citations[key]["occurrences"] += 1

    return citations


# =============================================================================
# Layer 2: Regex detection
# =============================================================================

def _detect_regex_citations(text: str) -> dict[tuple, dict]:
    """
    Detect citations using regex patterns for parenthetical and narrative forms.
    """
    citations = {}

    # Parenthetical citations.
    for match in _PAREN_CITATION_RE.finditer(text):
        full = match.group(0)
        for ay in _AUTHOR_YEAR_RE.finditer(full):
            author = ay.group(1).strip()
            year = ay.group(2).strip()
            key = (_norm_author(author), year)
            ctx = _extract_context(text, match.start(), match.end())
            if key not in citations:
                citations[key] = {
                    "occurrences": 0,
                    "detected_by": ["regex"],
                    "example_context": ctx,
                    "contexts": [],
                }
            citations[key]["occurrences"] += 1
            citations[key]["contexts"].append(ctx)

    # Narrative citations.
    for match in _NARRATIVE_AUTHOR_YEAR_RE.finditer(text):
        author = match.group(1).strip()
        year = match.group(2).strip()
        key = (_norm_author(author), year)
        ctx = _extract_context(text, match.start(), match.end())
        if key not in citations:
            citations[key] = {
                "occurrences": 0,
                "detected_by": ["regex"],
                "example_context": ctx,
                "contexts": [],
            }
        citations[key]["occurrences"] += 1
        citations[key]["contexts"].append(ctx)

    return citations


# =============================================================================
# Layer 3: LLM detection
# =============================================================================

def _detect_llm_citations(
    paragraphs: list[dict],
    llm_config: dict,
) -> dict[tuple, dict]:
    """
    Detect citations by sending each paragraph to the local LLM.

    The LLM identifies all works referenced in the text, including discursive
    and informal references that regex cannot capture.
    """
    from tqdm import tqdm

    citations = {}

    base_url = llm_config.get("base_url", "http://localhost:11434")
    model = llm_config.get("model", "qwen3:35b")
    temperature = llm_config.get("temperature", 0.2)
    timeout = llm_config.get("timeout", 120)

    # Filter to paragraphs with enough text to plausibly contain citations.
    substantive = [p for p in paragraphs if len(p.get("text", "")) > 50]

    logger.info("Layer 3: Sending %d paragraphs to LLM for citation detection...", len(substantive))

    for para in tqdm(substantive, desc="LLM audit"):
        para_text = para.get("text", "")
        # Strip existing citation placeholders for cleaner LLM input.
        para_text = re.sub(r"\{\{CITE:\w*\}\}", "", para_text).strip()

        if len(para_text) < 50:
            continue

        prompt = LLM_CITATION_DETECT_PROMPT.format(paragraph_text=para_text)

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": temperature,
                "num_predict": 1024,
            },
        }

        try:
            resp = requests.post(
                f"{base_url}/api/generate",
                json=payload,
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue

            data = resp.json()
            response_text = data.get("response", "").strip()

            parsed = _parse_llm_json_array(response_text)
            if not parsed:
                continue

            for item in parsed:
                author = str(item.get("first_author", "")).strip()
                year = str(item.get("year", "")).strip()

                if not author or not year or not re.match(r"^(19|20)\d{2}[a-c]?$", year):
                    continue

                key = (_norm_author(author), year)
                if key not in citations:
                    citations[key] = {
                        "occurrences": 0,
                        "detected_by": ["llm"],
                        "example_context": para_text[:300],
                        "contexts": [],
                    }
                citations[key]["occurrences"] += 1
                citations[key]["contexts"].append(para_text[:200])

        except (requests.Timeout, requests.ConnectionError):
            continue
        except Exception as e:
            logger.debug("LLM citation detection error: %s", e)
            continue

    return citations


# =============================================================================
# Merge and reconcile
# =============================================================================

def _merge_detections(
    grobid: dict[tuple, dict],
    regex: dict[tuple, dict],
    llm: dict[tuple, dict],
) -> dict[tuple, dict]:
    """
    Merge detections from all three layers into a single deduplicated dict.

    When the same (author, year) pair is found by multiple layers, we combine
    their data and track which layers detected it.
    """
    merged = {}

    for source_name, source_dict in [("grobid", grobid), ("regex", regex), ("llm", llm)]:
        for key, info in source_dict.items():
            # Normalize the key for deduplication.
            norm_key = (_norm_author(key[0]), key[1][:4])

            if norm_key not in merged:
                merged[norm_key] = {
                    "occurrences": 0,
                    "detected_by": [],
                    "example_context": "",
                    "contexts": [],
                }

            merged[norm_key]["occurrences"] += info.get("occurrences", 1)

            for layer in info.get("detected_by", [source_name]):
                if layer not in merged[norm_key]["detected_by"]:
                    merged[norm_key]["detected_by"].append(layer)

            if not merged[norm_key]["example_context"] and info.get("example_context"):
                merged[norm_key]["example_context"] = info["example_context"]

            merged[norm_key]["contexts"].extend(info.get("contexts", []))

    # Deduplicate contexts.
    for key in merged:
        merged[key]["contexts"] = list(dict.fromkeys(merged[key]["contexts"]))[:5]

    return merged


def _build_bib_lookup(bibliography: list[dict]) -> list[dict]:
    """Build a lookup structure from bibliography entries for matching."""
    lookup = []
    for ref in bibliography:
        authors = ref.get("author", [])
        first_family = ""
        if authors and authors[0].get("family"):
            first_family = authors[0]["family"]

        year = ""
        date = ref.get("date", "")
        year_match = re.search(r"\b((?:19|20)\d{2})\b", str(date))
        if year_match:
            year = year_match.group(1)

        lookup.append({
            "family": first_family,
            "family_lower": first_family.lower(),
            "family_ascii": unidecode(first_family).lower(),
            "year": year,
            "citekey": ref.get("citekey", ""),
            "title": ref.get("title", ""),
        })

    return lookup


def _find_in_bibliography(
    author: str,
    year: str,
    bib_lookup: list[dict],
) -> str | None:
    """
    Try to match an (author, year) pair to a bibliography entry.

    Matching is fuzzy on the author name (handles diacritics, transliteration,
    substring matching for hyphenated names) and exact on the year (base 4 digits).
    """
    author_lower = author.lower()
    author_ascii = unidecode(author).lower()
    year_base = year[:4] if len(year) >= 4 else year

    for entry in bib_lookup:
        if not entry["year"] or entry["year"] != year_base:
            continue

        # Exact match (case-insensitive).
        if author_lower == entry["family_lower"]:
            return entry["citekey"]

        # ASCII-transliterated match.
        if author_ascii == entry["family_ascii"]:
            return entry["citekey"]

        # Substring match (for hyphenated names).
        if len(author_lower) >= 4:
            if author_lower in entry["family_lower"] or entry["family_lower"] in author_lower:
                return entry["citekey"]

    return None


# =============================================================================
# Helpers
# =============================================================================

def _extract_author_year_pairs(text: str) -> list[tuple[str, str]]:
    """Extract (author, year) pairs from a citation marker string."""
    pairs = []

    for match in _AUTHOR_YEAR_RE.finditer(text):
        pairs.append((_norm_author(match.group(1)), match.group(2)))

    if not pairs:
        for match in _NARRATIVE_AUTHOR_YEAR_RE.finditer(text):
            pairs.append((_norm_author(match.group(1)), match.group(2)))

    return pairs


def _norm_author(author: str) -> str:
    """Normalize an author name: strip whitespace, keep original casing."""
    return author.strip()


def _extract_context(text: str, start: int, end: int, window: int = 100) -> str:
    """Extract surrounding text around a match for context display."""
    ctx_start = max(0, start - window)
    ctx_end = min(len(text), end + window)
    context = text[ctx_start:ctx_end].strip()
    if ctx_start > 0:
        context = "..." + context
    if ctx_end < len(text):
        context = context + "..."
    return context


def _build_hint(author: str, year: str, contexts: list[str]) -> dict:
    """Build a hint for recovering an unmatched citation."""
    return {
        "search_terms": {
            "author_surname": author,
            "author_ascii": unidecode(author),
            "year": year,
        },
        "example_contexts": contexts[:3],
        "suggestion": (
            f"Look for a reference by '{author}' published in {year} "
            f"in the bibliography section of the source PDF. "
            f"This citation appears {len(contexts)} time(s) in the body text."
        ),
    }


def _parse_llm_json_array(text: str) -> list[dict] | None:
    """Parse a JSON array from LLM response, handling common formatting issues."""
    text = text.strip()

    # Strip think tags.
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    text = re.sub(r"<think>[\s\S]*$", "", text).strip()

    # Try direct parse.
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Strip markdown fences.
    if text.startswith("```"):
        lines = text.split("\n")
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

    # Find first JSON array.
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None
