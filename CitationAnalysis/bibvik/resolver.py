"""
bibvik.resolver — Resolve unmatched citations to bibliographic records.

When the detector finds a citation (author, year) that doesn't match any
existing bibliography entry, the resolver attempts to build a record for it
using the LLM.

CrossRef is no longer used for identification here. Author+year queries to
CrossRef are too weak — they return whatever CrossRef finds first, which is
frequently a wrong match (different author with the same surname, unrelated
paper from the same year). CrossRef is used strictly for metadata enrichment
of already-identified entries, via the --enrich flag and bibvik/enricher.py.

See docs/methods/resolver-method.md for the full rationale and design history.

LLM resolution:
    Send citation contexts to the LLM and ask it to infer bibliographic
    metadata. Particularly useful for non-English works and grey literature
    not covered by CrossRef. Requires citation contexts to be available
    (i.e. the citation was detected by regex or LLM body scan, not only
    by GROBID bibliography extraction which stores no contexts).

Resolved entries are tagged with:
    _resolution_method: "llm_from_context" | "llm_from_footnote" | "stub"
    _resolution_confidence: "medium" | "low"
"""

import json
import logging
import re
from typing import Any

import requests
from unidecode import unidecode

from .utils import extract_year, norm_author

logger = logging.getLogger(__name__)


# =============================================================================
# Main resolver
# =============================================================================

def resolve_citations(
    unmatched: dict[tuple, dict],
    email: str = "",
    llm_config: dict | None = None,
) -> list[dict]:
    """
    Attempt to resolve unmatched (author, year) citations to full records
    using the LLM. CrossRef is not used here — see enricher.py.

    Args:
        unmatched:  Dict mapping (norm_author, year) → detection info with
                    'author' (original casing), 'year', 'contexts'.
        email:      Unused — retained for API compatibility.
        llm_config: LLM config. If None or LLM unavailable, entries become stubs.

    Returns:
        List of resolved bibliographic record dicts, each tagged with
        _resolution_method and _resolution_confidence.
    """
    resolved = []

    for key, info in unmatched.items():
        author   = info.get("author", key[0])
        year     = info.get("year", key[1])
        contexts = info.get("contexts", [])

        # LLM resolution — only attempted when contexts are available
        if llm_config and contexts:
            record = _try_llm(author, year, contexts, llm_config)
            if record:
                resolved.append(record)
                continue

        # Stub — preserves the citation relationship even without metadata
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
# Tier 2: LLM metadata generation
# =============================================================================

_LLM_RESOLVE_PROMPT = """You are an expert in academic bibliography. Based on the following citation contexts, infer the full bibliographic metadata for a work by {author} published in {year}.

## Citation contexts where this work is referenced
{contexts}

## Task
Based on these contexts, infer as much bibliographic metadata as you can for this specific work. If you can identify the title, journal/book, and other details from the context, include them. If not, provide what you can.

Respond ONLY with a JSON object:
{{"first_author_family": "...", "first_author_given": "...", "additional_authors": [], "year": "...", "title": "...", "container_title": "...", "volume": "", "pages": "", "entry_type": "article|book|incollection|misc"}}
/no_think"""


def _try_llm(
    author: str,
    year: str,
    contexts: list[str],
    llm_config: dict,
) -> dict | None:
    """Ask the LLM to infer bibliographic metadata from citation contexts."""
    base_url = llm_config.get("base_url", "http://localhost:11434")
    model    = llm_config.get("model", "qwen2.5:7b")
    timeout  = llm_config.get("timeout", 120)
    backend  = llm_config.get("backend", "ollama")

    ctx_text = "\n---\n".join(contexts[:3])
    prompt = _LLM_RESOLVE_PROMPT.format(
        author=author, year=year, contexts=ctx_text
    )

    try:
        if backend == "llama_server":
            resp = requests.post(
                f"{base_url}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "temperature": 0.2,
                    "max_tokens": 512,
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
            "date": parsed.get("year") or year,
            "year": (parsed.get("year") or year)[:4],
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