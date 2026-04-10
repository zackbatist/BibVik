"""
bibvik.footnote_extractor — Extract bibliographic references from footnotes.

Problem:
    Some papers in the corpus (notably Abrams 2012) embed full bibliographic
    information in footnotes rather than a separate bibliography section.
    GROBID's standard reference extraction misses these entirely, inflating
    the unmatched-citation count in the reference audit.

Approach:
    1. Parse GROBID's TEI-XML for <note place="foot"> elements.
    2. For each footnote containing year patterns (likely bibliographic),
       send the text to the LLM and ask it to extract structured metadata.
    3. The LLM returns a list of reference dicts (one per distinct work cited
       in the footnote). A single footnote may contain multiple references.
    4. Each extracted reference is assigned a citekey and a
       '_resolution_method': 'llm_from_footnote' provenance tag.
    5. The extracted references are merged into the bibliography, skipping
       any that appear to duplicate an existing entry (by title similarity).

This module is called from run.py via --footnotes (or implicitly as part
of --audit when footnote-rich papers are detected).

Output:
    Extracted references are written to output/footnote_references.json, which
    contains both the raw footnote texts and the extracted bibliography entries.
    Entries that are new (not already in bibliography.json) are also merged
    directly into the main bibliography.
"""

import logging
import re
from pathlib import Path
from typing import Any

from unidecode import unidecode

from .tei_parser import parse_tei_footnotes
from .llm_analyzer import LLMAnalyzer
from .utils import write_json
from .normalize import normalize_entry

logger = logging.getLogger(__name__)


# =============================================================================
# Main entry point
# =============================================================================

