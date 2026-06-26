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
import json
import sys
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
from bibvik.normalize import normalize_titles_in_bibliography, normalize_authors_in_bibliography
from bibvik.metadata import build_contexts_metadata, build_clusters_metadata


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
    parser.add_argument("--audit", action="store_true", help="Draw stratified audit sample from graph state.")
    parser.add_argument("--audit-n", type=int, default=10, help="Entries per audit stratum (default: 10).")
    parser.add_argument("--audit-seed", type=int, default=42, help="Random seed for audit sampling (default: 42).")
    parser.add_argument("--audit-threshold", type=float, default=0.85, help="Title similarity threshold for duplicate detection (default: 0.85).")
    parser.add_argument("--enrich", action="store_true", help="Enrich bibliography and authors from CrossRef and OpenAlex.")
    parser.add_argument("--enrich-bib-only", action="store_true", help="Bibliography enrichment only (skip author enrichment).")
    parser.add_argument("--enrich-auth-only", action="store_true", help="Author enrichment only (skip bibliography enrichment).")
    parser.add_argument("--enrich-threshold", type=float, default=0.85, help="Title similarity threshold for CrossRef title enrichment (default: 0.85).")
    parser.add_argument("--postprocess", action="store_true", help="Post-process bibliography to fix known data quality artifacts.")
    parser.add_argument("--export", action="store_true", help="Export citation graph to GraphML, GEXF, and CSV formats.")
    parser.add_argument("--config", type=str, default="config.yaml")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--seed", type=str, default=None)
    parser.add_argument("--f1-dir", type=str, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Limit F1 papers (testing).")
    parser.add_argument("--context-limit", type=int, default=None, help="Limit LLM context analysis.")
    parser.add_argument("--email", type=str, default=None, help="Email for Unpaywall/CrossRef.")
    parser.add_argument("--download-oa", action="store_true")
    parser.add_argument("--remote", action="store_true",
                        help="Use remote cluster LLM instead of local. "
                             "Reads llm.remote_url from config.yaml.")
    parser.add_argument("--no-think", action="store_true",
                        help="Append /no_think to all LLM prompts (Qwen3 only). "
                             "Has no effect if already in prompts.")
    parser.add_argument("--model", type=str, default=None,
                        help="Override LLM model from config.yaml.")

    args = parser.parse_args()
    if not any([args.all, args.extract, args.iterate_f1, args.contexts, args.cluster, args.coverage, args.audit, args.enrich, args.enrich_bib_only, args.enrich_auth_only, args.postprocess, args.export]):
        parser.print_help()
        sys.exit(1)
    return args


def _save_bibliography(bibliography: dict, path: Path, config: dict, log: logging.Logger) -> None:
    """Normalize and save bibliography as flat citekey→entry dict."""
    from bibvik.biblatex_model import add_completeness_scores
    n_t = normalize_titles_in_bibliography(bibliography)
    n_a = normalize_authors_in_bibliography(bibliography)
    if n_t or n_a:
        log.debug("Normalization: %d titles, %d author forms updated.", n_t, n_a)
    add_completeness_scores(bibliography)
    write_json(bibliography, path)


def _fmt_time(seconds: float) -> str:
    """Format elapsed seconds as '4m 32s' or '45s'."""
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds)}s"


