"""
bibvik.metadata — Controlled vocabularies, scope notes, and analysis metadata.

Every output JSON file includes a "_metadata" branch that documents:
1. The parameters used for the analysis run.
2. Controlled vocabularies with definitions for every categorical field.
3. Scope notes explaining what each field means and how it was produced.

This makes the output files self-documenting: a reader can understand
every term and category without consulting external documentation.
"""

import time
from typing import Any


# =============================================================================
# Controlled vocabularies with definitions
# =============================================================================

CITATION_FUNCTION_VOCABULARY = {
    "_description": (
        "Citation functions classify how a citing author uses a referenced work. "
        "These categories are not a closed list — the LLM may assign labels "
        "beyond those listed here based on the specific context. The categories "
        "below represent the most common functions described in citation analysis "
        "literature (e.g., Garfield 1965; Moravcsik & Murugesan 1975; "
        "Teufel et al. 2006; Jurgens et al. 2018)."
    ),
    "terms": {
        "evidential_support": {
            "label": "Evidential support",
            "definition": (
                "The cited work provides empirical data, experimental findings, "
                "or other evidence that supports a claim made by the citing author."
            ),
        },
        "methodological_basis": {
            "label": "Methodological basis",
            "definition": (
                "The citing author adopts, adapts, or builds upon a method, "
                "technique, analytical framework, or tool from the cited work."
            ),
        },
        "theoretical_framing": {
            "label": "Theoretical framing",
            "definition": (
                "The cited work provides a theoretical lens, conceptual model, "
                "or interpretive framework that shapes the citing discussion."
            ),
        },
        "background": {
            "label": "Background / context",
            "definition": (
                "The cited work is referenced to establish disciplinary context, "
                "prior knowledge, or the state of the art, without the citing "
                "author engaging deeply with its specific content."
            ),
        },
        "contrast_critique": {
            "label": "Contrast / critique",
            "definition": (
                "The citing author disagrees with, qualifies, problematizes, "
                "or positions their own work in contrast to the cited work."
            ),
        },
        "extension": {
            "label": "Extension / building-upon",
            "definition": (
                "The citing author explicitly extends, refines, or develops "
                "ideas, methods, or findings from the cited work."
            ),
        },
        "example_illustration": {
            "label": "Example / illustration",
            "definition": (
                "The cited work is used as a case study, example, or "
                "illustrative instance of a broader point."
            ),
        },
        "attribution": {
            "label": "Attribution",
            "definition": (
                "A concept, term, dataset, or finding is attributed to the "
                "cited work without further elaboration on its content."
            ),
        },
        "gap_identification": {
            "label": "Gap identification",
            "definition": (
                "The cited work (or the body of literature it represents) is "
                "referenced to identify a research gap, limitation, or "
                "unanswered question that motivates the citing study."
            ),
        },
        "analysis_failed": {
            "label": "Analysis failed",
            "definition": (
                "The LLM did not return a parseable result for this context. "
                "This may indicate a timeout, malformed response, or an "
                "unusually complex citation context."
            ),
        },
    },
}

CHARACTERIZATION_ASSESSMENT_VOCABULARY = {
    "_description": (
        "Characterization assessments evaluate how faithfully the citing author "
        "represents the cited work, by comparing the citation context against "
        "the cited paper's actual content. Only produced in the content-enriched "
        "analysis pass, where we have the cited paper's text."
    ),
    "terms": {
        "faithful": {
            "label": "Faithful",
            "definition": (
                "The citing author's characterization accurately represents "
                "the cited work's content, findings, or arguments."
            ),
        },
        "selective": {
            "label": "Selective",
            "definition": (
                "The citing author highlights certain aspects of the cited "
                "work while omitting others. The characterization is not "
                "inaccurate but is incomplete."
            ),
        },
        "reframing": {
            "label": "Reframing",
            "definition": (
                "The citing author reinterprets or recontextualizes the cited "
                "work in a way that differs from the original's self-presentation. "
                "The cited work's ideas are used, but their emphasis or "
                "implications are shifted."
            ),
        },
        "superficial": {
            "label": "Superficial",
            "definition": (
                "The citation invokes the work without engaging substantively "
                "with its content. The citing author may name-drop the reference "
                "or cite it as part of a list without demonstrating familiarity "
                "with its arguments."
            ),
        },
    },
}

