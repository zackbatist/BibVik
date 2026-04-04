"""
bibvik.reference_resolver — Resolve unmatched in-text citations to full
bibliographic records.

Problem:
    The reference audit identifies in-text citations (author, year pairs)
    that have no matching bibliography entry. These arise when:
    - GROBID failed to extract a reference from the bibliography section
    - The paper uses footnote-style references not caught by footnote_extractor
    - The PDF was cut off before the bibliography
    - GROBID merged two consecutive entries into one

Approach (two tiers):
    Tier 1 — CrossRef API:
        Query CrossRef's free /works endpoint by author surname and year.
        CrossRef has good coverage of English-language journal articles and
        books; coverage of Scandinavian grey literature is patchy.
        A result is accepted when:
          - The first author's family name matches (unaccented, case-insensitive)
          - The year matches exactly
          - The returned title has ≥ 0.5 token overlap with any example context
            keywords OR the result DOI can be confirmed (high confidence)

    Tier 2 — LLM fallback:
        Send the example citation contexts to the local LLM and ask it to
        infer the full bibliographic metadata. The LLM is particularly good
        at recovering non-English works and grey literature that CrossRef
        doesn't know about.

Output:
    Resolved entries are added to the bibliography with:
      _resolution_method: "crossref" | "llm_from_context"
      _resolution_confidence: "high" | "medium" | "low"
      _source_contexts: list of example citation contexts used for resolution

    The updated bibliography is written back to bibliography.json.
    A resolution report is written to output/resolution_report.json.
"""

import json
import logging
import re
import time
from typing import Any

import requests
from unidecode import unidecode

logger = logging.getLogger(__name__)


# =============================================================================
# CrossRef API client
# =============================================================================

CROSSREF_BASE = "https://api.crossref.org/works"
CROSSREF_ROWS = 3        # Max results to consider per query
CROSSREF_DELAY = 0.1     # Seconds between requests (polite)


