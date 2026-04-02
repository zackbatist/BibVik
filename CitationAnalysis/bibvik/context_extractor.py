"""
bibvik.context_extractor — Extract verbatim citation contexts from body text.

For each citation in a paper's body text, this module extracts the surrounding
text that provides context for why the reference was cited. This is the raw
material for understanding citation functions and relationships.

Context window strategy:
    Academic citation contexts are not uniform. A citation at the start of a
    paragraph may be contextualized by the sentences that follow it. A citation
    at the end of a paragraph may be explained by the preceding sentences or
    even the next paragraph. Our approach is adaptive:

    1. Start with the enclosing paragraph as the base context.
    2. If the citation is near a paragraph boundary (within `boundary_threshold`
       characters of the start or end), extend into the adjacent paragraph.
    3. If the enclosing paragraph is very long, trim to ±`sentence_window`
       sentences around the citation marker.

    The goal is to capture enough text to understand the citation's role without
    including irrelevant material.

Citation context IDs:
    Each context gets a unique ID formatted as:
        ctx_{citing_paper_citekey}_{cited_citekey}_{sequence_number}

    These IDs are used to cross-reference contexts across the citation_contexts
    and cluster analysis outputs, allowing you to trace co-occurrence relationships
    back to specific text passages.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Regex to find our {{CITE:id}} placeholders in paragraph text.
CITE_PLACEHOLDER_RE = re.compile(r"\{\{CITE:(\w+)\}\}")


def extract_all_contexts(
    processed_papers: dict[str, dict],
    grobid_map: dict[tuple[str, str], str],
    bibliography: dict[str, dict],
    sentence_window: int = 3,
    boundary_threshold: int = 150,
) -> dict[str, list[dict]]:
    """
    Extract citation contexts from all processed papers.

    For each paper, we walk through its paragraphs, find citation placeholders,
    and build context records. These records are then attached to the cited
    entry's 'cited_by' list in the bibliography.

    Args:
        processed_papers: Dict mapping PDF filenames to processing results
                          (from PDFProcessor.process()).
        grobid_map:       Mapping from (source_pdf, grobid_id) → citekey.
        bibliography:     The full bibliography dict (modified in place to
                          add cited_by entries).
        sentence_window:  Number of sentences to include around citation when
                          paragraph is very long.
        boundary_threshold: Character distance from paragraph edge that triggers
                           context extension into adjacent paragraph.

    Returns:
        Dict mapping citekeys to lists of context records. Each context record
        contains:
        - context_id: Unique identifier for this context
        - citing_citekey: Citekey of the paper that contains this citation
        - verbatim_text: The extracted context text (with citation markers
                         replaced by readable forms)
        - citation_marker: The original marker text (e.g., "(Smith 2020)")
        - co_occurring_citekeys: List of other citekeys cited in the same context
        - paragraph_index: Index of the source paragraph in the body text
    """
    all_contexts: dict[str, list[dict]] = {}

    for source_pdf, paper_data in processed_papers.items():
        paragraphs = paper_data.get("paragraphs", [])
        gid_to_citekey = paper_data.get("grobid_id_to_citekey", {})

        # Determine the citing paper's citekey.
        # We look it up from the bibliography by matching source_pdf.
        citing_citekey = _find_citing_citekey(source_pdf, bibliography)

        if not citing_citekey:
            logger.warning(
                "Cannot determine citekey for %s. Contexts will use filename.",
                source_pdf,
            )
            citing_citekey = source_pdf.replace(".pdf", "")

        logger.info(
            "Extracting contexts from %s (%d paragraphs).",
            source_pdf,
            len(paragraphs),
        )

        # --- Context counter per (citing, cited) pair for unique IDs ---
        context_counters: dict[str, int] = {}

        for para_idx, para in enumerate(paragraphs):
            text = para["text"]
            citations_in_para = para.get("citations", [])

            if not citations_in_para:
                continue

            # --- Find all cited citekeys in this paragraph ---
            # We use both the structured citations list AND regex on the text,
            # because GROBID sometimes misses linking some markers.
            cited_in_para = set()
            for cite in citations_in_para:
                gid = cite.get("grobid_id", "")
                if gid and gid in gid_to_citekey:
                    cited_in_para.add(gid_to_citekey[gid])
                elif gid and (source_pdf, gid) in grobid_map:
                    cited_in_para.add(grobid_map[(source_pdf, gid)])

            # Also check placeholder regex for any GROBID missed.
            for match in CITE_PLACEHOLDER_RE.finditer(text):
                gid = match.group(1)
                if gid in gid_to_citekey:
                    cited_in_para.add(gid_to_citekey[gid])

            if not cited_in_para:
                continue

            # --- Build the context text ---
            # Replace {{CITE:id}} placeholders with readable citation markers.
            readable_text = _make_readable(text, gid_to_citekey, citations_in_para)

            # --- Adaptive windowing ---
            context_text = _build_context_window(
                paragraphs,
                para_idx,
                readable_text,
                citations_in_para,
                sentence_window,
                boundary_threshold,
                gid_to_citekey,
            )

            # --- Build context records for each cited work ---
            co_occurring = list(cited_in_para)

            for cited_citekey in cited_in_para:
                # Generate unique context ID.
                counter_key = cited_citekey
                context_counters.setdefault(counter_key, 0)
                context_counters[counter_key] += 1
                seq = str(context_counters[counter_key]).zfill(3)
                context_id = f"ctx_{citing_citekey}_{cited_citekey}_{seq}"

                # Co-occurring citekeys (excluding self).
                co_occurring_others = [ck for ck in co_occurring if ck != cited_citekey]

                context_record = {
                    "context_id": context_id,
                    "citing_citekey": citing_citekey,
                    "verbatim_text": _clean_leftover_placeholders(context_text),
                    "citation_marker": _find_marker_text(
                        cited_citekey, citations_in_para, gid_to_citekey
                    ),
                    "co_occurring_citekeys": co_occurring_others,
                    "paragraph_index": para_idx,
                }

                # Store in the all_contexts dict.
                all_contexts.setdefault(cited_citekey, [])
                all_contexts[cited_citekey].append(context_record)

                # Also attach to the bibliography entry's cited_by list.
                if cited_citekey in bibliography:
                    bib_entry = bibliography[cited_citekey]
                    bib_entry.setdefault("cited_by", [])

                    # Find or create the cited_by entry for this citing paper.
                    citing_entry = None
                    for cb in bib_entry["cited_by"]:
                        if cb.get("citekey") == citing_citekey:
                            citing_entry = cb
                            break

                    if citing_entry is None:
                        # Determine the generation of this citation link.
                        citing_gen = bibliography.get(citing_citekey, {}).get("generation", "")
                        cited_gen = bib_entry.get("generation", "")
                        citing_entry = {
                            "citekey": citing_citekey,
                            "generation": cited_gen,
                            "contexts": [],
                        }
                        bib_entry["cited_by"].append(citing_entry)

                    citing_entry["contexts"].append(context_record)

    # Log summary.
    total_contexts = sum(len(v) for v in all_contexts.values())
    logger.info(
        "Extracted %d citation contexts across %d cited works.",
        total_contexts,
        len(all_contexts),
    )

    return all_contexts


# =============================================================================
# Internal helpers
# =============================================================================

def _find_citing_citekey(source_pdf: str, bibliography: dict) -> str | None:
    """
    Find the citekey of a paper given its source PDF filename.

    Searches the bibliography for entries whose _source_pdf matches.
    """
    for key, entry in bibliography.items():
        if entry.get("_source_pdf") == source_pdf:
            return key
    return None


def _make_readable(
    text: str,
    gid_to_citekey: dict[str, str],
    citations: list[dict],
) -> str:
    """
    Replace {{CITE:id}} placeholders with human-readable citation markers.

    If the original marker text is available (e.g., "(Smith 2020)"), use that.
    Otherwise, use the citekey in brackets (e.g., "[smith2020]").
    """
    # Build a map from GROBID ID to the best available marker text.
    gid_to_marker = {}
    for cite in citations:
        gid = cite.get("grobid_id", "")
        marker = cite.get("marker_text", "")
        if gid and marker:
            gid_to_marker[gid] = marker

    def replace_cite(match):
        gid = match.group(1)
        if gid in gid_to_marker:
            return gid_to_marker[gid]
        elif gid in gid_to_citekey:
            return f"[{gid_to_citekey[gid]}]"
        else:
            return f"[ref:{gid}]"

    return CITE_PLACEHOLDER_RE.sub(replace_cite, text)


# Also catch malformed/empty placeholders that slip through (e.g., {{CITE:}}).
_LEFTOVER_CITE_RE = re.compile(r"\{\{CITE:?\w*\}\}")


def _clean_leftover_placeholders(text: str) -> str:
    """
    Remove any remaining {{CITE:...}} placeholders that weren't resolved.

    This is a safety net — ideally all placeholders are resolved by
    _make_readable, but GROBID sometimes produces refs with empty or
    unrecognized target IDs.
    """
    text = _LEFTOVER_CITE_RE.sub("", text)
    # Clean up double spaces left behind.
    text = re.sub(r"  +", " ", text)
    return text.strip()


def _build_context_window(
    paragraphs: list[dict],
    para_idx: int,
    readable_text: str,
    citations: list[dict],
    sentence_window: int,
    boundary_threshold: int,
    gid_to_citekey: dict[str, str],
) -> str:
    """
    Build an adaptive context window around citation(s) in a paragraph.

    Rules:
    1. If the paragraph is ≤ 1000 characters, use the whole paragraph.
    2. If the paragraph is long, trim to ±sentence_window sentences around
       the first citation marker.
    3. If any citation is within boundary_threshold characters of the
       paragraph start, prepend the previous paragraph's last 2 sentences.
    4. If any citation is within boundary_threshold characters of the
       paragraph end, append the next paragraph's first 2 sentences.
    """
    context_parts = []

    # --- Check if we need to extend backward ---
    first_cite_offset = 0
    if citations:
        first_cite_offset = citations[0].get("char_offset", 0)

    if first_cite_offset < boundary_threshold and para_idx > 0:
        prev_text = _make_readable(
            paragraphs[para_idx - 1]["text"],
            gid_to_citekey,
            paragraphs[para_idx - 1].get("citations", []),
        )
        # Take last 2 sentences of previous paragraph.
        prev_sentences = _split_sentences(prev_text)
        if prev_sentences:
            context_parts.append(" ".join(prev_sentences[-2:]))
            context_parts.append(" [...] ")

    # --- Main paragraph ---
    if len(readable_text) > 1000:
        # Long paragraph: trim to sentence window around citation.
        sentences = _split_sentences(readable_text)
        # Find the sentence containing the first citation.
        cite_sentence_idx = 0
        char_count = 0
        for i, sent in enumerate(sentences):
            char_count += len(sent)
            if char_count >= first_cite_offset:
                cite_sentence_idx = i
                break

        start = max(0, cite_sentence_idx - sentence_window)
        end = min(len(sentences), cite_sentence_idx + sentence_window + 1)

        trimmed = " ".join(sentences[start:end])
        if start > 0:
            trimmed = "[...] " + trimmed
        if end < len(sentences):
            trimmed = trimmed + " [...]"
        context_parts.append(trimmed)
    else:
        context_parts.append(readable_text)

    # --- Check if we need to extend forward ---
    last_cite_offset = first_cite_offset
    if citations:
        last_cite_offset = citations[-1].get("char_offset", first_cite_offset)

    text_length = len(readable_text)
    if (text_length - last_cite_offset) < boundary_threshold and para_idx < len(paragraphs) - 1:
        next_text = _make_readable(
            paragraphs[para_idx + 1]["text"],
            gid_to_citekey,
            paragraphs[para_idx + 1].get("citations", []),
        )
        # Take first 2 sentences of next paragraph.
        next_sentences = _split_sentences(next_text)
        if next_sentences:
            context_parts.append(" [...] ")
            context_parts.append(" ".join(next_sentences[:2]))

    return "".join(context_parts).strip()


def _split_sentences(text: str) -> list[str]:
    """
    Split text into sentences using a simple regex heuristic.

    This is intentionally simple — we don't need perfect sentence splitting,
    just reasonable boundaries for context windowing. It handles common
    abbreviations (Dr., Fig., etc.) gracefully enough for this purpose.
    """
    # Split on period/question/exclamation followed by space and uppercase letter.
    sentences = re.split(r'(?<=[.!?])\s+(?=[A-Z])', text)
    return [s.strip() for s in sentences if s.strip()]


def _find_marker_text(
    citekey: str,
    citations: list[dict],
    gid_to_citekey: dict[str, str],
) -> str:
    """
    Find the original citation marker text for a given citekey.
    """
    for cite in citations:
        gid = cite.get("grobid_id", "")
        if gid in gid_to_citekey and gid_to_citekey[gid] == citekey:
            return cite.get("marker_text", "")
    return ""
