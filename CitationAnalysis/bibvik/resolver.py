"""
bibvik.resolver — Resolve unmatched citations to bibliographic records.

When the detector finds a citation (author, year) that doesn't match any
existing bibliography entry, the resolver attempts to build a record for it.

Tier 1 — CrossRef API:
    Query by author surname + year. Accept if the first author and year
    match and the title has reasonable token overlap with any context where
    the citation appears. Free API, no auth needed (polite pool with email).

Tier 2 — LLM metadata generation:
    Send the citation contexts to the LLM and ask it to infer bibliographic
    metadata. Particularly useful for non-English works and grey literature
    that CrossRef doesn't cover.

Resolved entries are tagged with:
    _resolution_method: "crossref" | "llm_from_context" | "llm_from_footnote"
    _resolution_confidence: "high" | "medium" | "low"
"""

import json
import logging
import re
import time
from typing import Any

import requests
from unidecode import unidecode

from .utils import extract_year, norm_author

logger = logging.getLogger(__name__)

CROSSREF_BASE = "https://api.crossref.org/works"
CROSSREF_DELAY = 0.15  # Polite delay between requests


# =============================================================================
# Main resolver
# =============================================================================

def resolve_citations(
    unmatched: dict[tuple, dict],
    email: str = "",
    llm_config: dict | None = None,
) -> list[dict]:
    """
    Attempt to resolve unmatched (author, year) citations to full records.

    Args:
        unmatched:  Dict mapping (norm_author, year) → detection info with
                    'author' (original casing), 'year', 'contexts'.
        email:      Contact email for CrossRef polite pool.
        llm_config: LLM config for tier 2. If None, tier 2 is skipped.

    Returns:
        List of resolved bibliographic record dicts, ready to merge into
        the bibliography. Each has _resolution_method and _resolution_confidence.
    """
    resolved = []

    for key, info in unmatched.items():
        author = info.get("author", key[0])
        year = info.get("year", key[1])
        contexts = info.get("contexts", [])

        # Tier 1: CrossRef
        if email:
            record = _try_crossref(author, year, contexts, email)
            if record:
                resolved.append(record)
                continue

        # Tier 2: LLM
        if llm_config and contexts:
            record = _try_llm(author, year, contexts, llm_config)
            if record:
                resolved.append(record)
                continue

        # Unresolvable — create a minimal stub
        resolved.append({
            "author": [{"family": author, "given": ""}],
            "date": year,
            "year": year,
            "title": "",
            "entry_type": "misc",
            "_resolution_method": "stub",
            "_resolution_confidence": "low",
        })

    return resolved


# =============================================================================
# Tier 1: CrossRef
# =============================================================================

def _try_crossref(
    author: str,
    year: str,
    contexts: list[str],
    email: str,
) -> dict | None:
    """
    Query CrossRef for a matching work.

    Acceptance criteria (all must pass):
    1. Year matches exactly.
    2. Normalised full surname matches. Short surnames (≤3 chars) use
       prefix matching as a fallback for truncation artifacts.
    3. Title/context plausibility: at least one content word from the
       CrossRef title appears in the combined citation contexts. Catches
       obvious domain mismatches (e.g. a pedagogy paper matched to a
       Viking Age archaeology citation). Skipped — and confidence
       downgraded to medium — when the title has no content words or
       contexts are empty (e.g. non-English contexts where vocabulary
       overlap is not expected).

    Confidence:
    - high:   full author match + overlap confirmed + DOI present
    - medium: full author match + overlap confirmed but no DOI; or
              overlap inconclusive (short/vague title or no contexts)
    """
    author_norm = _norm(author)

    try:
        resp = requests.get(
            CROSSREF_BASE,
            params={
                "query.author": author,
                "query.bibliographic": year,
                "rows": 3,
                "mailto": email,
            },
            timeout=15,
        )
        time.sleep(CROSSREF_DELAY)

        if resp.status_code != 200:
            return None

        items = resp.json().get("message", {}).get("items", [])
        if not items:
            return None

        context_words = _content_words(" ".join(contexts))

        for item in items:
            # ── 1. Year ──────────────────────────────────────────────────────
            issued = item.get("issued", {}).get("date-parts", [[None]])
            item_year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
            if item_year != year[:4]:
                continue

            # ── 2. Author ────────────────────────────────────────────────────
            cr_authors = item.get("author", [])
            if not cr_authors:
                continue
            cr_family = _norm(cr_authors[0].get("family", ""))
            if not cr_family:
                continue

            if author_norm != cr_family:
                min_len = min(len(author_norm), len(cr_family))
                if min_len <= 3 or author_norm[:min_len] != cr_family[:min_len]:
                    continue

            # ── 3. Title/context plausibility ────────────────────────────────
            cr_title = " ".join(item.get("title", []))
            title_words = _content_words(cr_title)

            overlap_inconclusive = False
            if not title_words or not context_words:
                overlap_inconclusive = True
            elif title_words & context_words:
                pass  # overlap confirmed
            else:
                logger.debug(
                    "CrossRef rejected (no title/context overlap): "
                    "%s %s → '%s'",
                    author, year, cr_title[:80],
                )
                continue

            # ── Build record ─────────────────────────────────────────────────
            authors = [
                {"family": a.get("family", ""), "given": a.get("given", "")}
                for a in cr_authors
            ]
            container = " ".join(item.get("container-title", []))
            doi = item.get("DOI", "")

            entry_type = "article"
            if item.get("type") == "book":
                entry_type = "book"
            elif item.get("type") == "book-chapter":
                entry_type = "incollection"

            if not overlap_inconclusive and doi:
                confidence = "high"
            else:
                confidence = "medium"

            record = {
                "author": authors,
                "date": item_year,
                "year": item_year,
                "title": cr_title,
                "entry_type": entry_type,
                "_resolution_method": "crossref",
                "_resolution_confidence": confidence,
            }
            if container:
                if entry_type == "article":
                    record["journaltitle"] = container
                else:
                    record["booktitle"] = container
            if doi:
                record["doi"] = doi
            if item.get("volume"):
                record["volume"] = item["volume"]
            if item.get("page"):
                record["pages"] = item["page"]
            if item.get("publisher"):
                record["publisher"] = item["publisher"]

            return record

    except (requests.Timeout, requests.ConnectionError):
        return None
    except Exception as e:
        logger.debug("CrossRef error for %s %s: %s", author, year, e)
        return None

    return None


