"""
bibvik.cluster_analyzer — Identify and characterize citation relationship clusters.

This module goes beyond simple co-occurrence counting to identify meaningful
clusters of references and characterize the nature of their relationships.

Two analysis passes:
1. **Context-only**: Based solely on how references appear together and are
   described in the citing papers. No knowledge of the cited papers' actual
   content.
2. **Content-enriched**: Incorporates information from the cited papers
   themselves (when PDFs are available), allowing the LLM to assess whether
   co-cited works are truly related or just happen to appear together.

Cluster types we aim to detect:
- **Similar/parallel**: Sources doing similar work, cited together as a body
  of related findings or perspectives.
- **Methodological lineage**: Sources cited in sequence to trace the evolution
  of a method, technique, or approach.
- **Theoretical conversation**: Sources engaged in a common theoretical debate,
  whether agreeing or disagreeing.
- **Contrast/foil**: Sources cited against each other, used to highlight
  differences in approach, findings, or interpretation.
- **Building-upon**: Sources where one explicitly extends or refines another,
  cited together to trace a line of development.
- **Complementary**: Sources that address different facets of the same problem,
  cited together to provide a comprehensive picture.

Approach:
    1. Build a co-occurrence matrix from citation contexts.
    2. Enrich co-occurrence with citation function data (from llm_analyzer).
    3. Identify candidate clusters using co-occurrence + function similarity.
    4. Send each candidate cluster to the LLM for characterization.

Design decision — separate output:
    Cluster analysis is output as a separate JSON file rather than embedded
    in the bibliography. This is because clusters describe relationships
    *between* entries, not properties of individual entries. Embedding them
    would require either awkward cross-references or data duplication. A
    separate file with references back to citekeys and context_ids keeps
    the data normalized.
"""

import logging
from collections import Counter, defaultdict
from itertools import combinations


from .llm_analyzer import LLMAnalyzer, _format_authors, _parse_llm_json

logger = logging.getLogger(__name__)


# =============================================================================
# Cluster analysis prompt
# =============================================================================

CLUSTER_ANALYSIS_PROMPT = """You are an expert in academic citation analysis and bibliometrics. Your task is to analyze a group of references that frequently appear together in academic citations, and characterize the nature of their relationship.

## Co-cited references

The following references frequently appear together in citation contexts:

{reference_list}

## Citation contexts where they co-occur

{contexts_text}

## Citation function data

{function_data}

## Task

Analyze the relationship between these co-cited references. Consider:
1. Are they doing similar work (parallel findings)?
2. Is one building on another (methodological or theoretical lineage)?
3. Are they being used as contrasts or foils for each other?
4. Do they address complementary aspects of a problem?
5. Are they part of a theoretical conversation or debate?
6. Is there another type of relationship?

Respond in JSON format:
{{
  "relationship_type": "<one of: similar_parallel, methodological_lineage, theoretical_conversation, contrast_foil, building_upon, complementary, debate, other>",
  "relationship_name": "<a descriptive 5-15 word name for this specific cluster's relationship>",
  "rationale": "<3-6 sentences explaining the nature of the relationship, with specific reference to the citation contexts>",
  "directionality": "<undirected if symmetric, or describe the direction: e.g., 'A builds on B', 'A contrasts with B'>",
  "strength": "<strong/moderate/weak - how closely related are these sources in how they're used?>"
}}

Respond ONLY with the JSON object. No preamble, no markdown fences."""


CLUSTER_CONTENT_ENRICHED_PROMPT = """You are an expert in academic citation analysis. Your task is to analyze co-cited references, considering both how they are used in the citing paper AND the actual content of the cited works.

## Co-cited references (with summaries of their content)

{reference_list_with_content}

## Citation contexts where they co-occur

{contexts_text}

## Citation function data

{function_data}

## Task

Analyze the relationship between these co-cited references. You have access to both:
(a) How the citing author uses these references together
(b) What the cited works actually contain

Consider whether:
1. The citing author's grouping reflects genuine intellectual relationships
2. The works are more related or less related than the citing context suggests
3. The relationship is one of similarity, lineage, contrast, complementarity, or something else

Respond in JSON format:
{{
  "relationship_type": "<similar_parallel, methodological_lineage, theoretical_conversation, contrast_foil, building_upon, complementary, debate, other>",
  "relationship_name": "<descriptive 5-15 word name>",
  "rationale": "<3-6 sentences explaining the relationship>",
  "content_alignment": "<How well does the citing author's grouping reflect the actual content? aligned/partially_aligned/misaligned>",
  "content_alignment_explanation": "<2-3 sentences>",
  "directionality": "<undirected or directed description>",
  "strength": "<strong/moderate/weak>"
}}

Respond ONLY with the JSON object."""


# =============================================================================
# Main analysis functions
# =============================================================================

