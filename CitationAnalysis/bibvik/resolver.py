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
    """Query CrossRef for a matching work."""
    query = f"{author} {year}"

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

        # Find best match
        author_norm = _norm(author)
        for item in items:
            # Check year
            issued = item.get("issued", {}).get("date-parts", [[None]])
            item_year = str(issued[0][0]) if issued and issued[0] and issued[0][0] else ""
            if item_year != year[:4]:
                continue

            # Check first author
            cr_authors = item.get("author", [])
            if not cr_authors:
                continue
            cr_family = cr_authors[0].get("family", "")
            if _norm(cr_family) != author_norm and not (
                author_norm[:4] in _norm(cr_family) or _norm(cr_family)[:4] in author_norm
            ):
                continue

            # Match found — build record
            authors = [
                {"family": a.get("family", ""), "given": a.get("given", "")}
                for a in cr_authors
            ]
            title = " ".join(item.get("title", []))
            container = " ".join(item.get("container-title", []))
            doi = item.get("DOI", "")

            entry_type = "article"
            if item.get("type") == "book":
                entry_type = "book"
            elif item.get("type") == "book-chapter":
                entry_type = "incollection"

            record = {
                "author": authors,
                "date": item_year,
                "year": item_year,
                "title": title,
                "entry_type": entry_type,
                "_resolution_method": "crossref",
                "_resolution_confidence": "high" if doi else "medium",
            }
            if container:
                if entry_type == "article":
                    record["journaltitle"] = container
                else:
                    record["booktitle"] = container
            if doi:
                record["doi"] = doi
            vol = item.get("volume", "")
            if vol:
                record["volume"] = vol
            pages = item.get("page", "")
            if pages:
                record["pages"] = pages
            publisher = item.get("publisher", "")
            if publisher:
                record["publisher"] = publisher

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
    return re.sub(r"[^a-z]", "", unidecode(s).lower())