# =============================================================================
# Tier 2: LLM metadata generation
# =============================================================================

_LLM_RESOLVE_PROMPT = """You are an expert in academic bibliography. Based on the following citation contexts, infer the full bibliographic metadata for a work by {author} published in {year}.

## Citation contexts where this work is referenced
{contexts}

## Task
Based on these contexts, infer as much bibliographic metadata as you can for this specific work. If you can identify the title, journal/book, and other details from the context, include them. If not, provide what you can.

Respond ONLY with a JSON object:
{{"first_author_family": "...", "first_author_given": "...", "additional_authors": [], "year": "...", "title": "...", "container_title": "...", "volume": "", "pages": "", "entry_type": "article|book|incollection|misc"}}"""


def _try_llm(
    author: str,
    year: str,
    contexts: list[str],
    llm_config: dict,
) -> dict | None:
    """Ask the LLM to infer bibliographic metadata from citation contexts."""
    base_url = llm_config.get("base_url", "http://localhost:11434")
    model = llm_config.get("model", "qwen3.5:35b")
    timeout = llm_config.get("timeout", 120)

    ctx_text = "\n---\n".join(contexts[:3])
    prompt = _LLM_RESOLVE_PROMPT.format(
        author=author, year=year, contexts=ctx_text
    )

    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                "options": {"temperature": 0.2, "num_predict": 512},
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None

        raw = resp.json().get("response", "").strip()
        raw = re.sub(r"<think>[\s\S]*?</think>", "", raw).strip()

        # Parse JSON object
        parsed = None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass

        if not parsed or not isinstance(parsed, dict):
            return None

        family = parsed.get("first_author_family", author)
        given = parsed.get("first_author_given", "")
        title = parsed.get("title", "")

        if not title:
            return None

        authors = [{"family": family, "given": given}]
        for aa in parsed.get("additional_authors", []):
            if isinstance(aa, dict) and aa.get("family"):
                authors.append({"family": aa["family"], "given": aa.get("given", "")})

        record = {
            "author": authors,
            "date": parsed.get("year", year),
            "year": parsed.get("year", year)[:4],
            "title": title,
            "entry_type": parsed.get("entry_type", "misc"),
            "_resolution_method": "llm_from_context",
            "_resolution_confidence": "medium" if title else "low",
        }

        container = parsed.get("container_title", "")
        if container:
            if record["entry_type"] == "article":
                record["journaltitle"] = container
            else:
                record["booktitle"] = container
        vol = parsed.get("volume", "")
        if vol:
            record["volume"] = vol
        pages = parsed.get("pages", "")
        if pages:
            record["pages"] = pages

        return record

    except (requests.Timeout, requests.ConnectionError):
        return None
    except Exception as e:
        logger.debug("LLM resolve error for %s %s: %s", author, year, e)
        return None


# =============================================================================
# Helpers
# =============================================================================

def _norm(s: str) -> str:
    """Normalise author surname — delegates to utils.norm_author."""
    return norm_author(s)


# Common English stopwords — excluded from title/context overlap check.
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "have", "been",
    "were", "their", "they", "about", "which", "into", "through", "some",
    "also", "when", "other", "than", "more", "over", "such", "upon",
    "between", "under", "after", "before", "during", "within", "without",
}


def _content_words(text: str) -> set[str]:
    """
    Extract content words from text for title/context overlap checking.

    Content words are lowercase alphabetic tokens of 4+ characters that
    are not on the stopword list. Short words and stopwords are excluded
    because they appear in any text and would produce false overlap signals.
    """
    tokens = re.findall(r"[a-zA-Z]{4,}", unidecode(text).lower())
    return {t for t in tokens if t not in _STOPWORDS}