def extract_footnote_references(
    tei_files: dict[str, str],
    bibliography: dict[str, dict],
    analyzer: LLMAnalyzer,
    min_footnote_length: int = 40,
) -> dict[str, Any]:
    """
    Extract bibliographic references from footnotes across a set of TEI-XML files.

    For each paper, parses its footnotes, sends candidates to the LLM, and
    merges results into the bibliography where they are novel entries.

    Args:
        tei_files:           Dict mapping PDF filename → TEI-XML string.
                             (Built from the tei/ output directory.)
        bibliography:        The existing bibliography dict (modified in-place
                             with newly discovered entries).
        analyzer:            An initialized LLMAnalyzer instance.
        min_footnote_length: Minimum character length for a footnote to be
                             sent to the LLM. Shorter notes are almost always
                             pure prose commentary.

    Returns:
        A result dict suitable for writing to footnote_references.json:
        {
            "summary": { ... counts ... },
            "per_paper": {
                "<pdf_name>": {
                    "footnotes_found": N,
                    "footnotes_with_refs": N,
                    "references_extracted": [ ... ],
                    "references_merged": N,
                }
            }
        }
    """
    total_footnotes = 0
    total_refs_extracted = 0
    total_refs_merged = 0

    per_paper: dict[str, dict] = {}

    # Build a reverse lookup: source PDF filename → citekey of that paper.
    # This lets us record cited_by links for newly discovered footnote entries.
    pdf_to_citekey: dict[str, str] = {}
    for citekey, entry in bibliography.items():
        source_pdf = entry.get("_source_pdf", "")
        if source_pdf:
            pdf_to_citekey[source_pdf] = citekey

    n_files = len(tei_files)
    for file_idx, (pdf_name, tei_xml) in enumerate(tei_files.items(), 1):
        logger.info("  [%d/%d] %s", file_idx, n_files, pdf_name[:60])
        logger.info("Processing footnotes for: %s", pdf_name)

        # --- Step 1: Parse footnotes from TEI-XML ---
        footnotes = parse_tei_footnotes(tei_xml)
        total_footnotes += len(footnotes)

        paper_result: dict[str, Any] = {
            "footnotes_found": len(footnotes),
            "footnotes_with_refs": 0,
            "references_extracted": [],
            "references_merged": 0,
        }

        for footnote in footnotes:
            fn_text = footnote["text"]
            fn_id = footnote["note_id"]

            # Only send substantial footnotes to the LLM.
            if len(fn_text) < min_footnote_length:
                logger.debug("Skipping short footnote %s (%d chars)", fn_id, len(fn_text))
                continue

            # Quick pre-filter: must contain at least one 4-digit year.
            if not re.search(r'\b(?:19|20)\d{2}\b', fn_text):
                logger.debug("Skipping footnote %s: no year pattern found", fn_id)
                continue

            # --- Step 2: LLM extraction ---
            logger.debug("Sending footnote %s to LLM (%d chars)", fn_id, len(fn_text))
            extracted = analyzer.extract_references_from_footnote(fn_text)

            if extracted is None:
                logger.warning("LLM failed to process footnote %s in %s", fn_id, pdf_name)
                continue

            if not extracted:
                # Empty list = LLM found no bibliographic references.
                logger.debug("No references found in footnote %s", fn_id)
                continue

            paper_result["footnotes_with_refs"] += 1

            for raw_ref in extracted:
                if not isinstance(raw_ref, dict):
                    continue

                # Clean and validate the extracted reference.
                ref = _clean_extracted_ref(raw_ref, fn_id, fn_text, pdf_name)
                if ref is None:
                    continue

                paper_result["references_extracted"].append(ref)
                total_refs_extracted += 1

                # --- Step 3: Normalize the entry ---
                normalize_entry(ref)

                # --- Step 4: Merge into bibliography if novel ---
                if _is_novel(ref, bibliography):
                    citekey = _assign_citekey(ref, bibliography)
                    ref["citekey"] = citekey

                    # Populate cited_by: this entry was found in a footnote
                    # of the source PDF, so it is cited by that paper.
                    citing_citekey = pdf_to_citekey.get(pdf_name, "")
                    ref["cited_by"] = [citing_citekey] if citing_citekey else []

                    bibliography[citekey] = ref
                    paper_result["references_merged"] += 1
                    total_refs_merged += 1
                    logger.info(
                        "  + New entry from footnote: %s (%s)",
                        citekey, ref.get("title", "")[:60],
                    )
                else:
                    # Entry already exists — still ensure the citing link is recorded.
                    citing_citekey = pdf_to_citekey.get(pdf_name, "")
                    if citing_citekey:
                        for existing_key, existing_entry in bibliography.items():
                            if _titles_match(ref.get("title", ""), existing_entry.get("title", "")):
                                existing_entry.setdefault("cited_by", [])
                                if citing_citekey not in existing_entry["cited_by"]:
                                    existing_entry["cited_by"].append(citing_citekey)
                                break
                    logger.debug(
                        "  ~ Duplicate (already in bibliography): %s",
                        ref.get("title", "")[:60],
                    )

        per_paper[pdf_name] = paper_result

    result = {
        "summary": {
            "papers_processed": len(tei_files),
            "footnotes_found": total_footnotes,
            "references_extracted": total_refs_extracted,
            "references_merged_into_bibliography": total_refs_merged,
        },
        "per_paper": per_paper,
    }

    logger.info(
        "Footnote extraction complete: %d footnotes → %d references extracted, "
        "%d merged into bibliography.",
        total_footnotes, total_refs_extracted, total_refs_merged,
    )
    return result


# =============================================================================
# Helpers
# =============================================================================