def build_cooccurrence_matrix(
    contexts: dict[str, list[dict]],
    min_cooccurrence: int = 2,
) -> dict[tuple[str, str], int]:
    """
    Build a co-occurrence matrix from citation contexts.

    Two references co-occur when they appear in the same citation context
    (i.e., the same paragraph or adapted context window). We count how many
    distinct contexts they share.

    Args:
        contexts:         Dict mapping citekeys to lists of context records.
        min_cooccurrence: Minimum co-occurrence count to include a pair.

    Returns:
        Dict mapping (citekey_a, citekey_b) tuples (alphabetically ordered)
        to their co-occurrence count.
    """
    pair_counts: Counter = Counter()

    # Collect all context IDs and the citekeys that appear in each context.
    # A context groups citekeys by shared context_id.
    context_groups: dict[str, set[str]] = defaultdict(set)

    for cited_citekey, ctx_list in contexts.items():
        for ctx in ctx_list:
            ctx_id = ctx.get("context_id", "")
            if ctx_id:
                # The cited citekey itself appears in this context.
                context_groups[ctx_id].add(cited_citekey)
                # Co-occurring citekeys also appear.
                for co_ck in ctx.get("co_occurring_citekeys", []):
                    context_groups[ctx_id].add(co_ck)

    # Count pairwise co-occurrences.
    for ctx_id, citekeys in context_groups.items():
        for pair in combinations(sorted(citekeys), 2):
            pair_counts[pair] += 1

    # Filter by minimum co-occurrence.
    filtered = {pair: count for pair, count in pair_counts.items() if count >= min_cooccurrence}

    logger.info(
        "Co-occurrence matrix: %d pairs above threshold (min=%d).",
        len(filtered),
        min_cooccurrence,
    )

    return filtered


def identify_clusters(
    cooccurrence: dict[tuple[str, str], int],
    contexts: dict[str, list[dict]],
) -> list[dict]:
    """
    Identify candidate clusters from the co-occurrence matrix.

    Strategy: connected components in the co-occurrence graph. Two nodes
    (citekeys) are connected if they co-occur above the threshold. We then
    group connected components into clusters.

    For large graphs this could produce very large clusters, so we also
    split clusters that exceed 8 members (arbitrary but practical limit for
    LLM analysis) into sub-clusters using highest-co-occurrence heuristics.

    Args:
        cooccurrence: Co-occurrence matrix from build_cooccurrence_matrix.
        contexts:     Citation contexts for retrieving shared context IDs.

    Returns:
        List of candidate cluster dicts, each with:
        - 'members': List of citekeys in the cluster.
        - 'relevant_contexts': List of context IDs where cluster members co-occur.
        - 'pair_counts': Dict of pairwise co-occurrence counts within cluster.
    """
    # --- Build adjacency list ---
    adjacency: dict[str, set[str]] = defaultdict(set)
    for (a, b), count in cooccurrence.items():
        adjacency[a].add(b)
        adjacency[b].add(a)

    # --- Find connected components (simple BFS) ---
    visited = set()
    components = []

    for node in adjacency:
        if node in visited:
            continue
        # BFS from this node.
        component = set()
        queue = [node]
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            component.add(current)
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    queue.append(neighbor)
        if len(component) >= 2:
            components.append(component)

    # --- Build cluster dicts ---
    clusters = []
    for component in components:
        members = sorted(component)

        # If the component is too large, we could split it here.
        # For now, we cap at 8 members per cluster for LLM analysis.
        if len(members) > 8:
            # Split into sub-clusters by taking highest-connected sub-groups.
            # Simple heuristic: sort by total co-occurrence weight and take top 8.
            member_weights = {}
            for m in members:
                weight = sum(
                    cooccurrence.get(tuple(sorted([m, other])), 0)
                    for other in members if other != m
                )
                member_weights[m] = weight
            members = sorted(member_weights, key=member_weights.get, reverse=True)[:8]

        # Find relevant context IDs.
        relevant_contexts = _find_shared_contexts(members, contexts)

        # Pairwise counts within cluster.
        pair_counts = {}
        for pair in combinations(sorted(members), 2):
            if pair in cooccurrence:
                pair_counts[f"{pair[0]}+{pair[1]}"] = cooccurrence[pair]

        clusters.append({
            "members": members,
            "relevant_contexts": relevant_contexts,
            "pair_counts": pair_counts,
        })

    logger.info("Identified %d candidate clusters.", len(clusters))
    return clusters