def query_crossref(
    author: str,
    year: str,
    context_keywords: list[str],
    email: str = "",
) -> dict | None:
    """
    Query CrossRef for a reference by author surname and year.

    Args:
        author:           First author surname (may contain diacritics).
        year:             4-digit publication year string.
        context_keywords: Keywords extracted from example citation contexts,
                          used for title similarity scoring.
        email:            Contact email for CrossRef's polite pool (faster rates).

    Returns:
        A reference dict with biblatex-style fields, or None if no confident
        match was found.
    """
    # CrossRef handles diacritics in queries reasonably, but ASCII fallback
    # helps with inconsistent GROBID author normalization.
    author_ascii = unidecode(author).lower()

    params: dict[str, Any] = {
        "query.author": author_ascii,
        "filter": f"from-pub-date:{year},until-pub-date:{year}",
        "rows": CROSSREF_ROWS,
        "select": "DOI,title,author,published,type,container-title,volume,issue,page,publisher",
    }
    if email:
        params["mailto"] = email

    headers = {"User-Agent": f"BibVik/1.0 (mailto:{email})" if email else "BibVik/1.0"}

    try:
        resp = requests.get(
            CROSSREF_BASE,
            params=params,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.warning("CrossRef request failed for %s %s: %s", author, year, e)
        return None
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("CrossRef response parse error for %s %s: %s", author, year, e)
        return None

    items = data.get("message", {}).get("items", [])
    if not items:
        return None

    # Score each result and return the best confident match.
    best = _score_crossref_results(items, author, year, context_keywords)
    return best


def _score_crossref_results(
    items: list[dict],
    author: str,
    year: str,
    context_keywords: list[str],
) -> dict | None:
    """
    Score CrossRef results and return the best match above confidence threshold.
    """
    author_norm = unidecode(author).lower()

    best_score = 0.0
    best_item = None

    for item in items:
        score = 0.0

        # --- Author check ---
        cr_authors = item.get("author", [])
        if not cr_authors:
            continue
        first_family = unidecode(cr_authors[0].get("family", "")).lower()
        if not first_family:
            continue
        if first_family[:4] == author_norm[:4]:
            score += 0.5
        elif author_norm[:4] in first_family or first_family[:4] in author_norm:
            score += 0.3
        else:
            continue  # Author mismatch — skip entirely

        # --- Year check ---
        pub_date = item.get("published", {})
        date_parts = pub_date.get("date-parts", [[]])[0]
        cr_year = str(date_parts[0]) if date_parts else ""
        if cr_year == year:
            score += 0.3
        else:
            continue  # Year mismatch — skip

        # --- Title overlap with context keywords ---
        titles = item.get("title", [])
        cr_title = titles[0] if titles else ""
        if cr_title and context_keywords:
            title_tokens = set(_tokenize(cr_title))
            kw_tokens = set(context_keywords)
            overlap = len(title_tokens & kw_tokens) / max(len(kw_tokens), 1)
            score += overlap * 0.2

        if score > best_score:
            best_score = score
            best_item = item

    # Accept if author + year both matched (score ≥ 0.8).
    if best_score >= 0.8 and best_item is not None:
        return _crossref_item_to_ref(best_item, author, year, best_score)

    return None


def _crossref_item_to_ref(
    item: dict,
    queried_author: str,
    queried_year: str,
    score: float,
) -> dict:
    """Convert a CrossRef item dict to a biblatex-style reference dict."""
    ref: dict[str, Any] = {}

    # Title
    titles = item.get("title", [])
    ref["title"] = titles[0] if titles else ""

    # Authors
    ref["author"] = []
    for a in item.get("author", []):
        family = a.get("family", "").strip()
        given = a.get("given", "").strip()
        if family:
            ref["author"].append({"family": family, "given": given})

    # Year / date
    pub_date = item.get("published", {})
    date_parts = pub_date.get("date-parts", [[]])[0]
    if date_parts:
        ref["year"] = str(date_parts[0])
        ref["date"] = "-".join(str(p) for p in date_parts)
    else:
        ref["year"] = queried_year
        ref["date"] = queried_year

    # DOI
    doi = item.get("DOI", "")
    if doi:
        ref["doi"] = doi

    # Container (journal or book)
    containers = item.get("container-title", [])
    cr_type = item.get("type", "")
    if containers:
        container = containers[0]
        if cr_type in ("journal-article",):
            ref["journaltitle"] = container
            ref["entry_type"] = "article"
        else:
            ref["booktitle"] = container
            ref["entry_type"] = "incollection"
    elif cr_type in ("book", "monograph"):
        ref["entry_type"] = "book"
    else:
        ref["entry_type"] = "misc"

    # Volume, issue, pages
    if item.get("volume"):
        ref["volume"] = str(item["volume"])
    if item.get("issue"):
        ref["number"] = str(item["issue"])
    if item.get("page"):
        ref["pages"] = item["page"].replace("-", "--")

    # Publisher
    if item.get("publisher"):
        ref["publisher"] = item["publisher"]

    # Provenance
    ref["_resolution_method"] = "crossref"
    ref["_resolution_confidence"] = "high" if score >= 0.95 else "medium"
    ref["_crossref_score"] = round(score, 3)

    return ref


# =============================================================================
# LLM fallback resolver
# =============================================================================

LLM_RESOLUTION_PROMPT = """You are an expert bibliographer. A citation appears in an academic paper but its full bibliographic details are unknown. Your task is to infer the most likely complete bibliographic record from the available evidence.

## Citation evidence

Author surname: {author}
Year: {year}
Example citation contexts (passages where this citation appears):
{contexts}

## Task

Based on the author, year, and citation contexts, infer the most likely bibliographic metadata for this reference. Academic papers in Viking Age archaeology cite a mix of English, Norwegian, Swedish, Danish, German, and French sources.

Return a JSON object with as many of these fields as you can confidently infer. Omit fields you cannot determine — do not guess. Only include fields you are reasonably confident about based on the evidence.

Fields:
- "title": Full title of the work
- "author": List of {{"family": "...", "given": "..."}} dicts
- "date": Publication year (4-digit string)
- "entry_type": One of "article", "incollection", "book", "misc"
- "journaltitle": Journal name (for articles)
- "booktitle": Book or edited volume title (for chapters)
- "volume": Volume number
- "number": Issue number  
- "pages": Page range
- "publisher": Publisher name
- "location": Place of publication
- "doi": DOI if known
- "confidence": "high", "medium", or "low" — your confidence in the inferred metadata

Respond ONLY with the JSON object. No preamble, no markdown fences."""


def resolve_via_llm(
    author: str,
    year: str,
    contexts: list[str],
    llm_config: dict,
) -> dict | None:
    """
    Use the local LLM to infer bibliographic metadata from citation contexts.

    Args:
        author:     First author surname.
        year:       Publication year.
        contexts:   List of verbatim citation context strings.
        llm_config: Dict with 'base_url', 'model', 'temperature', 'timeout'.

    Returns:
        Reference dict with inferred metadata, or None if LLM failed.
    """
    contexts_text = "\n".join(f"- {c}" for c in contexts[:5] if c.strip())
    if not contexts_text:
        logger.debug("No contexts available for LLM resolution of %s %s", author, year)
        return None

    prompt = LLM_RESOLUTION_PROMPT.format(
        author=author,
        year=year,
        contexts=contexts_text,
    )

    payload = {
        "model": llm_config.get("model", "qwen3:35b"),
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {
            "temperature": llm_config.get("temperature", 0.2),
            "num_predict": 1024,
        },
    }

    base_url = llm_config.get("base_url", "http://localhost:11434").rstrip("/")

    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json=payload,
            timeout=llm_config.get("timeout", 300),
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "")

        # Strip think tags.
        text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
        text = re.sub(r"<think>[\s\S]*$", "", text).strip()

        if not text:
            return None

        result = _parse_llm_json(text)
        if not result or not isinstance(result, dict):
            return None

        # Validate minimum fields.
        if not result.get("title") or len(result.get("title", "")) < 5:
            return None

        # Ensure author is present.
        if not result.get("author"):
            result["author"] = [{"family": author, "given": ""}]

        # Ensure year.
        if not result.get("date"):
            result["date"] = year
        if not result.get("year"):
            result["year"] = year

        # Entry type fallback.
        if not result.get("entry_type"):
            result["entry_type"] = "misc"

        # Provenance.
        confidence = result.pop("confidence", "low")
        result["_resolution_method"] = "llm_from_context"
        result["_resolution_confidence"] = confidence

        return result

    except requests.RequestException as e:
        logger.warning("LLM request failed for %s %s: %s", author, year, e)
        return None
    except Exception as e:
        logger.warning("LLM resolution error for %s %s: %s", author, year, e)
        return None