CONFIDENCE_VOCABULARY = {
    "_description": (
        "The LLM's self-assessed confidence in its classification. This is "
        "a heuristic signal, not a calibrated probability."
    ),
    "terms": {
        "high": {
            "label": "High",
            "definition": (
                "The citation context provides clear, unambiguous signals "
                "about the function of the citation."
            ),
        },
        "medium": {
            "label": "Medium",
            "definition": (
                "The citation context is somewhat ambiguous or the function "
                "could be interpreted in multiple ways."
            ),
        },
        "low": {
            "label": "Low",
            "definition": (
                "The citation context provides minimal information about "
                "the function, or the classification is speculative."
            ),
        },
        "none": {
            "label": "None",
            "definition": (
                "No confidence assessment was possible (e.g., the LLM "
                "analysis failed or was skipped)."
            ),
        },
    },
}

CLUSTER_RELATIONSHIP_VOCABULARY = {
    "_description": (
        "Relationship types characterize how a cluster of co-cited references "
        "relate to each other in the way they are used by citing authors. "
        "These go beyond simple co-occurrence frequency to capture the "
        "qualitative nature of the relationship."
    ),
    "terms": {
        "similar_parallel": {
            "label": "Similar / parallel",
            "definition": (
                "The sources are doing similar work or presenting parallel "
                "findings. They are cited together as a body of related "
                "research addressing the same question or phenomenon."
            ),
        },
        "methodological_lineage": {
            "label": "Methodological lineage",
            "definition": (
                "The sources are cited in sequence to trace the evolution "
                "or refinement of a method, technique, or analytical approach."
            ),
        },
        "theoretical_conversation": {
            "label": "Theoretical conversation",
            "definition": (
                "The sources are engaged in a common theoretical debate or "
                "dialogue, whether agreeing, refining, or extending each "
                "other's conceptual contributions."
            ),
        },
        "contrast_foil": {
            "label": "Contrast / foil",
            "definition": (
                "The sources are cited against each other to highlight "
                "differences in approach, findings, interpretation, or "
                "theoretical orientation."
            ),
        },
        "building_upon": {
            "label": "Building upon",
            "definition": (
                "One or more sources explicitly build on, extend, or refine "
                "another. The cluster traces a developmental lineage of ideas."
            ),
        },
        "complementary": {
            "label": "Complementary",
            "definition": (
                "The sources address different facets of the same problem or "
                "phenomenon. They are cited together to provide a more "
                "comprehensive picture than any single source offers."
            ),
        },
        "debate": {
            "label": "Debate",
            "definition": (
                "The sources represent opposing positions or interpretations. "
                "They are cited together to frame a controversy or disagreement "
                "in the literature."
            ),
        },
        "other": {
            "label": "Other",
            "definition": (
                "The relationship does not fit neatly into the above categories. "
                "The rationale field provides a specific explanation."
            ),
        },
        "analysis_failed": {
            "label": "Analysis failed",
            "definition": "The LLM did not return a parseable result for this cluster.",
        },
    },
}

CLUSTER_STRENGTH_VOCABULARY = {
    "_description": (
        "How closely related the cluster members are in terms of how citing "
        "authors use them together."
    ),
    "terms": {
        "strong": {
            "label": "Strong",
            "definition": (
                "The sources are consistently cited together and their "
                "relationship is clearly articulated in the citation contexts."
            ),
        },
        "moderate": {
            "label": "Moderate",
            "definition": (
                "The sources appear together frequently but the nature of "
                "their relationship is not always explicitly stated."
            ),
        },
        "weak": {
            "label": "Weak",
            "definition": (
                "The sources co-occur but the relationship may be incidental "
                "or context-dependent rather than reflecting a deep connection."
            ),
        },
    },
}

CONTENT_ALIGNMENT_VOCABULARY = {
    "_description": (
        "In the content-enriched cluster analysis, this assesses whether "
        "the citing author's grouping of sources reflects genuine intellectual "
        "relationships as evidenced by the cited papers' actual content."
    ),
    "terms": {
        "aligned": {
            "label": "Aligned",
            "definition": (
                "The citing author's grouping accurately reflects the "
                "relationships between the cited works' actual content."
            ),
        },
        "partially_aligned": {
            "label": "Partially aligned",
            "definition": (
                "The grouping reflects some real relationships but misses "
                "or overstates others. The cited works share some common "
                "ground but are not as closely related as the citation "
                "context implies."
            ),
        },
        "misaligned": {
            "label": "Misaligned",
            "definition": (
                "The citing author groups these sources together, but their "
                "actual content suggests they are less related than the "
                "citation context implies, or related in a different way."
            ),
        },
    },
}

