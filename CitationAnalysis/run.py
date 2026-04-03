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
from bibvik.normalize import normalize_titles_in_bibliography, normalize_authors_in_bibliography
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
    parser.add_argument(
        "--footnotes", action="store_true",
        help="Extract bibliographic references from footnotes using the LLM. "
             "Targets papers that embed references in footnotes rather than a "
             "separate bibliography (e.g. Abrams 2012). Requires TEI-XML files "
             "in the output/tei/ directory (run --extract / --iterate-f1 first "
             "with save_tei_xml: true in config.yaml).",
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
    if not any([args.all, args.extract, args.iterate_f1, args.contexts, args.cluster, args.coverage, args.audit, args.footnotes]):
        parser.print_help()
        sys.exit(1)

    return args


def _write_bibliography(
    bibliography: dict,
    path: Path,
    config: dict,
    log: logging.Logger,
) -> None:
    """
    Normalize and write the bibliography to disk.

    Applies title and author-name normalization before writing, so the
    output is always consistent regardless of which source produced each entry.
    """
    n_titles = normalize_titles_in_bibliography(bibliography)
    n_authors = normalize_authors_in_bibliography(bibliography)
    if n_titles or n_authors:
        log.info(
            "Normalization: %d titles, %d author given-name forms updated.",
            n_titles, n_authors,
        )
    write_json(
        {"_metadata": build_bibliography_metadata(config), "entries": bibliography},
        path,
    )


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
    if args.footnotes:
        # Footnote extraction also needs TEI-XML files.
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
    run_footnotes = args.footnotes  # Not included in --all; must be explicitly requested.

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

        # Save intermediate bibliography with normalization and metadata.
        _write_bibliography(graph.get_bibliography(), bibliography_path, config, log)
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

        # Update bibliography with normalization and metadata.
        _write_bibliography(graph.get_bibliography(), bibliography_path, config, log)
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

        # Update bibliography with cited_by data, normalization, and metadata.
        _write_bibliography(bibliography, bibliography_path, config, log)

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
    # Footnote Reference Extraction (--footnotes)
    # =========================================================================
    if run_footnotes:
        log.info("=" * 60)
        log.info("FOOTNOTE EXTRACTION: Extracting references from footnotes")
        log.info("=" * 60)

        from bibvik.footnote_extractor import extract_footnote_references, load_tei_files

        # --- Load TEI-XML files ---
        tei_dir = output_dir / "tei"
        if not tei_dir.exists() or not any(tei_dir.glob("*.tei.xml")):
            log.error(
                "No TEI-XML files found in %s. "
                "Run --extract / --iterate-f1 with save_tei_xml: true in config.yaml first.",
                tei_dir,
            )
        else:
            tei_files = load_tei_files(tei_dir)

            # --- Load current bibliography ---
            if bibliography_path.exists():
                bib_data = read_json(bibliography_path)
                # Strip the _metadata key; we only want the entry dicts.
                current_bib = {
                    k: v for k, v in bib_data.items()
                    if not k.startswith("_")
                }
            else:
                current_bib = {}
                log.warning(
                    "bibliography.json not found at %s. "
                    "Footnote references will not be deduplicated against existing entries.",
                    bibliography_path,
                )

            # --- Set up LLM ---
            llm_config = config.get("llm", {})
            fn_analyzer = LLMAnalyzer(
                base_url=llm_config.get("base_url", "http://localhost:11434"),
                model=llm_config.get("model", "qwen3:35b"),
                temperature=llm_config.get("temperature", 0.2),
                max_tokens=llm_config.get("max_tokens", 2048),
                timeout=llm_config.get("timeout", 300),
            )

            if not fn_analyzer.is_available():
                log.error(
                    "Ollama is not available or model '%s' is not loaded. "
                    "Cannot run footnote extraction.",
                    llm_config.get("model", "qwen3:35b"),
                )
            else:
                footnote_results = extract_footnote_references(
                    tei_files=tei_files,
                    bibliography=current_bib,
                    analyzer=fn_analyzer,
                )

                # --- Write footnote_references.json ---
                write_json(footnote_results, output_dir / "footnote_references.json")

                # --- Merge newly discovered entries back into bibliography.json ---
                if bibliography_path.exists():
                    bib_data = read_json(bibliography_path)
                    from bibvik.metadata import build_bibliography_metadata
                    # current_bib was mutated in-place by extract_footnote_references.
                    # Any key in current_bib that isn't in the on-disk bib_data is new.
                    merged_count = 0
                    for citekey, entry in current_bib.items():
                        if citekey not in bib_data:
                            bib_data[citekey] = entry
                            merged_count += 1
                    # Refresh _metadata and normalize.
                    _write_bibliography(bib_data["entries"], bibliography_path, config, log)
                    log.info(
                        "Merged %d footnote-extracted entries into bibliography.json.",
                        merged_count,
                    )

                summary = footnote_results["summary"]
                log.info(
                    "Footnote extraction complete: %d papers, %d footnotes, "
                    "%d references extracted, %d merged into bibliography.",
                    summary["papers_processed"],
                    summary["footnotes_found"],
                    summary["references_extracted"],
                    summary["references_merged_into_bibliography"],
                )

                processing_log["stages"]["footnotes"] = {
                    "status": "success",
                    "papers_processed": summary["papers_processed"],
                    "footnotes_found": summary["footnotes_found"],
                    "references_extracted": summary["references_extracted"],
                    "references_merged": summary["references_merged_into_bibliography"],
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
        "seed_pdf_name": graph._seed_pdf_name,
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
    graph._seed_pdf_name = state.get("seed_pdf_name")
    graph.processed_papers = state.get("processed_papers", {})

    # Reconstruct grobid_map with tuple keys.
    raw_map = state.get("grobid_map", {})
    graph.grobid_map = {}
    for key_str, val in raw_map.items():
        parts = key_str.split("|||")
        if len(parts) == 2:
            graph.grobid_map[(parts[0], parts[1])] = val

    # --- Repair stale graph state ---
    # States saved before recent fixes may be missing seed_pdf_name (needed
    # for the already-claimed guard in _match_f1_to_existing), or may contain
    # corrupted titles or orphan entries created by the old buggy pipeline.
    # We repair these on load so --iterate-f1 produces correct output even
    # when run against a state file written by an older version.
    _repair_graph_state(graph)

    return graph


def _repair_graph_state(graph: "CitationGraph") -> None:
    """
    Fix known issues in graph states saved by older pipeline versions.

    Problems addressed:
    1. seed_pdf_name missing — infer it from the seed entry's _source_pdf.
    2. GROBID title errors — run _validate_titles_against_raw_citations so
       matching in the F1 stage uses correct titles.
    3. Orphan entries created by failed F1 matching — any entry whose
       _source_pdf already points to an F1 PDF (i.e. it was created as a
       fallback during a prior --iterate-f1 run rather than during --extract)
       is removed, so the upcoming F1 processing can match cleanly.
    """
    from bibvik.citation_graph import CitationGraph

    # 1. Infer seed_pdf_name if missing.
    if not graph._seed_pdf_name and graph.seed_citekey:
        seed_entry = graph.bibliography.get(graph.seed_citekey, {})
        inferred = seed_entry.get("_source_pdf", "")
        if inferred:
            graph._seed_pdf_name = inferred
            logging.getLogger("bibvik").info(
                "Repaired missing seed_pdf_name: %s", inferred
            )

    seed_pdf = graph._seed_pdf_name or ""

    # 2. Correct GROBID title errors against _raw_citation.
    n_corrected = graph._validate_titles_against_raw_citations()
    if n_corrected:
        logging.getLogger("bibvik").info(
            "State repair: corrected %d title errors via _raw_citation.", n_corrected
        )

    # 3. Remove orphan entries — those whose _source_pdf is an F1 PDF rather
    # than the seed PDF. These were created by _process_one_f1 when matching
    # failed in a prior run, and will be re-derived correctly in this run.
    # We identify them as: generation == F1, _source_pdf != seed_pdf,
    # and no _raw_citation (they were created from a PDF header, not from
    # the seed's reference list).
    orphans = [
        k for k, v in graph.bibliography.items()
        if v.get("generation") == "F1"
        and v.get("_source_pdf", "") != seed_pdf
        and not v.get("_raw_citation", "")
        and v.get("_source_pdf", "") != ""
    ]
    for k in orphans:
        logging.getLogger("bibvik").warning(
            "State repair: removing orphan entry %s (was created by failed F1 matching).",
            k,
        )
        del graph.bibliography[k]

    if orphans:
        logging.getLogger("bibvik").info(
            "State repair: removed %d orphan entries: %s", len(orphans), orphans
        )


if __name__ == "__main__":
    main()