# =============================================================================
# Main resolution orchestrator
# =============================================================================

def resolve_unmatched_citations(
    audit_report: dict,
    bibliography: dict[str, dict],
    email: str = "",
    llm_config: dict | None = None,
    min_occurrences: int = 1,
) -> dict[str, Any]:
    """
    Attempt to resolve all unmatched citations in the audit report.

    For each unmatched citation, tries CrossRef first, then the LLM.
    Successfully resolved citations are added to the bibliography dict
    in-place with provenance fields.

    Args:
        audit_report:    The full audit output dict (from run.py's audit stage).
        bibliography:    The bibliography dict (entries only, no _metadata).
                         Modified in-place.
        email:           Contact email for CrossRef polite pool.
        llm_config:      LLM configuration dict. If None, LLM fallback is skipped.
        min_occurrences: Only attempt resolution for citations that appear at
                         least this many times (reduces noise from false detections).

    Returns:
        Resolution report dict suitable for writing to resolution_report.json.
    """
    from .utils import generate_citekey
    from .normalize import normalize_entry

    unmatched = audit_report.get("aggregated_unmatched", [])
    if not unmatched:
        logger.info("No unmatched citations to resolve.")
        return {"summary": {"attempted": 0, "resolved": 0}, "resolutions": []}

    # Filter by occurrence threshold.
    candidates = [
        u for u in unmatched
        if u.get("total_occurrences", 0) >= min_occurrences
    ]
    logger.info(
        "Attempting resolution for %d of %d unmatched citations "
        "(min_occurrences=%d).",
        len(candidates), len(unmatched), min_occurrences,
    )

    # Build a set of existing citekeys and a title lookup for deduplication.
    existing_titles = {
        _norm_title(v.get("title", "")): k
        for k, v in bibliography.items()
        if v.get("title")
    }

    resolved = []
    skipped = []
    failed = []

    for item in candidates:
        author = item.get("first_author", "")
        year = item.get("year", "")
        hints = item.get("hints", [{}])
        hint = hints[0] if hints else {}
        contexts = hint.get("example_contexts", [])

        if not author or not year:
            failed.append({"author": author, "year": year, "reason": "missing author or year"})
            continue

        logger.info("Resolving: %s %s (%d occurrences)", author, year, item.get("total_occurrences", 0))

        # Extract keywords from contexts for title scoring.
        context_keywords = _extract_context_keywords(contexts)

        # --- Tier 1: CrossRef ---
        ref = None
        if email or True:  # Always try CrossRef
            ref = query_crossref(author, year, context_keywords, email)
            time.sleep(CROSSREF_DELAY)

        if ref:
            logger.info(
                "  CrossRef resolved: %s (%s)",
                ref.get("title", "")[:60],
                ref.get("_resolution_confidence", "?"),
            )
        elif llm_config:
            # --- Tier 2: LLM fallback ---
            ref = resolve_via_llm(author, year, contexts, llm_config)
            if ref:
                logger.info(
                    "  LLM resolved: %s (%s)",
                    ref.get("title", "")[:60],
                    ref.get("_resolution_confidence", "?"),
                )

        if not ref:
            failed.append({"author": author, "year": year, "reason": "no match found"})
            logger.info("  Failed: no match from CrossRef or LLM.")
            continue

        # --- Deduplication: check if this title already exists ---
        ref_title_norm = _norm_title(ref.get("title", ""))
        if ref_title_norm in existing_titles:
            existing_ck = existing_titles[ref_title_norm]
            skipped.append({
                "author": author,
                "year": year,
                "reason": f"duplicate of existing entry {existing_ck}",
                "resolved_title": ref.get("title", ""),
            })
            logger.info(
                "  Skipped: duplicate of existing entry %s.", existing_ck
            )
            continue

        # --- Assign citekey ---
        citekey = generate_citekey(ref.get("author", []), ref.get("year", year))

        # --- Add provenance fields ---
        ref["citekey"] = citekey
        if not ref.get("generation"):
            ref["generation"] = "resolved"
        ref["cited_by"] = []  # Will be populated if citation_contexts is re-run
        ref["_source_contexts"] = contexts[:3]

        # --- Normalize ---
        normalize_entry(ref)

        # --- Insert into bibliography ---
        bibliography[citekey] = ref
        existing_titles[ref_title_norm] = citekey

        resolved.append({
            "author": author,
            "year": year,
            "citekey": citekey,
            "title": ref.get("title", ""),
            "resolution_method": ref.get("_resolution_method", ""),
            "resolution_confidence": ref.get("_resolution_confidence", ""),
            "total_occurrences": item.get("total_occurrences", 0),
        })

    summary = {
        "candidates": len(candidates),
        "resolved": len(resolved),
        "skipped_duplicates": len(skipped),
        "failed": len(failed),
        "resolution_rate": round(len(resolved) / len(candidates), 3) if candidates else 0.0,
        "by_method": {
            "crossref": sum(1 for r in resolved if r["resolution_method"] == "crossref"),
            "llm_from_context": sum(1 for r in resolved if r["resolution_method"] == "llm_from_context"),
        },
    }

    logger.info(
        "Resolution complete: %d resolved, %d skipped (duplicate), %d failed.",
        len(resolved), len(skipped), len(failed),
    )

    return {
        "summary": summary,
        "resolutions": resolved,
        "skipped": skipped,
        "failed": failed,
    }