GENERATION_VOCABULARY = {
    "_description": (
        "The generation field indicates a reference's distance from the "
        "seed paper in the citation graph."
    ),
    "terms": {
        "P": {
            "label": "Primary / Seed",
            "definition": "The seed paper itself — the origin of the citation graph.",
        },
        "F1": {
            "label": "First generation",
            "definition": (
                "Works cited directly by the seed paper. These are one step "
                "removed from the seed in the citation graph."
            ),
        },
        "F2": {
            "label": "Second generation",
            "definition": (
                "Works cited by F1 papers. These are two steps removed from "
                "the seed. They represent the intellectual context of the "
                "seed paper's references."
            ),
        },
    },
}


# =============================================================================
# Metadata builder
# =============================================================================

def build_bibliography_metadata(config: dict) -> dict:
    """Build the _metadata branch for bibliography.json."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": "BibVik Citation Analysis Toolkit",
        "description": (
            "Biblatex-style bibliography with citation graph metadata. "
            "Each entry represents a scholarly work identified through "
            "multi-generational citation extraction from academic PDFs "
            "using GROBID (machine-learning-based reference parsing)."
        ),
        "parameters": {
            "seed_paper": config.get("seed_paper", ""),
            "f1_pdf_dir": config.get("f1_pdf_dir", ""),
            "grobid_url": config.get("grobid", {}).get("base_url", ""),
            "zotero_csv": config.get("zotero_csv", "not provided"),
        },
        "field_definitions": {
            "citekey": "Unique identifier in lastnameyear format (e.g., smith2020a).",
            "entry_type": "Biblatex entry type: article, book, incollection, inproceedings, misc.",
            "title": "Full title of the work.",
            "author": "List of authors, each with 'family' and 'given' name fields. Unicode preserved.",
            "editor": "List of editors (for edited volumes), same format as author.",
            "date": "Publication date in ISO or partial format.",
            "year": "4-digit publication year extracted from date.",
            "journaltitle": "Full name of the journal (for articles).",
            "booktitle": "Full name of the book (for chapters in edited volumes).",
            "volume": "Volume number.",
            "number": "Issue number.",
            "pages": "Page range in biblatex en-dash format (e.g., 45--67).",
            "publisher": "Publisher name.",
            "location": "Place of publication.",
            "doi": "Digital Object Identifier.",
            "generation": "Distance from seed paper in the citation graph.",
            "cited_by": "List of papers that cite this work, with citation contexts.",
            "_source_pdf": "Filename of the PDF from which this record was extracted.",
            "_grobid_id": "GROBID's internal reference ID (for debugging).",
            "_raw_citation": "Original unparsed citation string (fallback).",
        },
        "vocabularies": {
            "generation": GENERATION_VOCABULARY,
        },
    }


def build_contexts_metadata(config: dict) -> dict:
    """Build the _metadata branch for citation_contexts.json."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": "BibVik Citation Analysis Toolkit",
        "description": (
            "Citation context analysis (unified). Each entry contains the "
            "verbatim text surrounding a citation and the inferred function "
            "of the citation. Where the cited paper's PDF was available, the "
            "analysis is content-enriched: it includes an assessment of how "
            "faithfully the citing author represents the cited work. The "
            "'analysis_mode' field on each context indicates which prompt "
            "was used: 'content_enriched' (cited paper text was available) "
            "or 'context_only' (based solely on citing paper text)."
        ),
        "parameters": {
            "llm_model": config.get("llm", {}).get("model", ""),
            "llm_temperature": config.get("llm", {}).get("temperature", ""),
            "sentence_window": config.get("context", {}).get("sentence_window", ""),
            "boundary_threshold": config.get("context", {}).get("boundary_threshold", ""),
            "context_limit": config.get("context_limit", "none (all contexts)"),
        },
        "field_definitions": {
            "context_id": (
                "Unique identifier for this citation context, formatted as "
                "ctx_{citing_citekey}_{cited_citekey}_{sequence}. Used to "
                "cross-reference contexts across output files."
            ),
            "citing_citekey": "Citekey of the paper containing this citation.",
            "verbatim_text": (
                "The literal text surrounding the citation, extracted from "
                "the citing paper with adaptive windowing. Citation markers "
                "are replaced with readable forms."
            ),
            "citation_function": "Inferred function of this citation (see vocabulary).",
            "citation_function_explanation": "LLM-generated explanation of the classification.",
            "confidence": "LLM's self-assessed confidence in the classification.",
            "co_occurring_citekeys": "Other references cited in the same context passage.",
            "analysis_mode": (
                "Which analysis was performed: 'content_enriched' if the cited "
                "paper's PDF content was available and used, or 'context_only' "
                "if classification was based solely on the citing paper's text."
            ),
            "characterization_assessment": (
                "Only present when analysis_mode is 'content_enriched'. "
                "Assesses how faithfully the citing author represents the cited work."
            ),
            "characterization_explanation": (
                "Only present when analysis_mode is 'content_enriched'. "
                "Explanation of the characterization assessment."
            ),
        },
        "vocabularies": {
            "citation_function": CITATION_FUNCTION_VOCABULARY,
            "confidence": CONFIDENCE_VOCABULARY,
            "characterization_assessment": CHARACTERIZATION_ASSESSMENT_VOCABULARY,
            "analysis_mode": {
                "_description": "Indicates the analysis depth for each citation context.",
                "terms": {
                    "content_enriched": {
                        "label": "Content-enriched",
                        "definition": (
                            "The cited paper's PDF was available and its content "
                            "(abstract + introduction) was included in the LLM prompt. "
                            "The analysis includes a characterization assessment."
                        ),
                    },
                    "context_only": {
                        "label": "Context-only",
                        "definition": (
                            "No PDF content was available for the cited paper. "
                            "The analysis is based solely on the citation context "
                            "in the citing paper."
                        ),
                    },
                },
            },
        },
    }


