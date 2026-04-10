#!/usr/bin/env python3
"""
BibVik Citation Analysis — Main entry point.

Usage:
    python run.py --all                        # Full pipeline
    python run.py --extract                    # Seed paper only
    python run.py --extract --iterate-f1       # Seed + F1 papers
    python run.py --contexts                   # Citation context analysis
    python run.py --cluster                    # Cluster analysis
    python run.py --coverage --email you@uni.edu
    python run.py --all --limit 5              # Test run with 5 F1 papers
"""

import argparse
import logging
import sys
import time
from pathlib import Path

_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from bibvik.utils import (
    load_config, setup_logging, write_json, read_json,
    install_signal_handler, register_cancel_callback, clear_cancel_callbacks,
)
from bibvik.grobid_client import GrobidClient
from bibvik.graph import CitationGraph
from bibvik.context_extractor import extract_all_contexts
from bibvik.llm_analyzer import LLMAnalyzer, analyze_all_contexts
from bibvik.normalize import normalize_titles_in_bibliography, normalize_authors_in_bibliography
from bibvik.cluster_analyzer import build_cooccurrence_matrix, identify_clusters, analyze_clusters
from bibvik.metadata import build_bibliography_metadata, build_contexts_metadata, build_clusters_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BibVik Citation Analysis Toolkit",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run.py --all                        # Full pipeline
  python run.py --all --limit 5              # Test with 5 F1 papers
  python run.py --contexts --context-limit 20  # Test LLM on 20 contexts
  python run.py --coverage --email you@uni.edu # Coverage + OA lookup
        """,
    )
    parser.add_argument("--all", action="store_true", help="Run stages 1-4.")
    parser.add_argument("--extract", action="store_true", help="Stage 1: Seed paper extraction.")
    parser.add_argument("--iterate-f1", action="store_true", help="Stage 2: F1 papers → citation graph.")
    parser.add_argument("--contexts", action="store_true", help="Stage 3: Citation context analysis.")
    parser.add_argument("--cluster", action="store_true", help="Stage 4: Cluster analysis.")
    parser.add_argument("--coverage", action="store_true", help="Coverage report + OA lookup.")
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--seed", type=str, default=None)
    parser.add_argument("--f1-dir", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Limit F1 papers (testing).")
    parser.add_argument("--context-limit", type=int, default=None, help="Limit LLM context analysis.")
    parser.add_argument("--email", type=str, default=None, help="Email for Unpaywall/CrossRef.")
    parser.add_argument("--download-oa", action="store_true")

    args = parser.parse_args()
    if not any([args.all, args.extract, args.iterate_f1, args.contexts, args.cluster, args.coverage]):
        parser.print_help()
        sys.exit(1)
    return args


def _save_bibliography(bibliography: dict, path: Path, config: dict, log: logging.Logger) -> None:
    """Normalize and save bibliography with metadata."""
    from bibvik.biblatex_model import add_completeness_scores
    n_t = normalize_titles_in_bibliography(bibliography)
    n_a = normalize_authors_in_bibliography(bibliography)
    if n_t or n_a:
        log.debug("Normalization: %d titles, %d author forms updated.", n_t, n_a)
    add_completeness_scores(bibliography)
    write_json({"_metadata": build_bibliography_metadata(config), "entries": bibliography}, path)


def main():
    args = parse_args()
    install_signal_handler()

    config = load_config(args.config)
    if args.output_dir: config["output_dir"] = args.output_dir
    if args.seed: config["seed_paper"] = args.seed
    if args.f1_dir: config["f1_pdf_dir"] = args.f1_dir
    if args.verbose: config["log_level"] = "DEBUG"
    if args.limit is not None: config["limit"] = args.limit
    if args.context_limit is not None: config["context_limit"] = args.context_limit

    log = setup_logging(config["log_level"])
    log.info("BibVik — seed: %s", Path(config["seed_paper"]).name)

    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    run_extract = args.all or args.extract
    run_f1 = args.all or args.iterate_f1
    run_contexts = args.all or args.contexts
    run_cluster = args.all or args.cluster
    run_coverage = args.coverage

    bibliography_path = output_dir / "bibliography.json"
    graph_state_path = output_dir / "_graph_state.json"
    contexts_path = output_dir / "citation_contexts.json"

    llm_cfg = config.get("llm", {})
    email = args.email or config.get("email", "")

    # ── Register cancel callback to save partial state ──
    # This gets updated as we progress through stages.
    partial_bib = [None]  # Mutable container for closure

    def _save_partial():
        if partial_bib[0] is not None:
            partial_path = output_dir / "_partial_bibliography.json"
            write_json(partial_bib[0], partial_path)
            print(f"  Saved partial bibliography → {partial_path}", flush=True)

    register_cancel_callback(_save_partial)

    # =========================================================================
    # Stage 1: Seed paper
    # =========================================================================
    if run_extract:
        log.info("━━ STAGE 1: Seed paper extraction")

        grobid = GrobidClient(
            base_url=config["grobid"]["base_url"],
            timeout=config["grobid"]["timeout"],
        )
        if not grobid.is_alive():
            log.error("GROBID is not available.")
            log.error("  docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1")
            sys.exit(1)

        zotero_map = None
        if config.get("zotero_csv"):
            from bibvik.zotero_csv import parse_zotero_csv
            zotero_map = parse_zotero_csv(config["zotero_csv"])

        graph = CitationGraph(
            grobid=grobid,
            tei_dir=output_dir / "tei",
            zotero_map=zotero_map,
        )

        seed_path = Path(config["seed_paper"])
        if not seed_path.exists():
            log.error("Seed paper not found: %s", seed_path)
            sys.exit(1)

        result = graph.process_seed_paper(seed_path, llm_config=llm_cfg, email=email)
        if result is None:
            log.error("Failed to process seed paper.")
            sys.exit(1)

        partial_bib[0] = graph.get_bibliography()
        _save_bibliography(graph.get_bibliography(), bibliography_path, config, log)
        log.info("  %d entries → bibliography.json", len(graph.get_bibliography()))
        _save_graph_state(graph, graph_state_path)

    # =========================================================================
    # Stage 2: F1 papers → citation graph
    # =========================================================================
    if run_f1:
        log.info("━━ STAGE 2: F1 papers → citation graph")

        if not run_extract:
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Run --extract first.")
                sys.exit(1)

        def _progress(i, n, stem, n_refs, n_detected, success):
            label = stem[:55] + "…" if len(stem) > 55 else stem
            if success:
                print(f"  [{i:>{len(str(n))}}/{n}] {label}  "
                      f"({n_refs} bib, {n_detected} total detected)", flush=True)
            else:
                print(f"  [{i:>{len(str(n))}}/{n}] {label}  (FAILED)", flush=True)

        f1_results = graph.process_f1_papers(
            f1_dir=config["f1_pdf_dir"],
            seed_pdf_path=config["seed_paper"],
            limit=config.get("limit"),
            llm_config=llm_cfg,
            email=email,
            progress_callback=_progress,
        )
        print("", flush=True)

        partial_bib[0] = graph.get_bibliography()
        _save_bibliography(graph.get_bibliography(), bibliography_path, config, log)
        log.info("  %d total entries in bibliography", len(graph.get_bibliography()))
        _save_graph_state(graph, graph_state_path)

    # =========================================================================
    # Stage 3: Citation contexts
    # =========================================================================
    if run_contexts:
        log.info("━━ STAGE 3: Citation context analysis")

        if not (run_extract or run_f1):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Run earlier stages first.")
                sys.exit(1)

        bibliography = graph.get_bibliography()
        processed_papers = graph.get_processed_papers()

        contexts = extract_all_contexts(
            processed_papers=processed_papers,
            grobid_map=graph.get_grobid_map(),
            bibliography=bibliography,
            sentence_window=config["context"]["sentence_window"],
            boundary_threshold=config["context"]["boundary_threshold"],
        )

        analyzer = LLMAnalyzer(
            base_url=llm_cfg["base_url"], model=llm_cfg["model"],
            temperature=llm_cfg["temperature"], max_tokens=llm_cfg["max_tokens"],
            timeout=llm_cfg["timeout"],
        )

        if analyzer.is_available():
            log.info("  Analyzing with LLM...")
            contexts = analyze_all_contexts(
                contexts=contexts, bibliography=bibliography, analyzer=analyzer,
                content_enriched=True, limit=config.get("context_limit"),
                processed_papers=processed_papers,
            )
        else:
            log.warning("  Ollama unavailable — saving without LLM classification")

        write_json({"_metadata": build_contexts_metadata(config), "contexts": contexts}, contexts_path)
        log.info("  %d cited works, contexts saved", len(contexts))

        _save_bibliography(bibliography, bibliography_path, config, log)
        _save_graph_state(graph, graph_state_path)

    # =========================================================================
    # Stage 4: Cluster analysis
    # =========================================================================
    if run_cluster:
        log.info("━━ STAGE 4: Cluster analysis")

        if not run_contexts:
            if not contexts_path.exists():
                log.error("Run --contexts first.")
                sys.exit(1)
            raw = read_json(contexts_path)
            contexts = raw.get("contexts", raw)

        if not (run_extract or run_f1 or run_contexts):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Run earlier stages first.")
                sys.exit(1)

        bibliography = graph.get_bibliography()
        cooccurrence = build_cooccurrence_matrix(
            contexts, min_cooccurrence=config["clustering"]["min_cooccurrence"],
        )

        if not cooccurrence:
            log.warning("  No co-occurrence pairs above threshold.")
        else:
            clusters = identify_clusters(cooccurrence, contexts)

            analyzer = LLMAnalyzer(
                base_url=llm_cfg["base_url"], model=llm_cfg["model"],
                temperature=llm_cfg["temperature"], max_tokens=llm_cfg["max_tokens"],
                timeout=llm_cfg["timeout"],
            )

            if analyzer.is_available():
                log.info("  Characterizing %d clusters with LLM...", len(clusters))
                analyzed = analyze_clusters(clusters, contexts, bibliography, analyzer, content_enriched=False)
                write_json(
                    {"_metadata": build_clusters_metadata(config, enriched=False), "clusters": analyzed},
                    output_dir / "clusters_context_only.json",
                )
                if config["clustering"].get("run_content_enriched", True):
                    enriched = analyze_clusters(clusters, contexts, bibliography, analyzer, content_enriched=True)
                    write_json(
                        {"_metadata": build_clusters_metadata(config, enriched=True), "clusters": enriched},
                        output_dir / "clusters_content_enriched.json",
                    )
            else:
                log.warning("  Ollama unavailable — saving clusters without characterization")
                write_json(
                    {"_metadata": build_clusters_metadata(config, enriched=False), "clusters": clusters},
                    output_dir / "clusters_context_only.json",
                )

    # =========================================================================
    # Coverage
    # =========================================================================
    if run_coverage:
        log.info("━━ STAGE 5: Coverage report")

        if not (run_extract or run_f1 or run_contexts or run_cluster):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Run earlier stages first.")
                sys.exit(1)

        from bibvik.coverage import generate_coverage_report, download_oa_papers

        generate_coverage_report(
            bibliography=graph.get_bibliography(),
            processed_papers=graph.get_processed_papers(),
            f1_pdf_dir=config["f1_pdf_dir"],
            config=config, output_dir=output_dir,
            email=args.email, check_oa=bool(args.email),
        )

        if args.download_oa and args.email:
            from bibvik.coverage import download_oa_papers
            report = read_json(output_dir / "coverage_report.json")
            download_oa_papers(report, Path(config["f1_pdf_dir"]) / "oa_downloads")

    # =========================================================================
    log.info("Done. Output → %s", output_dir)
    clear_cancel_callbacks()


# =============================================================================
# Graph state persistence
# =============================================================================

def _save_graph_state(graph: CitationGraph, path: Path) -> None:
    """Save graph state for inter-stage persistence."""
    state = {
        "bibliography": graph.bibliography,
        "seed_citekey": graph.seed_citekey,
        "seed_pdf_name": graph._seed_pdf_name,
        "grobid_map": {f"{k[0]}|||{k[1]}": v for k, v in graph.grobid_map.items()},
        "processed_papers": {},
    }
    for pdf_name, data in graph.processed_papers.items():
        state["processed_papers"][pdf_name] = {
            "header": data.get("header", {}),
            "paragraphs": data.get("paragraphs", []),
            "source_pdf": data.get("source_pdf", ""),
            "grobid_id_to_citekey": data.get("grobid_id_to_citekey", {}),
            "references": data.get("references", []),
            "detection": data.get("detection", {}),
        }
    write_json(state, path)


def _load_graph_state(path: Path, config: dict) -> CitationGraph | None:
    """Load graph state from a previous run."""
    if not path.exists():
        return None

    state = read_json(path)
    grobid = GrobidClient(
        base_url=config["grobid"]["base_url"],
        timeout=config["grobid"]["timeout"],
    )

    zotero_map = None
    if config.get("zotero_csv"):
        from bibvik.zotero_csv import parse_zotero_csv
        zotero_map = parse_zotero_csv(config["zotero_csv"])

    graph = CitationGraph(grobid=grobid, tei_dir=Path(config["output_dir"]) / "tei", zotero_map=zotero_map)
    graph.bibliography = state.get("bibliography", {})
    graph.seed_citekey = state.get("seed_citekey")
    graph._seed_pdf_name = state.get("seed_pdf_name", "")
    graph.processed_papers = state.get("processed_papers", {})

    raw_map = state.get("grobid_map", {})
    graph.grobid_map = {}
    for key_str, val in raw_map.items():
        parts = key_str.split("|||")
        if len(parts) == 2:
            graph.grobid_map[(parts[0], parts[1])] = val

    return graph


if __name__ == "__main__":
    main()