def _clean_extracted_ref(
    raw: dict,
    footnote_id: str,
    footnote_text: str,
    source_pdf: str,
) -> dict | None:
    """
    Validate and normalize a single reference dict returned by the LLM.

    Adds provenance fields and applies basic sanity checks. Returns None
    if the reference is too incomplete to be useful.
    """
    ref: dict[str, Any] = {}

    # --- Required fields ---
    title = raw.get("title", "").strip()
    if not title or len(title) < 5:
        logger.debug("Skipping ref with no/trivial title from footnote %s", footnote_id)
        return None

    ref["title"] = title

    # --- Authors ---
    authors = raw.get("author", [])
    if isinstance(authors, list):
        cleaned_authors = []
        for a in authors:
            if isinstance(a, dict):
                family = a.get("family", "").strip()
                given = a.get("given", "").strip()
                if family or given:
                    cleaned_authors.append({"family": family, "given": given})
            elif isinstance(a, str) and a.strip():
                # LLM sometimes returns strings instead of dicts.
                parts = a.strip().rsplit(" ", 1)
                if len(parts) == 2:
                    cleaned_authors.append({"family": parts[1], "given": parts[0]})
                else:
                    cleaned_authors.append({"family": a.strip(), "given": ""})
        ref["author"] = cleaned_authors
    else:
        ref["author"] = []

    # --- Year / date ---
    date = str(raw.get("date", "")).strip()
    if not date:
        # Try to extract from the raw_text or title vicinity.
        year_match = re.search(r'\b((?:19|20)\d{2})\b', raw.get("raw_text", ""))
        if year_match:
            date = year_match.group(1)
    if date:
        ref["date"] = date

    # Minimum viability: must have at least author or year alongside the title.
    if not ref.get("author") and not ref.get("date"):
        logger.debug("Skipping ref with title only (no author/year): %s", title[:60])
        return None

    # --- Optional fields: pass through if present and non-empty ---
    for field in [
        "journaltitle", "booktitle", "volume", "number", "pages",
        "publisher", "location", "series", "doi", "url", "editor",
    ]:
        val = raw.get(field)
        if val:
            if isinstance(val, list):
                # editor field: same cleaning as author
                cleaned = []
                for item in val:
                    if isinstance(item, dict):
                        family = item.get("family", "").strip()
                        given = item.get("given", "").strip()
                        if family or given:
                            cleaned.append({"family": family, "given": given})
                if cleaned:
                    ref[field] = cleaned
            elif isinstance(val, str) and val.strip():
                ref[field] = val.strip()

    # --- Entry type ---
    entry_type = raw.get("entry_type", "misc")
    if entry_type not in ("article", "incollection", "book", "misc"):
        # Infer from available fields if LLM returned something unexpected.
        if ref.get("journaltitle"):
            entry_type = "article"
        elif ref.get("booktitle"):
            entry_type = "incollection"
        elif not ref.get("journaltitle") and not ref.get("booktitle"):
            entry_type = "book" if ref.get("publisher") else "misc"
    ref["entry_type"] = entry_type

    # --- Provenance ---
    ref["_resolution_method"] = "llm_from_footnote"
    ref["_source_footnote"] = footnote_id
    ref["_source_pdf"] = source_pdf
    if raw.get("raw_text"):
        ref["_footnote_raw_text"] = raw["raw_text"].strip()

    return ref


def _titles_match(title_a: str, title_b: str) -> bool:
    """
    Check whether two titles refer to the same work using the same
    normalization logic as _is_novel.
    """
    if not title_a or not title_b:
        return False
    a = _normalize_title(title_a)
    b = _normalize_title(title_b)
    if len(a) < 10 or len(b) < 10:
        return False
    if a == b:
        return True
    shorter, longer = sorted([a, b], key=len)
    if shorter in longer and len(shorter) / len(longer) > 0.5:
        return True
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if len(a_tokens) >= 4 and len(b_tokens) >= 4:
        overlap = len(a_tokens & b_tokens) / max(len(a_tokens), len(b_tokens))
        if overlap >= 0.85:
            return True
    return False