# =============================================================================
# Helpers
# =============================================================================

def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split into tokens of length ≥ 3."""
    text = unidecode(text).lower()
    return [t for t in re.split(r"[^\w]+", text) if len(t) >= 3]


def _norm_title(title: str) -> str:
    """Normalize a title for deduplication comparison."""
    t = unidecode(title).lower()
    t = re.sub(r"[^\w\s]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _extract_context_keywords(contexts: list[str]) -> list[str]:
    """
    Extract meaningful keywords from example citation contexts for
    use in CrossRef title scoring.

    Skips stop words and very short tokens. Returns up to 20 keywords.
    """
    stop = {
        "the", "and", "for", "that", "with", "this", "are", "was", "has",
        "been", "from", "have", "they", "which", "also", "but", "not",
        "more", "its", "their", "were", "into", "can", "our",
    }
    tokens: list[str] = []
    for ctx in contexts:
        for tok in _tokenize(ctx):
            if tok not in stop and len(tok) >= 4:
                tokens.append(tok)

    # Deduplicate preserving order.
    seen: set[str] = set()
    unique = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique[:20]


def _parse_llm_json(text: str) -> dict | None:
    """Parse a JSON object from LLM response text."""
    # Strip markdown fences.
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:] if lines[0].startswith("```") else lines
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # Find first JSON object.
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    return None
