"""
bibvik.citation_collector — Find every work cited by a paper, from all sources.

The question is simple: what works does this paper cite?

Sources:
    1. GROBID bibliography — structured entries from the PDF's reference section.
       Present when the paper has a machine-readable bibliography. Absent for
       papers with endnotes, footnote-only citations, or non-standard layouts.

    2. LLM body detection — the local LLM reads every paragraph and identifies
       all cited works, including inline author-year citations, discursive
       references, and anything regex cannot capture. Works regardless of
       bibliography format. This is the primary fallback when GROBID finds nothing.

    3. LLM footnote extraction — the local LLM reads footnote text and extracts
       structured bibliographic metadata for references embedded in footnotes.

After collecting, each unique citation is:
    - Checked against the global bibliography → if found, record cited_by
    - If not found, resolved via CrossRef → add to global bibliography
    - If CrossRef fails, resolved via LLM → add to bibliography
    - If all fail, recorded in summary as unresolvable
"""

import json
import logging
import re

import requests
from unidecode import unidecode

logger = logging.getLogger(__name__)


# =============================================================================
# LLM citation detection prompt
# =============================================================================

_DETECT_PROMPT = """You are an expert at identifying bibliographic references in academic text. Your task is to find ALL works that are cited or referenced in the following passage.

## Passage

---
{text}
---

## Task

List every distinct work cited or referenced in this passage. Include:
- Formal parenthetical citations: (Smith 2020), (Smith and Jones 2020)
- Narrative citations: Smith (2020) argued...
- Discursive references: as Smith argued in her 2020 monograph
- Organizational authors: (UNESCO 2019)
- Non-English citation styles common in Scandinavian, German, French scholarship

For each work extract:
- first_author: family name of the first author (or organizational name)
- year: publication year (4 digits, optional a/b/c suffix)

Respond ONLY with a JSON array. If no citations: []
Example: [{{"first_author": "Smith", "year": "2020"}}, {{"first_author": "Andrén", "year": "2006"}}]

Do not include page numbers, figure numbers, or locators. Only references to OTHER published works."""


# =============================================================================
# Public interface
# =============================================================================

def collect_and_resolve(
    paper_pdf_name: str,
    paper_citekey: str | None,
    tei_xml: str,
    grobid_refs: list[dict],
    paragraphs: list[dict],
    footnote_texts: list[dict],
    bibliography: dict[str, dict],
    llm_analyzer,
    llm_config: dict | None,
    email: str = "",
) -> dict:
    """
    Find every work cited by a paper and ensure each has a bibliography entry.

    Modifies bibliography in-place: adds new entries, updates cited_by lists.

    Args:
        paper_pdf_name:  PDF filename of the paper being processed.
        paper_citekey:   Citekey of this paper in the bibliography (may be None).
        tei_xml:         Raw TEI-XML from GROBID.
        grobid_refs:     Bibliography entries GROBID extracted (may be empty).
        paragraphs:      Parsed body paragraphs from GROBID.
        footnote_texts:  Footnote dicts from parse_tei_footnotes().
        bibliography:    Global bibliography dict — modified in-place.
        llm_analyzer:    Initialized LLMAnalyzer instance, or None.
        llm_config:      LLM config dict for direct API calls, or None.
        email:           Contact email for CrossRef polite pool.

    Returns:
        Summary dict with counts.
    """
    from .footnote_extractor import extract_footnote_references
    from .reference_resolver import resolve_unmatched_citations

    summary = {
        "grobid_refs": len(grobid_refs),
        "llm_detected": 0,
        "footnote_refs_added": 0,
        "resolved_via_crossref": 0,
        "resolved_via_llm": 0,
        "unresolvable": 0,
    }

    # Source 1: GROBID bibliography — already in bibliography, just record cited_by
    for ref in grobid_refs:
        ck = ref.get("citekey", "")
        if ck and ck in bibliography and paper_citekey:
            _add_cited_by(bibliography[ck], paper_citekey)

    # Source 2: LLM body detection
    llm_detected: dict[tuple, dict] = {}
    if llm_config and paragraphs:
        llm_detected = _detect_citations_llm(paragraphs, llm_config)
        summary["llm_detected"] = len(llm_detected)

    # Source 3: LLM footnote extraction
    if llm_analyzer and tei_xml:
        fn_result = extract_footnote_references(
            tei_files={paper_pdf_name: tei_xml},
            bibliography=bibliography,
            analyzer=llm_analyzer,
        )
        n_fn = fn_result["summary"]["references_merged_into_bibliography"]
        summary["footnote_refs_added"] = n_fn
        if paper_citekey and n_fn:
            for v in bibliography.values():
                if v.get("_source_footnote") and v.get("_source_pdf") == paper_pdf_name:
                    _add_cited_by(v, paper_citekey)

    # Resolve LLM-detected citations not yet in the bibliography
    if llm_detected:
        existing = _build_lookup(bibliography)
        to_resolve = []

        for (author, year), info in llm_detected.items():
            ck = _find_in_lookup(author, year, existing)
            if ck:
                if paper_citekey:
                    _add_cited_by(bibliography[ck], paper_citekey)
            else:
                to_resolve.append({
                    "first_author": author,
                    "year": year,
                    "total_occurrences": info.get("occurrences", 1),
                    "hints": [{"example_contexts": info.get("contexts", [])[:3],
                               "search_terms": {}}],
                })

        if to_resolve:
            result = resolve_unmatched_citations(
                audit_report={"aggregated_unmatched": to_resolve},
                bibliography=bibliography,
                email=email,
                llm_config=llm_config,
                min_occurrences=1,
            )
            rs = result["summary"]
            summary["resolved_via_crossref"] = rs["by_method"]["crossref"]
            summary["resolved_via_llm"] = rs["by_method"]["llm_from_context"]
            summary["unresolvable"] = rs["failed"]

            if paper_citekey:
                for res in result.get("resolutions", []):
                    ck = res.get("citekey")
                    if ck and ck in bibliography:
                        _add_cited_by(bibliography[ck], paper_citekey)

    return summary