def _normalize_title(title: str) -> str:
    """Normalize a title for duplicate detection."""
    t = unidecode(title).lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _is_novel(ref: dict, bibliography: dict[str, dict]) -> bool:
    """
    Check whether a reference appears to be genuinely new (not already in
    the bibliography). Uses title similarity as the primary signal.

    We use a simple normalized-string equality check rather than fuzzy
    matching, to avoid false negatives (incorrectly classifying a genuinely
    new work as a duplicate). A short title overlap threshold of 0.85 is used
    for secondary checks.

    Returns True if the reference appears to be new.
    """
    if not ref.get("title"):
        return False

    candidate_title = _normalize_title(ref["title"])
    if len(candidate_title) < 10:
        # Too short to be a reliable key.
        return True

    for entry in bibliography.values():
        existing_title = _normalize_title(entry.get("title", ""))
        if not existing_title:
            continue

        # Exact match after normalization.
        if candidate_title == existing_title:
            return False

        # Substring match for titles where one is a truncated/shortened version.
        # e.g. "Early Christian Grave Monuments" is a subset of the full title
        # "Early Christian Grave Monuments and the 11th-Century Context".
        # We require the shorter title to be at least 50% the length of the
        # longer one (to avoid matching on a very short common substring).
        if len(candidate_title) > 20 and len(existing_title) > 20:
            shorter, longer = sorted([candidate_title, existing_title], key=len)
            if shorter in longer and len(shorter) / len(longer) > 0.5:
                return False

        # Token overlap.
        cand_tokens = set(candidate_title.split())
        exist_tokens = set(existing_title.split())
        if len(cand_tokens) >= 4 and len(exist_tokens) >= 4:
            overlap = len(cand_tokens & exist_tokens) / max(len(cand_tokens), len(exist_tokens))
            if overlap >= 0.85:
                return False

    return True


def _assign_citekey(ref: dict, bibliography: dict[str, dict]) -> str:
    """
    Generate a unique citekey for a footnote-extracted reference.

    Delegates to utils.generate_citekey() so that the shared _citekey_registry
    is used for disambiguation. This guarantees no collisions with citekeys
    already assigned during the main extraction pipeline, and ensures that
    two footnote entries that would otherwise share a base key (e.g., two
    works by the same author in the same year) get distinct a/b/c suffixes.

    As a secondary guard, if the generated key is somehow already present in
    the bibliography dict (e.g., from a prior --footnotes run), we append
    additional suffixes until it's unique.
    """
    from .utils import generate_citekey

    authors = ref.get("author", [])
    date = ref.get("date", "")
    year = date[:4] if date else None

    citekey = generate_citekey(authors, year)

    # Secondary guard: if the key already exists in the bibliography dict
    # (e.g., from a previous --footnotes run that wasn't cleared), keep
    # incrementing until we find a free slot. generate_citekey handles the
    # registry internally, so we only need to guard against the dict.
    if citekey in bibliography:
        base = citekey.rstrip("abcdefghijklmnopqrstuvwxyz")
        for suffix in "abcdefghijklmnopqrstuvwxyz":
            candidate = base + suffix
            if candidate not in bibliography:
                citekey = candidate
                break
        else:
            for i in range(2, 1000):
                candidate = f"{base}_{i}"
                if candidate not in bibliography:
                    citekey = candidate
                    break

    return citekey


def load_tei_files(tei_dir: Path) -> dict[str, str]:
    """
    Load all TEI-XML files from a directory into a dict keyed by PDF name.

    The TEI files are named after the PDF they were extracted from,
    e.g. "Abrams 2012 - Diaspora.tei.xml" corresponds to the PDF
    "Abrams 2012 - Diaspora.pdf".

    Args:
        tei_dir: Path to the directory containing .tei.xml files.

    Returns:
        Dict mapping inferred PDF name → TEI-XML string.
    """
    tei_files: dict[str, str] = {}
    for tei_path in sorted(tei_dir.glob("*.tei.xml")):
        # Infer the PDF name: strip the .tei.xml suffix.
        pdf_name = tei_path.name.replace(".tei.xml", ".pdf")
        try:
            tei_files[pdf_name] = tei_path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Could not read TEI file %s: %s", tei_path, e)
    logger.info("Loaded %d TEI-XML files from %s", len(tei_files), tei_dir)
    return tei_files