def analyze_clusters(
    clusters: list[dict],
    contexts: dict[str, list[dict]],
    bibliography: dict[str, dict],
    analyzer: LLMAnalyzer,
    content_enriched: bool = False,
) -> list[dict]:
    """
    Send each candidate cluster to the LLM for relationship characterization.

    Args:
        clusters:         Candidate clusters from identify_clusters.
        contexts:         Citation contexts.
        bibliography:     Full bibliography.
        analyzer:         LLM analyzer instance.
        content_enriched: If True, include cited papers' abstracts in prompts.

    Returns:
        List of analyzed cluster dicts, each with LLM-generated fields:
        - cluster_id
        - relationship_type
        - relationship_name
        - rationale
        - directionality
        - strength
        - members
        - relevant_contexts
        - (if content_enriched): content_alignment, content_alignment_explanation
    """
    analyzed = []
    n_clusters = len(clusters)

    for i, cluster in enumerate(clusters):
        cluster_id = f"cluster_{str(i + 1).zfill(3)}"
        members = cluster["members"]
        logger.debug("  Cluster %d/%d (%d members)", i + 1, n_clusters, len(members))

        # --- Build reference list for prompt ---
        ref_list_parts = []
        ref_list_with_content_parts = []
        for ck in members:
            entry = bibliography.get(ck, {})
            title = entry.get("title", "Unknown")
            authors = _format_authors(entry.get("author", []))
            year = entry.get("year", "n.d.")
            abstract = entry.get("abstract", "")

            ref_list_parts.append(f"- [{ck}] {authors} ({year}). \"{title}\"")

            content_part = f"- [{ck}] {authors} ({year}). \"{title}\""
            if abstract:
                content_part += f"\n  Abstract: {abstract[:500]}{'...' if len(abstract) > 500 else ''}"
            else:
                content_part += "\n  Abstract: Not available."
            ref_list_with_content_parts.append(content_part)

        reference_list = "\n".join(ref_list_parts)
        reference_list_with_content = "\n\n".join(ref_list_with_content_parts)

        # --- Build contexts text for prompt ---
        contexts_text = _build_contexts_text(cluster["relevant_contexts"], contexts, members)

        # --- Build function data for prompt ---
        function_data = _build_function_data(members, contexts)

        # --- Query LLM ---
        if content_enriched:
            prompt = CLUSTER_CONTENT_ENRICHED_PROMPT.format(
                reference_list_with_content=reference_list_with_content,
                contexts_text=contexts_text,
                function_data=function_data,
            )
        else:
            prompt = CLUSTER_ANALYSIS_PROMPT.format(
                reference_list=reference_list,
                contexts_text=contexts_text,
                function_data=function_data,
            )

        result = analyzer._query_llm(prompt)

        analyzed_cluster = {
            "cluster_id": cluster_id,
            "members": members,
            "relevant_contexts": cluster["relevant_contexts"],
            "pair_counts": cluster.get("pair_counts", {}),
        }

        if result:
            analyzed_cluster.update(result)
        else:
            analyzed_cluster["relationship_type"] = "analysis_failed"
            analyzed_cluster["relationship_name"] = "LLM analysis did not return a result"
            analyzed_cluster["rationale"] = ""

        analyzed.append(analyzed_cluster)

    logger.info("Analyzed %d clusters.", len(analyzed))
    return analyzed


# =============================================================================
# Internal helpers
# =============================================================================

def _find_shared_contexts(
    members: list[str],
    contexts: dict[str, list[dict]],
) -> list[str]:
    """
    Find context IDs where at least two cluster members co-occur.
    """
    # Map context_id → set of cluster members present.
    context_members: dict[str, set[str]] = defaultdict(set)

    for ck in members:
        for ctx in contexts.get(ck, []):
            ctx_id = ctx.get("context_id", "")
            if ctx_id:
                context_members[ctx_id].add(ck)
                # Also check co-occurring citekeys.
                for co_ck in ctx.get("co_occurring_citekeys", []):
                    if co_ck in members:
                        context_members[ctx_id].add(co_ck)

    # Keep contexts with 2+ cluster members.
    return [ctx_id for ctx_id, mems in context_members.items() if len(mems) >= 2]


def _build_contexts_text(
    relevant_context_ids: list[str],
    contexts: dict[str, list[dict]],
    members: list[str],
) -> str:
    """
    Build a formatted text block of the relevant citation contexts for the LLM prompt.
    """
    seen_ctx_ids = set()
    parts = []

    for ck in members:
        for ctx in contexts.get(ck, []):
            ctx_id = ctx.get("context_id", "")
            if ctx_id in relevant_context_ids and ctx_id not in seen_ctx_ids:
                seen_ctx_ids.add(ctx_id)
                verbatim = ctx.get("verbatim_text", "")
                citing = ctx.get("citing_citekey", "unknown")
                parts.append(f"[Context {ctx_id}, from {citing}]:\n\"{verbatim}\"\n")

    if not parts:
        return "No shared citation contexts available."

    # Limit to 10 contexts to keep prompt manageable.
    if len(parts) > 10:
        total = len(parts)
        parts = parts[:10]
        parts.append(f"... and {total - 10} more contexts.")

    return "\n".join(parts)


def _build_function_data(
    members: list[str],
    contexts: dict[str, list[dict]],
) -> str:
    """
    Summarize citation function classifications for cluster members.
    """
    parts = []
    for ck in members:
        functions = []
        for ctx in contexts.get(ck, []):
            func = ctx.get("citation_function", "")
            if func and func != "analysis_failed":
                functions.append(func)
        if functions:
            func_summary = ", ".join(set(functions))
            parts.append(f"- [{ck}]: Functions: {func_summary}")

    if not parts:
        return "No citation function data available."

    return "\n".join(parts)