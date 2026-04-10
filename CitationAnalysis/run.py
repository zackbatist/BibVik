#!/usr/bin/env python3
"""
BibVik Citation Analysis — Main entry point.

Usage:
    python run.py --all                        # Full pipeline
    python run.py --extract                    # Seed paper only
    python run.py --extract --iterate-f1       # Seed + F1 papers (builds citation graph)
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

from bibvik.utils import load_config, setup_logging, write_json, read_json
from bibvik.grobid_client import GrobidClient
from bibvik.pdf_processor import PDFProcessor
from bibvik.citation_graph import CitationGraph
from bibvik.context_extractor import extract_all_contexts
from bibvik.llm_analyzer import LLMAnalyzer, analyze_all_contexts
from bibvik.normalize import normalize_titles_in_bibliography, normalize_authors_in_bibliography
from bibvik.cluster_analyzer import build_cooccurrence_matrix, identify_clusters, analyze_clusters
from bibvik.metadata import build_bibliography_metadata, build_contexts_metadata, build_clusters_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BibVik Citation Analysis Toolkit")
    parser.add_argument("--all", action="store_true", help="Run the full pipeline (stages 1-4).")
    parser.add_argument("--extract", action="store_true", help="Stage 1: Extract references from the seed paper.")
    parser.add_argument("--iterate-f1", action="store_true", help="Stage 2: Build citation graph from F1 papers.")
    parser.add_argument("--contexts", action="store_true", help="Stage 3: Extract and analyze citation contexts.")
    parser.add_argument("--cluster", action="store_true", help="Stage 4: Cluster analysis.")
    parser.add_argument("--coverage", action="store_true", help="Generate coverage report.")
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--seed", type=str, default=None)
    parser.add_argument("--f1-dir", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of F1 papers (for testing).")
    parser.add_argument("--context-limit", type=int, default=None)
    parser.add_argument("--email", type=str, default=None, help="Email for Unpaywall API (--coverage).")
    parser.add_argument("--download-oa", action="store_true")

    args = parser.parse_args()
    if not any([args.all, args.extract, args.iterate_f1, args.contexts, args.cluster, args.coverage]):
        parser.print_help()
        sys.exit(1)
    return args


def _write_bibliography(bibliography: dict, path: Path, config: dict, log: logging.Logger) -> None:
    from bibvik.biblatex_model import add_completeness_scores
    n_titles = normalize_titles_in_bibliography(bibliography)
    n_authors = normalize_authors_in_bibliography(bibliography)
    log.debug("Normalization: %d titles, %d author forms updated.", n_titles, n_authors)
    add_completeness_scores(bibliography)
    write_json({"_metadata": build_bibliography_metadata(config), "entries": bibliography}, path)


def main():
    args = parse_args()

    config = load_config(args.config)
    if args.output_dir: config["output_dir"] = args.output_dir
    if args.seed: config["seed_paper"] = args.seed
    if args.f1_dir: config["f1_pdf_dir"] = args.f1_dir
    if args.verbose: config["log_level"] = "DEBUG"
    if args.limit is not None: config["limit"] = args.limit
    if args.context_limit is not None: config["context_limit"] = args.context_limit
    config["save_tei_xml"] = True  # Always save TEI — needed for citation collection

    log = setup_logging(config["log_level"])
    log.info("BibVik — seed: %s", Path(config["seed_paper"]).name)

    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    processing_log = {"start_time": time.strftime("%Y-%m-%d %H:%M:%S"), "stages": {}}

    run_extract = args.all or args.extract
    run_f1 = args.all or args.iterate_f1
    run_contexts = args.all or args.contexts
    run_cluster = args.all or args.cluster
    run_coverage = args.coverage

    bibliography_path = output_dir / "bibliography.json"
    graph_state_path = output_dir / "_graph_state.json"

    # =========================================================================
    # Stage 1: Seed paper
    # =========================================================================
    if run_extract:
        log.info("\n── %s", "STAGE 1: Extracting references from seed paper")

        grobid = GrobidClient(base_url=config["grobid"]["base_url"], timeout=config["grobid"]["timeout"])
        if not grobid.is_alive():
            log.error("GROBID is not available. Start it with:")
            log.error("  docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1")
            sys.exit(1)

        processor = PDFProcessor(grobid=grobid, save_tei=True, tei_dir=output_dir / "tei")

        zotero_map = None
        if config.get("zotero_csv"):
            from bibvik.zotero_csv import parse_zotero_csv
            zotero_map = parse_zotero_csv(config["zotero_csv"])

        graph = CitationGraph(processor, zotero_map=zotero_map)

        seed_path = Path(config["seed_paper"])
        if not seed_path.exists():
            log.error("Seed paper not found: %s", seed_path)
            sys.exit(1)

        result = graph.process_seed_paper(seed_path)
        if result is None:
            log.error("Failed to process seed paper.")
            sys.exit(1)

        _write_bibliography(graph.get_bibliography(), bibliography_path, config, log)
        log.info("  %d references extracted → bibliography.json", len(graph.get_bibliography()))
        _save_graph_state(graph, graph_state_path)

        processing_log["stages"]["extract"] = {"status": "success", "references_found": len(result["references"])}

    # =========================================================================
    # Stage 2: F1 papers — build citation graph
    # =========================================================================
    if run_f1:
        log.info("\n── %s", "STAGE 2: Building citation graph from F1 papers")

        if not run_extract:
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run --extract first.")
                sys.exit(1)

        llm_cfg = config.get("llm", {})
        llm = LLMAnalyzer(
            base_url=llm_cfg.get("base_url", "http://localhost:11434"),
            model=llm_cfg.get("model", "qwen3:35b"),
            temperature=llm_cfg.get("temperature", 0.2),
            max_tokens=llm_cfg.get("max_tokens", 1024),
            timeout=llm_cfg.get("timeout", 300),
        )
        llm_available = llm.is_available()
        if not llm_available:
            log.warning("Ollama unavailable — LLM citation detection and footnote extraction will be skipped")

        email = args.email or config.get("email", "")

        # GROBID extraction
        def _progress(i, n, stem, n_refs, n_inline, success, matched=True):
            label = stem[:55] + "…" if len(stem) > 55 else stem
            status = f"{n_refs} bibliography entries" if success else "FAILED"
            print(f"\n  [{i:>{len(str(n))}}/{n}]  {label}  ({status})", flush=True)

        f1_results = graph.process_f1_papers(
            f1_dir=config["f1_pdf_dir"],
            seed_pdf_path=config["seed_paper"],
            limit=config.get("limit"),
            progress_callback=_progress,
        )
        print("", flush=True)

        # Citation collection — find everything each paper cites
        from bibvik.citation_collector import collect_and_resolve, format_summary
        from bibvik.tei_parser import parse_tei_footnotes

        tei_dir = output_dir / "tei"
        processed_papers = graph.get_processed_papers()
        bibliography = graph.get_bibliography()
        seed_pdf_name = Path(config["seed_paper"]).name

        totals = {k: 0 for k in ["grobid_refs", "llm_detected", "footnote_refs_added",
                                   "resolved_via_crossref", "resolved_via_llm", "unresolvable"]}

        for pdf_name, paper_data in processed_papers.items():
            if pdf_name == seed_pdf_name:
                continue

            label = Path(pdf_name).stem
            label = label[:55] + "…" if len(label) > 55 else label
            print(f"        {label}:", end=" ", flush=True)

            tei_xml = paper_data.get("tei_xml", "")
            if not tei_xml:
                tei_path = tei_dir / f"{Path(pdf_name).stem}.tei.xml"
                if tei_path.exists():
                    tei_xml = tei_path.read_text(encoding="utf-8")

            paper_citekey = next(
                (k for k, v in bibliography.items() if v.get("_source_pdf") == pdf_name), None
            )

            summary = collect_and_resolve(
                paper_pdf_name=pdf_name,
                paper_citekey=paper_citekey,
                tei_xml=tei_xml,
                grobid_refs=paper_data.get("references", []),
                paragraphs=paper_data.get("paragraphs", []),
                footnote_texts=parse_tei_footnotes(tei_xml) if tei_xml else [],
                bibliography=bibliography,
                llm_analyzer=llm if llm_available else None,
                llm_config=llm_cfg if llm_available else None,
                email=email,
            )

            print(format_summary(summary), flush=True)
            for k in totals:
                totals[k] += summary.get(k, 0)

        print("", flush=True)
        _write_bibliography(bibliography, bibliography_path, config, log)
        log.info("  %d total entries in bibliography", len(bibliography))

        total_new = totals["resolved_via_crossref"] + totals["resolved_via_llm"] + totals["footnote_refs_added"]
        if total_new:
            log.info("  +%d new entries  (%d from footnotes, %d via CrossRef, %d via LLM)",
                     total_new, totals["footnote_refs_added"],
                     totals["resolved_via_crossref"], totals["resolved_via_llm"])

        _save_graph_state(graph, graph_state_path)
        processing_log["stages"]["iterate_f1"] = {
            "status": "success",
            "pdfs_processed": sum(f1_results.values()),
            "total_bibliography_entries": len(bibliography),
            **totals,
        }

    # =========================================================================
    # Stage 3: Citation contexts
    # =========================================================================
    contexts_path = output_dir / "citation_contexts.json"

    if run_contexts:
        log.info("\n── %s", "STAGE 3: Extracting and analyzing citation contexts")

        if not (run_extract or run_f1):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run earlier stages first.")
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

        llm_cfg = config["llm"]
        analyzer = LLMAnalyzer(
            base_url=llm_cfg["base_url"], model=llm_cfg["model"],
            temperature=llm_cfg["temperature"], max_tokens=llm_cfg["max_tokens"],
            timeout=llm_cfg["timeout"],
        )

        if analyzer.is_available():
            log.info("  Analyzing contexts with LLM...")
            contexts = analyze_all_contexts(
                contexts=contexts, bibliography=bibliography, analyzer=analyzer,
                content_enriched=True, limit=config.get("context_limit"),
                processed_papers=processed_papers,
            )
        else:
            log.warning("Ollama unavailable — contexts saved without LLM classification")

        write_json({"_metadata": build_contexts_metadata(config), "contexts": contexts}, contexts_path)
        log.info("  %d cited works, contexts saved", len(contexts))

        _write_bibliography(bibliography, bibliography_path, config, log)
        _save_graph_state(graph, graph_state_path)

        processing_log["stages"]["contexts"] = {
            "status": "success",
            "total_contexts": sum(len(v) for v in contexts.values()),
            "cited_works": len(contexts),
        }

    # =========================================================================
    # Stage 4: Cluster analysis
    # =========================================================================
    if run_cluster:
        log.info("\n── %s", "STAGE 4: Cluster analysis")

        if not run_contexts:
            if not contexts_path.exists():
                log.error("Citation contexts not found. Run --contexts first.")
                sys.exit(1)
            raw = read_json(contexts_path)
            contexts = raw.get("contexts", raw)

        if not (run_extract or run_f1 or run_contexts):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run earlier stages first.")
                sys.exit(1)

        bibliography = graph.get_bibliography()
        cooccurrence = build_cooccurrence_matrix(contexts, min_cooccurrence=config["clustering"]["min_cooccurrence"])

        if not cooccurrence:
            log.warning("No co-occurrence pairs found. Skipping clustering.")
        else:
            clusters = identify_clusters(cooccurrence, contexts)
            llm_cfg = config["llm"]
            analyzer = LLMAnalyzer(
                base_url=llm_cfg["base_url"], model=llm_cfg["model"],
                temperature=llm_cfg["temperature"], max_tokens=llm_cfg["max_tokens"],
                timeout=llm_cfg["timeout"],
            )

            if analyzer.is_available():
                log.info("  Running context-only cluster analysis...")
                write_json(
                    {"_metadata": build_clusters_metadata(config, enriched=False),
                     "clusters": analyze_clusters(clusters, contexts, bibliography, analyzer, content_enriched=False)},
                    output_dir / "clusters_context_only.json",
                )
                if config["clustering"].get("run_content_enriched", True):
                    log.info("  Running content-enriched cluster analysis...")
                    write_json(
                        {"_metadata": build_clusters_metadata(config, enriched=True),
                         "clusters": analyze_clusters(clusters, contexts, bibliography, analyzer, content_enriched=True)},
                        output_dir / "clusters_content_enriched.json",
                    )
            else:
                log.warning("Ollama unavailable — clusters saved without LLM characterization")
                write_json(
                    {"_metadata": build_clusters_metadata(config, enriched=False), "clusters": clusters},
                    output_dir / "clusters_context_only.json",
                )

        processing_log["stages"]["cluster"] = {"status": "success", "cooccurrence_pairs": len(cooccurrence) if cooccurrence else 0}

    # =========================================================================
    # Coverage report
    # =========================================================================
    if run_coverage:
        log.info("\n── %s", "STAGE 5: Coverage report")

        if not (run_extract or run_f1 or run_contexts or run_cluster):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                log.error("Cannot load state. Run earlier stages first.")
                sys.exit(1)

        from bibvik.coverage import generate_coverage_report, download_oa_papers

        report = generate_coverage_report(
            bibliography=graph.get_bibliography(),
            processed_papers=graph.get_processed_papers(),
            f1_pdf_dir=config["f1_pdf_dir"],
            config=config, output_dir=output_dir,
            email=args.email, check_oa=bool(args.email),
        )

        if args.download_oa and args.email:
            results = download_oa_papers(report, Path(config["f1_pdf_dir"]) / "oa_downloads", generation="F1")
            processing_log["stages"]["coverage"] = {
                "status": "success",
                "f1_coverage_percent": report["summary"]["f1_coverage_percent"],
                "oa_downloaded": sum(results.values()) if results else 0,
            }
        else:
            processing_log["stages"]["coverage"] = {
                "status": "success",
                "f1_coverage_percent": report["summary"]["f1_coverage_percent"],
            }

    # =========================================================================
    processing_log["end_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
    write_json(processing_log, output_dir / "processing_log.json")
    log.info("Done. Output → %s", output_dir)


# =============================================================================
# Graph state persistence
# =============================================================================

def _save_graph_state(graph: CitationGraph, path: Path) -> None:
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
        }
    write_json(state, path)


def _load_graph_state(path: Path, config: dict) -> CitationGraph | None:
    if not path.exists():
        return None

    state = read_json(path)
    grobid = GrobidClient(base_url=config["grobid"]["base_url"], timeout=config["grobid"]["timeout"])
    processor = PDFProcessor(grobid=grobid, save_tei=True, tei_dir=Path(config["output_dir"]) / "tei")

    zotero_map = None
    if config.get("zotero_csv"):
        from bibvik.zotero_csv import parse_zotero_csv
        zotero_map = parse_zotero_csv(config["zotero_csv"])

    graph = CitationGraph(processor, zotero_map=zotero_map)
    graph.bibliography = state.get("bibliography", {})
    graph.seed_citekey = state.get("seed_citekey")
    graph._seed_pdf_name = state.get("seed_pdf_name")
    graph.processed_papers = state.get("processed_papers", {})

    raw_map = state.get("grobid_map", {})
    graph.grobid_map = {}
    for key_str, val in raw_map.items():
        parts = key_str.split("|||")
        if len(parts) == 2:
            graph.grobid_map[(parts[0], parts[1])] = val

    _repair_graph_state(graph)
    return graph


def _repair_graph_state(graph: CitationGraph) -> None:
    if not graph._seed_pdf_name and graph.seed_citekey:
        seed_entry = graph.bibliography.get(graph.seed_citekey, {})
        inferred = seed_entry.get("_source_pdf", "")
        if inferred:
            graph._seed_pdf_name = inferred

    graph._validate_titles_against_raw_citations()

    orphans = [k for k, v in graph.bibliography.items() if v.get("_failed_match")]
    for k in orphans:
        del graph.bibliography[k]


if __name__ == "__main__":
    main()
