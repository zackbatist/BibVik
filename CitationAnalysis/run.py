#!/usr/bin/env python3
"""
BibVik Citation Analysis — Main entry point.

This script orchestrates the full citation analysis pipeline:

    Stage 1 (--extract):     Extract references from the seed paper.
    Stage 2 (--iterate-f1):  Extract references from all F1 PDFs.
    Stage 3 (--contexts):    Extract citation contexts and analyze with LLM.
    Stage 4 (--cluster):     Run cluster analysis on citation relationships.
    All stages (--all):      Run stages 1–4 sequentially.

Each stage builds on the output of the previous one. You can run them
individually if you want to inspect intermediate results, or use --all
for a complete run.

Usage:
    python run.py --all
    python run.py --extract
    python run.py --extract --iterate-f1
    python run.py --contexts    # requires stages 1-2 to have been run first
    python run.py --cluster     # requires stages 1-3 to have been run first
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Ensure the bibvik package is importable regardless of the working directory.
# This inserts the directory containing run.py (i.e., the project root) onto
# sys.path so that `import bibvik` resolves to the local package, even when
# the script is invoked from outside the project folder.
# ---------------------------------------------------------------------------
_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from bibvik.utils import load_config, setup_logging, write_json, read_json, reset_citekey_registry
from bibvik.grobid_client import GrobidClient
from bibvik.pdf_processor import PDFProcessor
from bibvik.citation_graph import CitationGraph
from bibvik.context_extractor import extract_all_contexts
from bibvik.llm_analyzer import LLMAnalyzer, analyze_all_contexts
from bibvik.cluster_analyzer import (
    build_cooccurrence_matrix,
    identify_clusters,
    analyze_clusters,
)
from bibvik.metadata import (
    build_bibliography_metadata,
    build_contexts_metadata,
    build_clusters_metadata,
)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="BibVik Citation Analysis Toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py --all                              # Full pipeline
  python run.py --all --limit 5                    # Full pipeline, only 5 F1 papers
  python run.py --extract                          # Seed paper only
  python run.py --extract --iterate-f1             # Seed + F1 papers
  python run.py --contexts --context-limit 20      # Context analysis (test run)
  python run.py --cluster                          # Cluster analysis
  python run.py --coverage --email you@uni.edu     # Coverage report + OA lookup
        """,
    )

    # --- Stage selection ---
    parser.add_argument(
        "--all", action="store_true",
        help="Run the full pipeline (stages 1-4).",
    )
    parser.add_argument(
        "--extract", action="store_true",
        help="Stage 1: Extract references from the seed paper.",
    )
    parser.add_argument(
        "--iterate-f1", action="store_true",
        help="Stage 2: Extract references from all F1 PDFs.",
    )
    parser.add_argument(
        "--contexts", action="store_true",
        help="Stage 3: Extract and analyze citation contexts.",
    )
    parser.add_argument(
        "--cluster", action="store_true",
        help="Stage 4: Run cluster analysis.",
    )
    parser.add_argument(
        "--coverage", action="store_true",
        help="Stage 5: Generate coverage report and check OA availability.",
    )
    parser.add_argument(
        "--audit", action="store_true",
        help="Run reference audit: detect in-text citations independently and "
             "compare against GROBID's extracted bibliography. Reports missing "
             "references with hints for manual recovery. Requires save_tei_xml: true "
             "in config, or must be run together with --extract / --iterate-f1 / --all.",
    )

    # --- Overrides ---
    parser.add_argument(
        "--config", type=str, default="config.yaml",
        help="Path to config file (default: config.yaml).",
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help="Override output directory.",
    )
    parser.add_argument(
        "--seed", type=str, default=None,
        help="Override seed paper path.",
    )
    parser.add_argument(
        "--f1-dir", type=str, default=None,
        help="Override F1 PDF directory.",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Limit the number of F1 PDFs to process. Useful for testing "
             "(e.g., --limit 5 to process only 5 F1 papers).",
    )
    parser.add_argument(
        "--context-limit", type=int, default=None,
        help="Limit the number of citation contexts to analyze with the LLM. "
             "Useful for testing (e.g., --context-limit 20). Contexts are "
             "still extracted for all citations; only LLM analysis is capped.",
    )
    parser.add_argument(
        "--email", type=str, default=None,
        help="Email address for Unpaywall API (required for --coverage OA lookups). "
             "Unpaywall uses this for polite rate-limit tracking, not spam.",
    )
    parser.add_argument(
        "--download-oa", action="store_true",
        help="Download available open access PDFs (used with --coverage).",
    )

    args = parser.parse_args()

    # If no stage is selected, show help.
    if not any([args.all, args.extract, args.iterate_f1, args.contexts, args.cluster, args.coverage, args.audit]):
        parser.print_help()
        sys.exit(1)

    return args