def format_summary(summary: dict) -> str:
    """One-line human-readable summary of collect_and_resolve output."""
    parts = []
    if summary.get("grobid_refs"):
        parts.append(f"{summary['grobid_refs']} from bibliography")
    if summary.get("llm_detected"):
        parts.append(f"{summary['llm_detected']} detected in text")
    if summary.get("footnote_refs_added"):
        parts.append(f"+{summary['footnote_refs_added']} from footnotes")
    resolved = summary.get("resolved_via_crossref", 0) + summary.get("resolved_via_llm", 0)
    if resolved:
        parts.append(f"+{resolved} resolved")
    if summary.get("unresolvable"):
        parts.append(f"{summary['unresolvable']} unresolvable")
    return ",  ".join(parts) if parts else "no citations found"


# =============================================================================
# LLM citation detection
# =============================================================================

def _detect_citations_llm(
    paragraphs: list[dict],
    llm_config: dict,
) -> dict[tuple, dict]:
    """
    Ask the LLM to identify all cited works in the body text paragraphs.

    Returns dict keyed by (normalized_author, year) with occurrence counts
    and example contexts.
    """
    base_url = llm_config.get("base_url", "http://localhost:11434")
    model = llm_config.get("model", "qwen3:35b")
    temperature = llm_config.get("temperature", 0.2)
    timeout = llm_config.get("timeout", 120)

    substantive = [p for p in paragraphs if len(p.get("text", "")) > 50]
    citations: dict[tuple, dict] = {}

    for para in substantive:
        text = para.get("text", "")
        text = re.sub(r"\{\{CITE:\w*\}\}", "", text).strip()
        if len(text) < 50:
            continue

        try:
            resp = requests.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": _DETECT_PROMPT.format(text=text),
                    "stream": False,
                    "think": False,
                    "options": {"temperature": temperature, "num_predict": 512},
                },
                timeout=timeout,
            )
            if resp.status_code != 200:
                continue

            raw = resp.json().get("response", "").strip()
            raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()
            parsed = _parse_json_array(raw)
            if not parsed:
                continue

            for item in parsed:
                author = str(item.get("first_author", "")).strip()
                year = str(item.get("year", "")).strip()
                if not author or not re.match(r"^(19|20)\d{2}[a-c]?$", year):
                    continue
                key = (_norm(author), year)
                if key not in citations:
                    citations[key] = {"occurrences": 0, "contexts": []}
                citations[key]["occurrences"] += 1
                if len(citations[key]["contexts"]) < 3:
                    citations[key]["contexts"].append(text[:200])

        except (requests.Timeout, requests.ConnectionError):
            continue
        except Exception as e:
            logger.debug("LLM detection error: %s", e)
            continue

    return citations


# =============================================================================
# Helpers
# =============================================================================

def _norm(author: str) -> str:
    """Normalize author surname for deduplication."""
    return re.sub(r"[^a-z]", "", unidecode(author).lower())


def _add_cited_by(entry: dict, citekey: str) -> None:
    """Add citekey to entry's cited_by list if not already present."""
    entry.setdefault("cited_by", [])
    if citekey not in entry["cited_by"]:
        entry["cited_by"].append(citekey)


def _build_lookup(bibliography: dict[str, dict]) -> dict[tuple, str]:
    """Build (author_prefix, year) → citekey lookup."""
    lookup: dict[tuple, str] = {}
    for ck, entry in bibliography.items():
        if ck.startswith("_"):
            continue
        authors = entry.get("author", [])
        family = authors[0].get("family", "") if authors else ""
        prefix = _norm(family)[:6]
        year = entry.get("year", entry.get("date", ""))[:4]
        if prefix and year:
            lookup[(prefix, year)] = ck
    return lookup


def _find_in_lookup(author: str, year: str, lookup: dict[tuple, str]) -> str | None:
    """Find a citekey matching author+year."""
    prefix = _norm(author)[:6]
    year4 = year[:4]
    if (prefix, year4) in lookup:
        return lookup[(prefix, year4)]
    for (p, y), ck in lookup.items():
        if y == year4 and (prefix.startswith(p[:4]) or p.startswith(prefix[:4])):
            return ck
    return None


def _parse_json_array(text: str) -> list | None:
    """Parse a JSON array from LLM response."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return result
        except Exception:
            pass
    return None