def _llm_status(llm_cfg: dict) -> str:
    """Return a short string describing LLM availability."""
    import socket
    backend    = llm_cfg.get("backend", "ollama")
    base_url   = llm_cfg.get("base_url", "http://localhost:11434")
    model      = llm_cfg.get("model", "unknown")
    extra_urls = llm_cfg.get("extra_urls", [])
    is_remote  = llm_cfg.get("base_url") == llm_cfg.get("remote_url") and llm_cfg.get("remote_url")
    location   = "remote" if is_remote else "local"
    all_urls   = [base_url] + [u for u in extra_urls if u]

    def _reachable(url: str) -> bool:
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (11434 if backend == "ollama" else 8080)
            socket.create_connection((host, port), timeout=1).close()
            return True
        except OSError:
            return False

    backend_label = "llama-server" if backend == "llama_server" else "Ollama"
    reachable = [u for u in all_urls if _reachable(u)]
    n_total = len(all_urls)

    if not reachable:
        return f"LLM unavailable ({backend_label} not running)"
    elif n_total == 1:
        return f"LLM available ({backend_label} · {model} · {location})"
    else:
        return (f"LLM available ({backend_label} · {model} · {location} · "
                f"{len(reachable)}/{n_total} endpoints)")


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

    output_dir = Path(config["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)

    log = setup_logging(
        config["log_level"],
        log_file=output_dir / "bibvik.log",
    )

    # ── Apply CLI overrides to LLM config ────────────────────────────────────
    llm_cfg = config.get("llm", {})

    if args.remote:
        remote_url = llm_cfg.get("remote_url", "")
        if not remote_url:
            print("ERROR: --remote requires llm.remote_url in config.yaml.", flush=True)
            sys.exit(1)
        llm_cfg["base_url"] = remote_url
        llm_cfg["backend"] = llm_cfg.get("remote_backend", "ollama")
        if llm_cfg.get("remote_model"):
            llm_cfg["model"] = llm_cfg["remote_model"]
        print(f"   Using remote LLM at {remote_url}", flush=True)

    if args.model:
        llm_cfg["model"] = args.model

    if args.no_think:
        llm_cfg["no_think"] = True

    run_extract  = args.all or args.extract
    run_f1       = args.all or args.iterate_f1
    run_contexts = args.all or args.contexts
    run_cluster  = args.all or args.cluster
    run_coverage = args.coverage
    run_audit    = args.audit
    run_enrich   = args.enrich or args.enrich_bib_only or args.enrich_auth_only
    do_bib_enrich  = args.enrich or args.enrich_bib_only
    do_auth_enrich = args.enrich or args.enrich_auth_only
    run_postprocess = args.postprocess

    bibliography_path = output_dir / "bibliography.json"
    graph_state_path  = output_dir / "_graph_state.json"
    contexts_path     = output_dir / "citation_contexts.json"

    email   = args.email or config.get("email", "")
    # Store email in llm_cfg so graph.py can pass it to per-paper enrichment
    if email:
        llm_cfg["_email"] = email

    partial_bib = [None]

    def _save_partial():
        if partial_bib[0] is not None:
            partial_path = output_dir / "_partial_bibliography.json"
            write_json(partial_bib[0], partial_path)
            print(f"\n  Cancelled — partial bibliography saved → {partial_path}", flush=True)

    register_cancel_callback(_save_partial)

    # =========================================================================
    # Stage 1: Seed paper
    # =========================================================================
    if run_extract:
        print(f"\n━━ Stage 1: Seed paper", flush=True)

        grobid = GrobidClient(
            base_url=config["grobid"]["base_url"],
            timeout=config["grobid"]["timeout"],
            ocr_dir=output_dir / "ocr",
            container_name=config["grobid"].get("container_name", "grobid-server"),
        )
        if not grobid.is_alive():
            print("ERROR: GROBID is not available.", flush=True)
            print("  docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1", flush=True)
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
            print(f"ERROR: Seed paper not found: {seed_path}", flush=True)
            sys.exit(1)

        print(f"   {seed_path.name}", flush=True)
        print(f"   Sending to GROBID...", end=" ", flush=True)

        import threading as _threading
        _seed_print_lock = _threading.Lock()

        def _seed_event(event: str, citekey: str, elapsed: float | None = None):
            _EVENT_LABELS = {
                "grobid_start": "→ GROBID",
                "grobid_done":  "✓ GROBID",
                "ocr_start":    "→ OCR",
                "ocr_done":     "✓ OCR",
                "llm_body_start": "→ LLM",
                "llm_body_done":  "✓ LLM",
                "resolve_start":  "→ resolve",
                "resolve_done":   "✓ resolve",
            }
            import time as _t
            label = _EVENT_LABELS.get(event)
            if not label:
                return
            ts = _t.strftime("%H:%M:%S")
            elapsed_str = f"{_fmt_time(elapsed):>6}" if elapsed is not None else ""
            with _seed_print_lock:
                print(f"{ts}  {citekey:<22}  {label:<16}{elapsed_str}", flush=True)

        result = graph.process_seed_paper(
            seed_path, llm_config=llm_cfg, email=email,
            phase_callback=_seed_event,
        )
        if result is None:
            print("ERROR: Seed paper processing failed.", flush=True)
            sys.exit(1)

        bib = graph.get_bibliography()
        detection = result.get("detection", {})
        mc = detection
        print(f"   GROBID: {len(result.get('references', []))} entries in reference list, "
              f"{len(result.get('paragraphs', []))} paragraphs", flush=True)
        print(f"   Citations detected — "
              f"reference list: {mc.get('reference_list', 0)}  "
              f"body: {mc.get('inline_markers', 0)} (GROBID), {mc.get('text_patterns', 0)} (regex)"
              + (f", {mc.get('llm_body_scan', 0)} (LLM)" if mc.get('llm_body_scan') else "  LLM unavailable"),
              flush=True)
        print(f"   {len(bib)} entries in bibliography.json", flush=True)

        partial_bib[0] = bib
        _save_bibliography(bib, bibliography_path, config, log)
        _save_graph_state(graph, graph_state_path)

    # =========================================================================
    # Stage 2: F1 papers → citation graph
    # =========================================================================
    if run_f1:
        if not run_extract:
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                print("ERROR: Run --extract first.", flush=True)
                sys.exit(1)

        # ── Startup summary ───────────────────────────────────────────────────
        from bibvik.utils import collect_pdfs
        all_pdfs = collect_pdfs(config["f1_pdf_dir"], exclude=config.get("seed_paper"))
        limit = config.get("limit")
        n_total = min(len(all_pdfs), limit) if limit else len(all_pdfs)
        n_cached = sum(1 for p in all_pdfs[:n_total] if p.name in graph.get_processed_papers())

        print(f"\n━━ Stage 2: F1 papers → citation graph", flush=True)
        print(f"   {n_total} papers", end="", flush=True)
        if n_cached:
            print(f"  ·  {n_cached} cached", end="", flush=True)
        print(f"  ·  {_llm_status(llm_cfg)}", flush=True)
        if config.get("zotero_csv"):
            print(f"   Zotero CSV loaded", flush=True)
        print(flush=True)

        # ── Per-paper progress ────────────────────────────────────────────────
        import time as _time
        import threading as _threading
        _stage2_times: list[float] = []
        _stage2_bib_before = len(graph.get_bibliography())
        _t_stage2_start = _time.time()
        _print_lock = _threading.Lock()

        _EVENT_LABELS = {
            "grobid_start":       "→ GROBID",
            "grobid_done":        "✓ GROBID",
            "ocr_start":          "→ OCR",
            "ocr_done":           "✓ OCR",
            "llm_body_start":     "→ LLM",
            "llm_body_done":      "✓ LLM",
            "resolve_start":      "→ resolve",
            "resolve_done":       "✓ resolve",
        }

        def _event(event: str, citekey: str, elapsed: float | None = None):
            label = _EVENT_LABELS.get(event)
            if not label:
                return
            ts = _time.strftime("%H:%M:%S")
            elapsed_str = f"{_fmt_time(elapsed):>6}" if elapsed is not None else ""
            with _print_lock:
                print(f"{ts}  {citekey:<22}  {label:<16}{elapsed_str}", flush=True)

        def _progress(
            index, total, stem, success, elapsed,
            n_bib, n_paragraphs, n_footnotes,
            detection, n_crossref, n_unresolved,
            language, ocr_applied, failure_reason,
            eta=None,
        ):
            ts = _time.strftime("%H:%M:%S")
            lang_tag = f" [{language}]" if language and language != "en" else ""
            eta_str = f"  ·  ~{_fmt_time(eta)} remaining" if eta else ""
            with _print_lock:
                if success:
                    print(f"{ts}  {stem[:22]:<22}  {'✓':<16}{_fmt_time(elapsed):>6}{lang_tag}", flush=True)
                else:
                    reason = failure_reason or "unknown error"
                    print(f"{ts}  {stem[:22]:<22}  {'✗':<16}{reason[:60]}", flush=True)
                _stage2_times.append(elapsed)
                # Save graph state after every paper so interruptions lose minimal work
                _save_graph_state(graph, graph_state_path)

        def _start(index, total, stem):
            pass  # events handle per-paper output now

        def _phase(phase, n_paragraphs=None, **kwargs):
            pass  # replaced by _event callbacks

        f1_results = graph.process_f1_papers(
            f1_dir=config["f1_pdf_dir"],
            seed_pdf_path=config["seed_paper"],
            limit=config.get("limit"),
            llm_config=llm_cfg,
            email=email,
            progress_callback=_progress,
            start_callback=_start,
            phase_callback=_event,
        )

        # ── Stage 2 summary ───────────────────────────────────────────────────
        bib = graph.get_bibliography()
        succeeded    = sum(f1_results.values())
        failed       = len(f1_results) - succeeded
        new_entries  = len(bib) - _stage2_bib_before
        elapsed_total = _fmt_time(_time.time() - _t_stage2_start)

        print(f"━━ Stage 2 complete  ·  {elapsed_total}", flush=True)
        print(f"   {succeeded}/{len(f1_results)} papers succeeded"
              + (f"  ·  {failed} failed" if failed else ""), flush=True)
        print(f"   {new_entries} new entries in bibliography.json  ·  {len(bib)} total", flush=True)
        print(f"   Output → {output_dir}", flush=True)

        partial_bib[0] = bib
        _save_bibliography(bib, bibliography_path, config, log)
        _save_graph_state(graph, graph_state_path)

    # =========================================================================
    # Stage 3: Citation contexts
    # =========================================================================
    if run_contexts:
        print(f"\n━━ Stage 3: Citation context analysis", flush=True)

        if not (run_extract or run_f1):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                print("ERROR: Run earlier stages first.", flush=True)
                sys.exit(1)

        bibliography = graph.get_bibliography()
        processed_papers = graph.get_processed_papers()

        # Tabled modules — imported lazily so they don't load during normal runs.
        from bibvik.context_extractor import extract_all_contexts
        from bibvik.llm_analyzer import LLMAnalyzer, analyze_all_contexts

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
            timeout=llm_cfg["timeout"], backend=llm_cfg.get("backend", "ollama"),
        )

        if analyzer.is_available():
            print(f"   Analyzing {len(contexts)} contexts with LLM...", flush=True)
            contexts = analyze_all_contexts(
                contexts=contexts, bibliography=bibliography, analyzer=analyzer,
                content_enriched=True, limit=config.get("context_limit"),
                processed_papers=processed_papers,
            )
        else:
            print("   LLM unavailable — saving contexts without classification", flush=True)

        write_json({"_metadata": build_contexts_metadata(config), "contexts": contexts}, contexts_path)
        print(f"   {len(contexts)} cited works → citation_contexts.json", flush=True)

        _save_bibliography(bibliography, bibliography_path, config, log)
        _save_graph_state(graph, graph_state_path)

    # =========================================================================
    # Stage 4: Cluster analysis
    # =========================================================================
    if run_cluster:
        print(f"\n━━ Stage 4: Cluster analysis", flush=True)

        if not run_contexts:
            if not contexts_path.exists():
                print("ERROR: Run --contexts first.", flush=True)
                sys.exit(1)
            raw = read_json(contexts_path)
            contexts = raw.get("contexts", raw)

        if not (run_extract or run_f1 or run_contexts):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                print("ERROR: Run earlier stages first.", flush=True)
                sys.exit(1)

        bibliography = graph.get_bibliography()

        # Tabled modules — imported lazily so they don't load during normal runs.
        from bibvik.cluster_analyzer import build_cooccurrence_matrix, identify_clusters, analyze_clusters
        from bibvik.llm_analyzer import LLMAnalyzer

        cooccurrence = build_cooccurrence_matrix(
            contexts, min_cooccurrence=config["clustering"]["min_cooccurrence"],
        )

        if not cooccurrence:
            print("   No co-occurrence pairs above threshold.", flush=True)
        else:
            clusters = identify_clusters(cooccurrence, contexts)

            analyzer = LLMAnalyzer(
                base_url=llm_cfg["base_url"], model=llm_cfg["model"],
                temperature=llm_cfg["temperature"], max_tokens=llm_cfg["max_tokens"],
                timeout=llm_cfg["timeout"], backend=llm_cfg.get("backend", "ollama"),
            )

            if analyzer.is_available():
                print(f"   Characterizing {len(clusters)} clusters with LLM...", flush=True)
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
                print("   LLM unavailable — saving clusters without characterization", flush=True)
                write_json(
                    {"_metadata": build_clusters_metadata(config, enriched=False), "clusters": clusters},
                    output_dir / "clusters_context_only.json",
                )

    # =========================================================================
    # Coverage
    # =========================================================================
    if run_coverage:
        print(f"\n━━ Coverage report", flush=True)

        if not (run_extract or run_f1 or run_contexts or run_cluster):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                print("ERROR: Run earlier stages first.", flush=True)
                sys.exit(1)

        from bibvik.coverage import generate_coverage_report, download_oa_papers

        summary = generate_coverage_report(
            bibliography=graph.get_bibliography(),
            processed_papers=graph.get_processed_papers(),
            f1_pdf_dir=config["f1_pdf_dir"],
            config=config, output_dir=output_dir,
            email=args.email, check_oa=bool(args.email),
        )
        print(f"   {summary['f1_with_pdf']}/{summary['f1_total']} F1 papers have PDFs "
              f"({summary['f1_coverage_pct']}%)", flush=True)
        if summary.get("oa_available"):
            print(f"   {summary['oa_available']} available open access → coverage.md", flush=True)

        if args.download_oa and args.email:
            from bibvik.coverage import download_oa_papers
            download_oa_papers(
                bibliography = graph.get_bibliography(),
                download_dir = Path(config["f1_pdf_dir"]) / "oa_downloads",
                email        = args.email,
            )

    # =========================================================================
    if run_enrich:
        print(f"\n━━ Enrichment", flush=True)

        if not (run_extract or run_f1 or run_contexts or run_cluster or run_coverage):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                print("ERROR: No graph state found. Run --iterate-f1 first.", flush=True)
                sys.exit(1)

        from bibvik.enricher import enrich_bibliography, enrich_authors

        if do_bib_enrich:
            import time as _time
            _enrich_start = _time.time()
            _enrich_times: list[float] = []
            _enrich_last = [_enrich_start]

            def _bib_progress(done, total):
                now = _time.time()
                elapsed = now - _enrich_last[0]
                _enrich_last[0] = now
                if elapsed > 0:
                    _enrich_times.append(elapsed)
                remaining = total - done
                if remaining > 0 and _enrich_times:
                    avg = sum(_enrich_times) / len(_enrich_times)
                    eta = _fmt_time(avg * remaining)
                    print(
                        f"\r   Bibliography enrichment (CrossRef)... "
                        f"{done}/{total}  ·  ~{eta} remaining   ",
                        end="", flush=True,
                    )

            print("   Bibliography enrichment (CrossRef)...", end="", flush=True)
            counts = enrich_bibliography(
                bibliography     = graph.get_bibliography(),
                email            = email,
                title_threshold  = args.enrich_threshold,
                progress_callback = _bib_progress,
            )
            print(
                f"\r   Bibliography enrichment (CrossRef): "
                f"{counts['doi_enriched']} DOI lookups  ·  "
                f"{counts['title_enriched']} title matches  ·  "
                f"{counts['skipped']} skipped  ·  "
                f"{_fmt_time(_time.time() - _enrich_start)}",
                flush=True,
            )

        if do_auth_enrich:
            print("   Author enrichment (CrossRef DOI)...", flush=True)
            counts = enrich_authors(
                processed_papers = graph.get_processed_papers(),
                email            = email,
            )
            print(
                f"   Papers found: {counts['papers_found']}  ·  "
                f"Authors enriched: {counts['authors_enriched']}  ·  "
                f"No DOI: {counts['papers_no_doi']}  ·  "
                f"Not in CrossRef: {counts['papers_not_found']}",
                flush=True,
            )

        _save_bibliography(graph.get_bibliography(), bibliography_path, config, log)
        _save_graph_state(graph, graph_state_path)
        print(f"   Output → {output_dir}", flush=True)

    # =========================================================================
    if run_postprocess:
        print("\n━━ Post-processing bibliography", flush=True)

        from bibvik.postprocess import run_postprocess as _run_postprocess

        bib_path = output_dir / "bibliography.json"
        counts = _run_postprocess(bib_path, bib_path, llm_config=llm_cfg, project_root=Path(__file__).parent)
        for name, count in counts.items():
            print(f"   {name:<45}  {count}", flush=True)

    # ── Stage: Post-process ───────────────────────────────────────────────────
    if run_audit:
        print(f"\n━━ Audit sample", flush=True)

        if not (run_extract or run_f1 or run_contexts or run_cluster or run_coverage):
            graph = _load_graph_state(graph_state_path, config)
            if graph is None:
                print("ERROR: No graph state found. Run --iterate-f1 first.", flush=True)
                sys.exit(1)

        from bibvik.audit import run_audit as _run_audit

        # Read bibliography from disk so postprocess corrections are reflected
        import json as _json
        _audit_bib_path = output_dir / "bibliography.json"
        _bib_for_audit = _json.loads(_audit_bib_path.read_text(encoding="utf-8"))

        sample_path = _run_audit(
            bibliography     = _bib_for_audit,
            processed_papers = graph.get_processed_papers(),
            output_dir       = output_dir,
            n                = args.audit_n,
            seed             = args.audit_seed,
            threshold        = args.audit_threshold,
        )
        print(f"   Audit sample → {sample_path}", flush=True)

    # ── Stage: Export ─────────────────────────────────────────────────────────
    if args.export:
        print("\n━━ Exporting citation graph", flush=True)

        from bibvik.exporter import run_export as _run_export

        bib_path = output_dir / "bibliography.json"
        bib = json.loads(bib_path.read_text(encoding="utf-8"))
        results = _run_export(bib, output_dir)

        for fmt, counts in results.items():
            parts = "  ·  ".join(f"{v} {k}" for k, v in counts.items())
            print(f"   {fmt:<12}  {parts}", flush=True)

    # =========================================================================
    print(f"\nDone. Output → {output_dir}", flush=True)
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
            "language": data.get("language", "unknown"),
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
        ocr_dir=Path(config["output_dir"]) / "ocr",
        container_name=config["grobid"].get("container_name", "grobid-server"),
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