def build_clusters_metadata(config: dict, enriched: bool = False) -> dict:
    """Build the _metadata branch for cluster analysis JSON files."""
    mode = "content-enriched" if enriched else "context-only"
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": "BibVik Citation Analysis Toolkit",
        "analysis_mode": mode,
        "description": (
            f"Citation cluster analysis ({mode}). Clusters are groups of "
            "references that co-occur in citation contexts and share "
            "meaningful relationships beyond simple co-occurrence frequency."
            + (
                " In content-enriched mode, the analysis also considers "
                "the actual content of the cited papers to assess whether "
                "the citing author's grouping reflects genuine intellectual "
                "relationships."
                if enriched else
                " This analysis is based solely on how references appear "
                "together in the citing paper's text."
            )
        ),
        "parameters": {
            "llm_model": config.get("llm", {}).get("model", ""),
            "llm_temperature": config.get("llm", {}).get("temperature", ""),
            "min_cooccurrence": config.get("clustering", {}).get("min_cooccurrence", ""),
        },
        "field_definitions": {
            "cluster_id": "Unique identifier for this cluster.",
            "members": "List of citekeys belonging to this cluster.",
            "relationship_type": "Category of relationship between cluster members (see vocabulary).",
            "relationship_name": "Descriptive name for this specific cluster's relationship.",
            "rationale": "LLM-generated explanation of why these sources are grouped.",
            "directionality": "Whether the relationship is symmetric or has a direction.",
            "strength": "How closely related the cluster members are (see vocabulary).",
            "relevant_contexts": "Context IDs where cluster members co-occur.",
            "pair_counts": "Pairwise co-occurrence counts within the cluster.",
            **({"content_alignment": "Whether the grouping reflects actual content relationships.",
                "content_alignment_explanation": "Explanation of the content alignment assessment."} if enriched else {}),
        },
        "vocabularies": {
            "relationship_type": CLUSTER_RELATIONSHIP_VOCABULARY,
            "strength": CLUSTER_STRENGTH_VOCABULARY,
            **({"content_alignment": CONTENT_ALIGNMENT_VOCABULARY} if enriched else {}),
        },
    }


def build_coverage_metadata(config: dict) -> dict:
    """Build the _metadata branch for coverage_report.json."""
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tool": "BibVik Citation Analysis Toolkit",
        "description": (
            "Coverage report showing which referenced works have PDFs available "
            "for analysis, and which are missing. Includes open access status "
            "and suggestions for acquiring missing papers."
        ),
        "parameters": {
            "seed_paper": config.get("seed_paper", ""),
            "f1_pdf_dir": config.get("f1_pdf_dir", ""),
        },
    }