def main():
    """Main pipeline orchestrator."""
    args = parse_args()

    # --- Load configuration ---
    config = load_config(args.config)

    # Apply CLI overrides.
    if args.output_dir:
        config["output_dir"] = args.output_dir
    if args.seed:
        config["seed_paper"] = args.seed
    if args.f1_dir:
        config["f1_pdf_dir"] = args.f1_dir
    if args.verbose:
        config["log_level"] = "DEBUG"
    if args.limit is not None:
        config["limit"] = args.limit
    if args.context_limit is not None:
        config["context_limit"] = args.context_limit
    if args.audit:
        # The audit needs TEI-XML files. Enable saving them automatically.
        config["save_tei_xml"] = True

    # --- Setup logging ---
    log = setup_logging(config["log_level"])
    log.info("BibVik Citation Analysis starting.")
    log.info("Config: seed=%s, f1_dir=%s, output=%s",
             config["seed_paper"], config["f1_pdf_dir"], config["output_dir"])

    # --- Create output directory ---
    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    # --- Processing log (tracks what was done, any errors) ---
    processing_log = {
        "start_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "config": {k: str(v) for k, v in config.items() if k not in ("grobid", "llm", "context", "clustering")},
        "stages": {},
    }

    # Determine which stages to run.
    run_extract = args.all or args.extract
    run_f1 = args.all or args.iterate_f1
    run_contexts = args.all or args.contexts
    run_cluster = args.all or args.cluster
    run_coverage = args.all or args.coverage
    run_audit = args.audit  # Not included in --all; must be explicitly requested.

    # =========================================================================
    # Stage 1: Extract references from seed paper
    # =========================================================================
    bibliography_path = output_dir / "bibliography.json"
    graph_state_path = output_dir / "_graph_state.json"  # Internal state for stage continuity.

    if run_extract:
        log.info("=" * 60)
        log.info("STAGE 1: Extracting references from seed paper")
        log.info("=" * 60)

        # --- Initialize GROBID client ---
        grobid = GrobidClient(
            base_url=config["grobid"]["base_url"],
            timeout=config["grobid"]["timeout"],
        )
        if not grobid.is_alive():
            log.error("GROBID is not available. Please start it first.")
            log.error("  docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1")
            sys.exit(1)

        # --- Initialize processor and graph ---
        processor = PDFProcessor(
            grobid=grobid,
            save_tei=config.get("save_tei_xml", False),
            tei_dir=output_dir / "tei",
        )

        # Load optional Zotero CSV mapping for exact PDF↔citekey matching.
        zotero_map = None
        zotero_csv_path = config.get("zotero_csv")
        if zotero_csv_path:
            from bibvik.zotero_csv import parse_zotero_csv
            zotero_map = parse_zotero_csv(zotero_csv_path)

        graph = CitationGraph(processor, zotero_map=zotero_map)

        # --- Process seed paper ---
        seed_path = Path(config["seed_paper"])
        if not seed_path.exists():
            log.error("Seed paper not found: %s", seed_path)
            sys.exit(1)

        result = graph.process_seed_paper(seed_path)
        if result is None:
            log.error("Failed to process seed paper. Aborting.")
            sys.exit(1)

        # Save intermediate bibliography with metadata.
        write_json(
            {"_metadata": build_bibliography_metadata(config), "entries": graph.get_bibliography()},
            bibliography_path,
        )
        log.info("Bibliography saved: %s (%d entries)", bibliography_path, len(graph.get_bibliography()))

        # Save graph state for stage continuity.
        # We can't pickle the graph object directly, so we save its data.
        _save_graph_state(graph, graph_state_path)

        processing_log["stages"]["extract"] = {
            "status": "success",
            "seed_paper": str(seed_path),
            "references_found": len(result["references"]),
            "paragraphs_found": len(result["paragraphs"]),
        }

    # =========================================================================
    # Stage 2: Extract references from F1 PDFs
    # =========================================================================
    if run_f1:
        log.info("=" * 60)
        log.info("STAGE 2: Extracting references from F1 papers")
        log.info("=" * 60)

        # Load graph state if we didn't just run stage 1.
        if not run_extract:
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error(
                    "Cannot load state from stage 1. Run --extract first, or use --all."
                )
                sys.exit(1)

        # --- Process F1 papers ---
        f1_results = graph.process_f1_papers(
            f1_dir=config["f1_pdf_dir"],
            seed_pdf_path=config["seed_paper"],
            limit=config.get("limit"),
        )

        # Update bibliography with metadata.
        write_json(
            {"_metadata": build_bibliography_metadata(config), "entries": graph.get_bibliography()},
            bibliography_path,
        )
        log.info("Bibliography updated: %d entries", len(graph.get_bibliography()))

        # Save updated graph state.
        _save_graph_state(graph, graph_state_path)

        processing_log["stages"]["iterate_f1"] = {
            "status": "success",
            "pdfs_processed": sum(f1_results.values()),
            "pdfs_failed": sum(1 for v in f1_results.values() if not v),
            "total_bibliography_entries": len(graph.get_bibliography()),
            "per_pdf": {k: "success" if v else "failed" for k, v in f1_results.items()},
        }

    # =========================================================================
    # Stage 3: Citation context extraction and LLM analysis
    # =========================================================================
    contexts_path = output_dir / "citation_contexts.json"

    if run_contexts:
        log.info("=" * 60)
        log.info("STAGE 3: Extracting and analyzing citation contexts")
        log.info("=" * 60)

        # Load state if needed.
        if not (run_extract or run_f1):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run earlier stages first.")
                sys.exit(1)

        bibliography = graph.get_bibliography()
        processed_papers = graph.get_processed_papers()
        grobid_map = graph.get_grobid_map()

        # --- Extract contexts ---
        contexts = extract_all_contexts(
            processed_papers=processed_papers,
            grobid_map=grobid_map,
            bibliography=bibliography,
            sentence_window=config["context"]["sentence_window"],
            boundary_threshold=config["context"]["boundary_threshold"],
        )

        # --- LLM analysis ---
        llm_config = config["llm"]
        analyzer = LLMAnalyzer(
            base_url=llm_config["base_url"],
            model=llm_config["model"],
            temperature=llm_config["temperature"],
            max_tokens=llm_config["max_tokens"],
            timeout=llm_config["timeout"],
        )

        if analyzer.is_available():
            context_limit = config.get("context_limit")

            # Unified analysis: for each context, use the enriched prompt
            # if we have the cited paper's content, otherwise use the
            # context-only prompt. Each context record indicates which
            # mode was used via the 'analysis_mode' field.
            log.info("Running unified LLM analysis...")
            contexts = analyze_all_contexts(
                contexts=contexts,
                bibliography=bibliography,
                analyzer=analyzer,
                content_enriched=True,
                limit=context_limit,
                processed_papers=processed_papers,
            )
        else:
            log.warning(
                "Ollama is not available. Skipping LLM analysis. "
                "Contexts will be saved without function classifications."
            )

        # Save contexts with metadata.
        contexts_output = {
            "_metadata": build_contexts_metadata(config),
            "contexts": contexts,
        }
        write_json(contexts_output, contexts_path)
        log.info("Citation contexts saved: %s", contexts_path)

        # Update bibliography with cited_by data and metadata.
        bib_output = {
            "_metadata": build_bibliography_metadata(config),
            "entries": bibliography,
        }
        write_json(bib_output, bibliography_path)

        # Save state.
        _save_graph_state(graph, graph_state_path)

        total_ctx = sum(len(v) for v in contexts.values())
        processing_log["stages"]["contexts"] = {
            "status": "success",
            "total_contexts": total_ctx,
            "cited_works_with_contexts": len(contexts),
        }

    # =========================================================================
    # Stage 4: Cluster analysis
    # =========================================================================
    if run_cluster:
        log.info("=" * 60)
        log.info("STAGE 4: Cluster analysis")
        log.info("=" * 60)

        # Load contexts if not already in memory.
        if not run_contexts:
            if not contexts_path.exists():
                log.error("Citation contexts not found. Run --contexts first.")
                sys.exit(1)
            raw_contexts = read_json(contexts_path)
            # Handle both old format (dict) and new format (with _metadata).
            contexts = raw_contexts.get("contexts", raw_contexts)

        if not (run_extract or run_f1 or run_contexts):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run earlier stages first.")
                sys.exit(1)

        bibliography = graph.get_bibliography()

        # --- Build co-occurrence matrix ---
        cooccurrence = build_cooccurrence_matrix(
            contexts,
            min_cooccurrence=config["clustering"]["min_cooccurrence"],
        )

        if not cooccurrence:
            log.warning("No co-occurrence pairs found above threshold. Skipping clustering.")
        else:
            # --- Identify clusters ---
            clusters = identify_clusters(cooccurrence, contexts)

            # --- LLM characterization ---
            llm_config = config["llm"]
            analyzer = LLMAnalyzer(
                base_url=llm_config["base_url"],
                model=llm_config["model"],
                temperature=llm_config["temperature"],
                max_tokens=llm_config["max_tokens"],
                timeout=llm_config["timeout"],
            )

            if analyzer.is_available():
                # Context-only cluster analysis.
                log.info("Running context-only cluster analysis...")
                analyzed_clusters = analyze_clusters(
                    clusters=clusters,
                    contexts=contexts,
                    bibliography=bibliography,
                    analyzer=analyzer,
                    content_enriched=False,
                )
                write_json(
                    {
                        "_metadata": build_clusters_metadata(config, enriched=False),
                        "clusters": analyzed_clusters,
                    },
                    output_dir / "clusters_context_only.json",
                )

                # Content-enriched cluster analysis.
                if config["clustering"].get("run_content_enriched", True):
                    log.info("Running content-enriched cluster analysis...")
                    enriched_clusters = analyze_clusters(
                        clusters=clusters,
                        contexts=contexts,
                        bibliography=bibliography,
                        analyzer=analyzer,
                        content_enriched=True,
                    )
                    write_json(
                        {
                            "_metadata": build_clusters_metadata(config, enriched=True),
                            "clusters": enriched_clusters,
                        },
                        output_dir / "clusters_content_enriched.json",
                    )
            else:
                log.warning("Ollama not available. Saving clusters without LLM analysis.")
                write_json(
                    {
                        "_metadata": build_clusters_metadata(config, enriched=False),
                        "clusters": clusters,
                    },
                    output_dir / "clusters_context_only.json",
                )

        processing_log["stages"]["cluster"] = {
            "status": "success",
            "cooccurrence_pairs": len(cooccurrence) if cooccurrence else 0,
        }

    # =========================================================================
    # Stage 5: Coverage report
    # =========================================================================
    if run_coverage:
        log.info("=" * 60)
        log.info("STAGE 5: Coverage report")
        log.info("=" * 60)

        if not (run_extract or run_f1 or run_contexts or run_cluster):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run earlier stages first.")
                sys.exit(1)

        from bibvik.coverage import generate_coverage_report, download_oa_papers

        bibliography = graph.get_bibliography()
        processed_papers = graph.get_processed_papers()

        report = generate_coverage_report(
            bibliography=bibliography,
            processed_papers=processed_papers,
            f1_pdf_dir=config["f1_pdf_dir"],
            config=config,
            output_dir=output_dir,
            email=args.email,
            check_oa=bool(args.email),
        )

        # Download OA papers if requested.
        if args.download_oa and args.email:
            download_dir = Path(config["f1_pdf_dir"]) / "oa_downloads"
            download_results = download_oa_papers(report, download_dir, generation="F1")
            processing_log["stages"]["coverage"] = {
                "status": "success",
                "f1_coverage_percent": report["summary"]["f1_coverage_percent"],
                "oa_downloaded": sum(download_results.values()) if download_results else 0,
            }
        else:
            processing_log["stages"]["coverage"] = {
                "status": "success",
                "f1_coverage_percent": report["summary"]["f1_coverage_percent"],
            }

    # =========================================================================
    # Reference Audit (--audit)
    # =========================================================================
    if run_audit:
        log.info("=" * 60)
        log.info("REFERENCE AUDIT: Detecting in-text citations vs bibliography")
        log.info("=" * 60)

        from bibvik.reference_audit import audit_references

        # The audit needs TEI-XML. If we just ran extraction, it's in memory.
        # Otherwise, read from saved TEI files.
        if not (run_extract or run_f1):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run --extract first.")
                sys.exit(1)

        processed_papers = graph.get_processed_papers()

        # Build LLM config for Layer 3 audit detection.
        llm_audit_config = config.get("llm", {})

        # Check if TEI-XML is available in memory or from files.
        tei_dir = output_dir / "tei"
        audit_results = {}
        total_detected = 0
        total_matched = 0
        total_unmatched = 0
        all_unmatched_agg: dict[tuple, dict] = {}

        for pdf_name, paper_data in processed_papers.items():
            # Try to get TEI-XML: first from memory, then from saved file.
            tei_xml = paper_data.get("tei_xml", "")
            if not tei_xml:
                tei_path = tei_dir / f"{Path(pdf_name).stem}.tei.xml"
                if tei_path.exists():
                    with open(tei_path, "r", encoding="utf-8") as f:
                        tei_xml = f.read()

            if not tei_xml:
                log.debug("No TEI-XML for %s. Set save_tei_xml: true and re-run extraction.", pdf_name)
                continue

            refs = paper_data.get("references", [])
            paras = paper_data.get("paragraphs", [])
            report = audit_references(
                tei_xml, refs,
                source_pdf=pdf_name,
                llm_config=llm_audit_config,
                paragraphs=paras,
            )
            audit_results[pdf_name] = report

            total_detected += report.get("total_unique_citations", 0)
            total_matched += report.get("matched", 0)
            total_unmatched += report.get("unmatched", 0)

            # Aggregate unmatched
            for entry in report.get("unmatched_citations", []):
                key = (entry["first_author"].lower(), entry["year"])
                if key not in all_unmatched_agg:
                    all_unmatched_agg[key] = {
                        "first_author": entry["first_author"],
                        "year": entry["year"],
                        "total_occurrences": 0,
                        "found_in_papers": [],
                        "hints": [],
                    }
                all_unmatched_agg[key]["total_occurrences"] += entry["occurrences"]
                all_unmatched_agg[key]["found_in_papers"].append(pdf_name)
                if entry.get("hint"):
                    all_unmatched_agg[key]["hints"].append(entry["hint"])

        aggregated_unmatched = sorted(
            all_unmatched_agg.values(),
            key=lambda x: x["total_occurrences"],
            reverse=True,
        )

        audit_output = {
            "summary": {
                "papers_audited": len(audit_results),
                "total_in_text_citations_detected": total_detected,
                "total_matched_to_bibliography": total_matched,
                "total_unmatched": total_unmatched,
                "overall_match_rate": round(total_matched / total_detected, 3) if total_detected else 1.0,
                "unique_unmatched_references": len(aggregated_unmatched),
            },
            "aggregated_unmatched": aggregated_unmatched,
            "per_paper_reports": audit_results,
        }

        write_json(audit_output, output_dir / "reference_audit.json")
        log.info(
            "Reference audit complete. Match rate: %.1f%% (%d/%d). %d unique unmatched.",
            (total_matched / total_detected * 100) if total_detected else 100,
            total_matched,
            total_detected,
            len(aggregated_unmatched),
        )

        processing_log["stages"]["audit"] = {
            "status": "success",
            "match_rate": audit_output["summary"]["overall_match_rate"],
            "unmatched": len(aggregated_unmatched),
        }

    # =========================================================================
    # Save processing log
    # =========================================================================
    processing_log["end_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_json(processing_log, output_dir / "processing_log.json")

    log.info("=" * 60)
    log.info("Pipeline complete. Output files in: %s", output_dir)
    log.info("=" * 60)


# =============================================================================
# Graph state persistence
# =============================================================================
# The CitationGraph object holds in-memory state (bibliography, processed papers,
# GROBID mappings) that must persist across separately-invoked stages. We
# serialize this state to a JSON file.

def _save_graph_state(graph: CitationGraph, path: Path) -> None:
    """
    Save the graph's state to a JSON file for inter-stage persistence.

    We save:
    - bibliography
    - grobid_map (with tuple keys serialized as strings)
    - seed_citekey
    - processed_papers (paragraphs and GROBID ID mappings, but NOT full TEI-XML
      to keep file size reasonable)
    """
    state = {
        "bibliography": graph.bibliography,
        "seed_citekey": graph.seed_citekey,
        "grobid_map": {f"{k[0]}|||{k[1]}": v for k, v in graph.grobid_map.items()},
        "processed_papers": {},
    }

    # Save processed paper data, excluding bulky TEI-XML.
    for pdf_name, data in graph.processed_papers.items():
        state["processed_papers"][pdf_name] = {
            "header": data.get("header", {}),
            "paragraphs": data.get("paragraphs", []),
            "source_pdf": data.get("source_pdf", ""),
            "grobid_id_to_citekey": data.get("grobid_id_to_citekey", {}),
            "references": data.get("references", []),
        }

    write_json(state, path)


def _load_graph_state(path: Path, config: dict) -> CitationGraph | None:
    """
    Load a previously saved graph state and reconstruct the CitationGraph.
    """
    if not path.exists():
        return None

    state = read_json(path)

    # Reconstruct the graph.
    grobid = GrobidClient(
        base_url=config["grobid"]["base_url"],
        timeout=config["grobid"]["timeout"],
    )
    processor = PDFProcessor(
        grobid=grobid,
        save_tei=config.get("save_tei_xml", False),
        tei_dir=Path(config["output_dir"]) / "tei",
    )
    # Load optional Zotero CSV mapping.
    zotero_map = None
    zotero_csv_path = config.get("zotero_csv")
    if zotero_csv_path:
        from bibvik.zotero_csv import parse_zotero_csv
        zotero_map = parse_zotero_csv(zotero_csv_path)

    graph = CitationGraph(processor, zotero_map=zotero_map)

    graph.bibliography = state.get("bibliography", {})
    graph.seed_citekey = state.get("seed_citekey")
    graph.processed_papers = state.get("processed_papers", {})

    # Reconstruct grobid_map with tuple keys.
    raw_map = state.get("grobid_map", {})
    graph.grobid_map = {}
    for key_str, val in raw_map.items():
        parts = key_str.split("|||")
        if len(parts) == 2:
            graph.grobid_map[(parts[0], parts[1])] = val

    return graph


if __name__ == "__main__":
    main()
