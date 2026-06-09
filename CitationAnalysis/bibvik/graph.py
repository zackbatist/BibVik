"""
bibvik.graph — Multi-generational citation graph builder.

Assembles detection results from detector.py and resolution results from
resolver.py into a unified bibliography with generational tracking,
deduplication, and Zotero-assisted matching.

The graph is the central data structure: a dict mapping citekeys to
bibliographic records, where each record tracks who cites it, what
generation it belongs to, and how it was detected/resolved.
"""

import logging
import re
from pathlib import Path

from tqdm import tqdm

from .utils import generate_citekey, reset_citekey_registry, collect_pdfs, write_json, extract_year, norm_author
from .grobid_client import GrobidClient
from .tei_parser import parse_tei_references, parse_tei_body, parse_tei_header, parse_tei_footnotes, detect_language
from .detector import detect_all_citations
from .resolver import resolve_citations
from .normalize import normalize_entry
from .enricher import enrich_entry as _enrich_entry

logger = logging.getLogger(__name__)


# =============================================================================
# Module-level helpers
# =============================================================================

# Cyrillic → Latin transliteration table (ALA-LC standard, covers Russian,
# Ukrainian, Bulgarian). Used for cross-script duplicate detection in
# _find_duplicate() — cached transliterations keyed by raw family name.
_CYRILLIC_TO_LATIN = str.maketrans({
    'а': 'a',  'б': 'b',  'в': 'v',  'г': 'g',  'д': 'd',
    'е': 'e',  'ё': 'yo', 'ж': 'zh', 'з': 'z',  'и': 'i',
    'й': 'i',  'к': 'k',  'л': 'l',  'м': 'm',  'н': 'n',
    'о': 'o',  'п': 'p',  'р': 'r',  'с': 's',  'т': 't',
    'у': 'u',  'ф': 'f',  'х': 'kh', 'ц': 'ts', 'ч': 'ch',
    'ш': 'sh', 'щ': 'shch', 'ъ': '', 'ы': 'y',  'ь': '',
    'э': 'e',  'ю': 'iu', 'я': 'ia',
    # Ukrainian
    'є': 'ie', 'і': 'i',  'ї': 'i',  'ґ': 'g',
})
_TRANSLIT_CACHE: dict[str, str] = {}


def _split_compound_entry(ref: dict, llm_config: dict) -> list[dict] | None:
    """
    Ask the LLM to split a compound citation entry into individual references.
    Returns a list of entry dicts if splitting succeeded, None otherwise.
    Called inline during graph construction for entries flagged _possibly_compound.
    """
    import json as _json
    import requests as _requests
    import re as _re

    raw = ref.get("_raw_citation", "")
    if not raw:
        return None

    prompt = (
        "You are an expert bibliographer. The following string contains multiple "
        "bibliographic references merged together. Split them into individual references "
        "and return a JSON array of objects, each with keys: "
        "first_author_family, first_author_given, year, title, container_title, entry_type.\n\n"
        f"String: {raw}\n\n"
        "Respond ONLY with a JSON array. /no_think"
    )

    try:
        base_url = llm_config.get("base_url", "http://localhost:11434")
        model    = llm_config.get("model", "qwen2.5:7b")
        timeout  = llm_config.get("timeout", 60)
        backend  = llm_config.get("backend", "ollama")

        if backend == "ollama":
            resp = _requests.post(
                f"{base_url}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False,
                      "think": False, "options": {"temperature": 0.1, "num_predict": 512}},
                timeout=timeout,
            )
            raw_resp = resp.json().get("response", "").strip()
        else:
            resp = _requests.post(
                f"{base_url}/v1/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "stream": False, "temperature": 0.1, "max_tokens": 512},
                timeout=timeout,
            )
            raw_resp = resp.json()["choices"][0]["message"]["content"].strip()

        raw_resp = _re.sub(r"<think>[\s\S]*?</think>", "", raw_resp).strip()
        m = _re.search(r"\[[\s\S]*\]", raw_resp)
        if not m:
            return None
        parsed = _json.loads(m.group(0))

        if not isinstance(parsed, list) or len(parsed) < 2:
            return None

        entries = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            family = item.get("first_author_family", "")
            given  = item.get("first_author_given", "")
            if not family:
                continue
            entry = {
                "author":      [{"family": family, "given": given}],
                "date":        str(item.get("year", "")),
                "year":        str(item.get("year", ""))[:4],
                "title":       item.get("title", ""),
                "entry_type":  item.get("entry_type", "misc"),
                "_raw_citation": raw,
                "_split_from": ref.get("citekey", ""),
            }
            ct = item.get("container_title", "")
            if ct:
                if entry["entry_type"] == "article":
                    entry["journaltitle"] = ct
                else:
                    entry["booktitle"] = ct
            entries.append(entry)

        return entries if entries else None

    except Exception:
        return None


def _transliterate_author(name: str) -> str:
    """Transliterate a family name to Latin script for cross-script comparison."""
    if not name:
        return ""
    if name in _TRANSLIT_CACHE:
        return _TRANSLIT_CACHE[name]
    result = name.lower().translate(_CYRILLIC_TO_LATIN)
    # Strip non-alpha after transliteration and normalize
    result = re.sub(r"[^a-z]", "", result)
    _TRANSLIT_CACHE[name] = result
    return result
    """
    Build and manage a multi-generational citation graph.

    The graph is stored as a dict mapping citekeys to biblatex records.
    Each record includes cited_by, generation, and resolution provenance.
    """

    def __init__(
        self,
        grobid: GrobidClient,
        tei_dir: str | Path = "./output/tei",
        zotero_map: dict[str, dict] | None = None,
    ):
        self.grobid = grobid
        self.tei_dir = Path(tei_dir)
        self.zotero_map = zotero_map or {}

        self.bibliography: dict[str, dict] = {}
        self.grobid_map: dict[tuple[str, str], str] = {}
        self.processed_papers: dict[str, dict] = {}
        self.seed_citekey: str | None = None
        self._seed_pdf_name: str = ""

    # =========================================================================
    # Seed paper
    # =========================================================================

    def process_seed_paper(
        self,
        pdf_path: str | Path,
        llm_config: dict | None = None,
        email: str = "",
        phase_callback=None,
    ) -> dict | None:
        """
        Process the seed paper: extract references, detect citations,
        resolve unmatched, build the initial bibliography.
        """
        pdf_path = Path(pdf_path)
        self._seed_pdf_name = pdf_path.name
        logger.info("Processing seed paper: %s", pdf_path.name)

        reset_citekey_registry()

        # GROBID extraction
        logger.info("  Sending to GROBID (this may take 30–60 seconds)...")
        if phase_callback:
            phase_callback(event="grobid_start", citekey=pdf_path.stem[:20])
        _t0 = __import__('time').time()
        tei_xml = self.grobid.process_fulltext(pdf_path)
        if phase_callback:
            phase_callback(event="grobid_done", citekey=pdf_path.stem[:20], elapsed=__import__('time').time() - _t0)
        if tei_xml is None:
            logger.error("GROBID failed on seed paper.")
            return None

        # Save TEI
        self.tei_dir.mkdir(parents=True, exist_ok=True)
        tei_path = self.tei_dir / f"{pdf_path.stem}.tei.xml"
        tei_path.write_text(tei_xml, encoding="utf-8")

        # Parse
        logger.info("  Parsing TEI-XML...")
        header = parse_tei_header(tei_xml)
        grobid_refs = parse_tei_references(tei_xml)
        paragraphs = parse_tei_body(tei_xml)

        logger.info("  GROBID found %d bibliography entries, %d paragraphs", len(grobid_refs), len(paragraphs))

        # Add seed paper itself
        seed_authors = header.get("author", [])
        seed_year = _extract_year(header.get("date", ""))
        self.seed_citekey = generate_citekey(seed_authors, seed_year)

        self.bibliography[self.seed_citekey] = {
            "citekey": self.seed_citekey,
            "entry_type": "article",
            "title": header.get("title", ""),
            "author": seed_authors,
            "date": header.get("date", ""),
            "year": seed_year,
            "abstract": header.get("abstract", ""),
            "doi": header.get("doi", ""),
            "generation": "P",
            "cited_by": [],
            "_source_pdf": pdf_path.name,
        }

        # Add GROBID bibliography entries as F1
        for ref in grobid_refs:
            authors = ref.get("author", [])
            year = _extract_year(ref.get("date", ""))
            citekey = generate_citekey(authors, year)

            ref["citekey"] = citekey
            ref["generation"] = "F1"
            ref["cited_by"] = [self.seed_citekey]
            ref["_source_pdf"] = pdf_path.name
            ref = normalize_entry(ref)

            existing = self._find_duplicate(ref)
            if existing:
                self._merge_into(existing, ref)
                citekey = existing
            else:
                self.bibliography[citekey] = ref

            gid = ref.get("_grobid_id", "")
            if gid:
                self.grobid_map[(pdf_path.name, gid)] = citekey

        # Run full detection (all 5 methods)
        logger.info("  Running 5-method citation detection...")
        if phase_callback:
            phase_callback(event="llm_body_start", citekey=self.seed_citekey)
        _t0 = __import__('time').time()
        detection = detect_all_citations(
            tei_xml=tei_xml,
            source_pdf=pdf_path.name,
            llm_config=llm_config,
            grobid_refs=grobid_refs,
            paragraphs=paragraphs,
        )
        if phase_callback:
            phase_callback(event="llm_body_done", citekey=self.seed_citekey, elapsed=__import__('time').time() - _t0)
        mc = detection["method_counts"]
        logger.info("  Detection complete: %d unique citations found across all methods",
                     mc["merged_total"])
        logger.info("    reference list: %d  |  inline markers: %d  |  "
                     "text patterns: %d  |  LLM (body): %d  |  LLM (footnotes): %d",
                     mc["reference_list"], mc["inline_markers"],
                     mc["text_patterns"], mc["llm_body_scan"], mc["llm_footnotes"])

        # Integrate detected citations
        unmatched = {}
        for key, info in detection["citations"].items():
            existing_ck = self._find_by_author_year(key[0], key[1])
            if existing_ck:
                self._add_cited_by(existing_ck, self.seed_citekey)
            else:
                unmatched[key] = info

        # Integrate rich entries from footnotes
        n_fn = 0
        for rich in detection.get("rich_entries", []):
            if not rich.get("_resolution_method"):
                continue  # Skip GROBID bib entries (already added)
            authors = rich.get("author", [])
            year = _extract_year(rich.get("date", ""))
            if not authors or not year:
                continue
            citekey = generate_citekey(authors, year)
            rich["citekey"] = citekey
            rich["generation"] = "F1"
            rich["cited_by"] = [self.seed_citekey]
            rich["_source_pdf"] = pdf_path.name
            rich = normalize_entry(rich)
            existing = self._find_duplicate(rich)
            if not existing:
                self.bibliography[citekey] = rich
                n_fn += 1

        if n_fn:
            logger.info("  Added %d entries from footnotes", n_fn)

        # Resolve remaining unmatched
        if unmatched:
            logger.info("  Resolving %d unmatched citations (CrossRef + LLM)...", len(unmatched))
            if phase_callback:
                phase_callback(event="resolve_start", citekey=self.seed_citekey)
            _t0 = __import__('time').time()
            resolved = resolve_citations(unmatched, email=email, llm_config=llm_config)
            if phase_callback:
                phase_callback(event="resolve_done", citekey=self.seed_citekey, elapsed=__import__('time').time() - _t0)
            for record in resolved:
                if record.get("_resolution_method") == "stub" and not record.get("title"):
                    continue  # Skip empty stubs
                authors = record.get("author", [])
                year = _extract_year(record.get("date", ""))
                citekey = generate_citekey(authors, year)
                record["citekey"] = citekey
                record["generation"] = "F1"
                record["cited_by"] = [self.seed_citekey]
                record["_source_pdf"] = pdf_path.name
                record = normalize_entry(record)
                existing = self._find_duplicate(record)
                if not existing:
                    self.bibliography[citekey] = record

        # Store processed data. TEI-XML is saved to disk at output/tei/;
        # it is not stored in memory or serialised into the graph state.
        self.processed_papers[pdf_path.name] = {
            "header": header,
            "references": grobid_refs,
            "paragraphs": paragraphs,
            "source_pdf": pdf_path.name,
            "language": detect_language(paragraphs),
            "grobid_id_to_citekey": {
                gid: ck for (pdf, gid), ck in self.grobid_map.items()
                if pdf == pdf_path.name
            },
            "detection": detection["method_counts"],
        }

        logger.info("  Bibliography: %d entries", len(self.bibliography))
        return self.processed_papers[pdf_path.name]

    # =========================================================================
    # F1 papers
    # =========================================================================

    def process_f1_papers(
        self,
        f1_dir: str | Path,
        seed_pdf_path: str | Path | None = None,
        limit: int | None = None,
        llm_config: dict | None = None,
        email: str = "",
        progress_callback=None,
        start_callback=None,
        phase_callback=None,
    ) -> dict[str, bool]:
        """Process F1 PDFs and integrate their citations as F2.

        When llm_config contains extra_urls, papers are distributed across
        multiple LLM endpoints in parallel — one worker thread per endpoint.
        GROBID processing is always sequential (single GROBID instance).
        """
        import time as _time
        import threading

        pdfs = collect_pdfs(f1_dir, exclude=seed_pdf_path)

        if limit and limit < len(pdfs):
            logger.info("Limiting to %d of %d F1 PDFs.", limit, len(pdfs))
            pdfs = pdfs[:limit]

        # Skip already-processed PDFs (caching)
        already = [p for p in pdfs if p.name in self.processed_papers]
        remaining = [p for p in pdfs if p.name not in self.processed_papers]

        if already:
            logger.info(
                "%d of %d PDFs already processed (cached). Processing %d remaining.",
                len(already), len(pdfs), len(remaining),
            )

        total = len(pdfs)
        results = {p.name: True for p in already}
        times: list[float] = []

        # Build list of LLM endpoints for round-robin distribution
        llm_urls: list[str] = []
        if llm_config:
            llm_urls.append(llm_config.get("base_url", ""))
            for url in llm_config.get("extra_urls", []):
                if url:
                    llm_urls.append(url)
        n_workers = max(1, len(llm_urls))

        # Lock for shared state mutations (bibliography, processed_papers)
        _lock = threading.Lock()

        # Pre-fetch GROBID results in a background thread
        from concurrent.futures import ThreadPoolExecutor, Future, as_completed

        def _grobid_fetch(pdf_path: Path) -> str | None:
            """Send a PDF to GROBID (can run in background)."""
            tei = self.grobid.process_fulltext(pdf_path)
            if tei is None:
                tei = self.grobid.process_references_only(pdf_path)
            return tei

        def _process_with_url(pdf_path: Path, tei_xml: str | None,
                               llm_url: str | None, idx: int) -> tuple[bool, str, float]:
            """Process one paper using a specific LLM URL."""
            t0 = _time.time()

            # Build per-paper llm_config with the assigned URL
            paper_llm_cfg = None
            if llm_config and llm_url:
                paper_llm_cfg = {**llm_config, "base_url": llm_url}
            elif llm_config:
                paper_llm_cfg = llm_config

            with _lock:
                ok, fail_reason = self._process_one_f1(
                    pdf_path, llm_config=paper_llm_cfg, email=email,
                    prefetched_tei=tei_xml,
                    phase_callback=phase_callback,
                )

            elapsed = _time.time() - t0
            return ok, fail_reason, elapsed

        if n_workers == 1:
            # ── Sequential path (single LLM or no LLM) ───────────────────────
            grobid_executor = ThreadPoolExecutor(max_workers=1)
            prefetch_future: Future | None = None
            grobid_timeout = self.grobid.timeout

            for i, pdf_path in enumerate(remaining):
                t0 = _time.time()
                idx = len(already) + i + 1

                if start_callback:
                    start_callback(index=idx, total=total, stem=pdf_path.stem)

                tei_xml = None
                if prefetch_future is not None:
                    try:
                        tei_xml = prefetch_future.result(timeout=grobid_timeout + 30)
                    except Exception:
                        tei_xml = None
                    prefetch_future = None

                if i + 1 < len(remaining):
                    prefetch_future = grobid_executor.submit(_grobid_fetch, remaining[i + 1])

                try:
                    ok, fail_reason = self._process_one_f1(
                        pdf_path, llm_config=llm_config, email=email,
                        prefetched_tei=tei_xml,
                        phase_callback=phase_callback,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    logger.error("Unexpected error processing %s: %s", pdf_path.name, exc)
                    ok, fail_reason = False, str(exc)

                results[pdf_path.name] = ok
                elapsed = _time.time() - t0
                times.append(elapsed)

                if progress_callback:
                    paper_data = self.processed_papers.get(pdf_path.name, {})
                    mc = paper_data.get("detection", {}) or {}
                    n_bib_entries   = len(paper_data.get("references", []))
                    n_paragraphs    = len(paper_data.get("paragraphs", []))
                    with _lock:
                        n_crossref  = sum(1 for e in self.bibliography.values()
                                          if e.get("_source_pdf") == pdf_path.name
                                          and e.get("_resolution_method") == "crossref")
                        n_unresolved = sum(1 for e in self.bibliography.values()
                                          if e.get("_source_pdf") == pdf_path.name
                                          and not e.get("_resolution_method")
                                          and e.get("generation") != "P")
                    eta = (sum(times) / len(times)) * (len(remaining) - i - 1) if times else 0
                    progress_callback(
                        index        = idx,
                        total        = total,
                        stem         = pdf_path.stem,
                        success      = ok,
                        elapsed      = elapsed,
                        n_bib        = n_bib_entries,
                        n_paragraphs = n_paragraphs,
                        n_footnotes  = None,
                        detection    = mc,
                        n_crossref   = n_crossref,
                        n_unresolved = n_unresolved,
                        language     = paper_data.get("language", ""),
                        ocr_applied  = paper_data.get("ocr_applied", False),
                        failure_reason = fail_reason if not ok else "",
                        eta          = eta,
                    )

            grobid_executor.shutdown(wait=False)

        else:
            # ── Parallel path (multiple LLM endpoints) ────────────────────────
            # Divide papers across workers round-robin. Each worker processes
            # its own batch sequentially: GROBID then LLM per paper.
            # No shared queue — workers are fully independent.
            import threading as _threading

            logger.info(
                "Multi-GPU mode: %d papers across %d LLM endpoints.",
                len(remaining), n_workers,
            )

            # Divide papers across workers
            batches: list[list[Path]] = [[] for _ in range(n_workers)]
            for i, pdf_path in enumerate(remaining):
                batches[i % n_workers].append(pdf_path)

            def _worker(batch: list[Path], url: str, start_idx: int):
                worker_llm_cfg = {**llm_config, "base_url": url} if llm_config else None
                for j, pdf_path in enumerate(batch):
                    idx = start_idx + j
                    if start_callback:
                        start_callback(index=idx, total=total, stem=pdf_path.stem)
                    try:
                        t0 = _time.time()
                        ok, fail_reason = self._process_one_f1(
                            pdf_path, llm_config=worker_llm_cfg, email=email,
                            phase_callback=phase_callback,
                            state_lock=_lock,
                        )
                        elapsed = _time.time() - t0

                        with _lock:
                            results[pdf_path.name] = ok
                            times.append(elapsed)

                            if progress_callback:
                                paper_data = self.processed_papers.get(pdf_path.name, {})
                                mc         = paper_data.get("detection", {}) or {}
                                n_bib      = len(paper_data.get("references", []))
                                n_para     = len(paper_data.get("paragraphs", []))
                                n_crossref = sum(
                                    1 for e in self.bibliography.values()
                                    if e.get("_source_pdf") == pdf_path.name
                                    and e.get("_resolution_method") == "crossref"
                                )
                                n_unres = sum(
                                    1 for e in self.bibliography.values()
                                    if e.get("_source_pdf") == pdf_path.name
                                    and not e.get("_resolution_method")
                                    and e.get("generation") != "P"
                                )
                                eta = (sum(times) / len(times)) * max(0, len(remaining) - len(times))
                                progress_callback(
                                    index          = idx,
                                    total          = total,
                                    stem           = pdf_path.stem,
                                    success        = ok,
                                    elapsed        = elapsed,
                                    n_bib          = n_bib,
                                    n_paragraphs   = n_para,
                                    n_footnotes    = None,
                                    detection      = mc,
                                    n_crossref     = n_crossref,
                                    n_unresolved   = n_unres,
                                    language       = paper_data.get("language", ""),
                                    ocr_applied    = paper_data.get("ocr_applied", False),
                                    failure_reason = fail_reason if not ok else "",
                                    eta            = eta,
                                )
                    except Exception as exc:
                        logger.error("Error processing %s: %s", pdf_path.name, exc)
                        with _lock:
                            results[pdf_path.name] = False

            # Assign start indices for progress reporting
            start_indices = []
            idx = len(already) + 1
            for i in range(n_workers):
                start_indices.append(idx)
                idx += len(batches[i])

            threads = [
                _threading.Thread(
                    target=_worker,
                    args=(batches[i], llm_urls[i % n_workers], start_indices[i]),
                    daemon=True,
                )
                for i in range(n_workers)
            ]

            for t in threads:
                t.start()
            for t in threads:
                t.join()

        return results

    def _process_one_f1(
        self,
        pdf_path: Path,
        llm_config: dict | None = None,
        email: str = "",
        prefetched_tei: str | None = None,
        phase_callback=None,
        state_lock=None,
    ) -> tuple[bool, str]:
        """Process one F1 paper. Returns (success, failure_reason)."""
        import time as _t
        _cb = phase_callback  # shorthand

        def _fire(event: str, elapsed: float | None = None):
            if _cb:
                try:
                    _cb(event=event, citekey=_citekey[0], elapsed=elapsed)
                except Exception:
                    pass

        # Derive a provisional citekey from Zotero map (filename lookup) so
        # all events use a consistent identifier from the start.
        _provisional = pdf_path.stem[:22]
        if self.zotero_map:
            from .zotero_csv import match_pdf_to_bibliography
            _z = match_pdf_to_bibliography(pdf_path.name, self.zotero_map, self.bibliography)
            if _z:
                _provisional = _z
        _citekey = [_provisional]

        # ── GROBID ──────────────────────────────────────────────────────────
        if prefetched_tei is not None:
            tei_xml = prefetched_tei
        else:
            _fire("grobid_start")
            _t0 = _t.time()
            tei_xml = self.grobid.process_fulltext(pdf_path)

            # ── OCR fallback ─────────────────────────────────────────────────
            if tei_xml and "[NO_BLOCKS]" in tei_xml:
                _fire("grobid_done", _t.time() - _t0)
                _fire("ocr_start")
                _t0 = _t.time()
                ocr_pdf = self.grobid._run_ocr(pdf_path)
                _fire("ocr_done", _t.time() - _t0)
                if ocr_pdf:
                    _fire("grobid_start")
                    _t0 = _t.time()
                    tei_xml = self.grobid.process_fulltext(ocr_pdf)
                    _fire("grobid_done", _t.time() - _t0)
                else:
                    tei_xml = None
            else:
                if not prefetched_tei:
                    _fire("grobid_done", _t.time() - _t0)

            if tei_xml is None:
                tei_xml = self.grobid.process_references_only(pdf_path)

        if not tei_xml:
            return False, "GROBID returned no output"

        # Save TEI
        self.tei_dir.mkdir(parents=True, exist_ok=True)
        (self.tei_dir / f"{pdf_path.stem}.tei.xml").write_text(tei_xml, encoding="utf-8")

        # Parse
        header = parse_tei_header(tei_xml)
        grobid_refs = parse_tei_references(tei_xml)
        paragraphs = parse_tei_body(tei_xml)

        # Match this paper to existing bibliography entry
        _lock_ctx = state_lock if state_lock else __import__('contextlib').nullcontext()

        with _lock_ctx:
            f1_citekey = self._match_to_existing(header, pdf_path.name)

            if f1_citekey:
                existing = self.bibliography[f1_citekey]
                if header.get("abstract") and not existing.get("abstract"):
                    existing["abstract"] = header["abstract"]
                existing["_source_pdf"] = pdf_path.name
            else:
                f1_authors = header.get("author", [])
                f1_year = _extract_year(header.get("date", ""))
                f1_citekey = generate_citekey(f1_authors, f1_year)
                self.bibliography[f1_citekey] = normalize_entry({
                    "citekey": f1_citekey,
                    "entry_type": "article",
                    "title": header.get("title", ""),
                    "author": f1_authors,
                    "date": header.get("date", ""),
                    "year": f1_year,
                    "generation": "F1",
                    "cited_by": [],
                    "_source_pdf": pdf_path.name,
                })

            # Update citekey now that we know it
            _citekey[0] = f1_citekey

            # Add GROBID refs as F2
            for ref in grobid_refs:
                authors = ref.get("author", [])
                year = _extract_year(ref.get("date", ""))
                citekey = generate_citekey(authors, year)

                ref["citekey"] = citekey
                ref["generation"] = "F2"
                ref["cited_by"] = [f1_citekey]
                ref["_source_pdf"] = pdf_path.name
                ref = normalize_entry(ref)

                # Inline compound citation splitting — if GROBID flagged this
                # entry as possibly compound and we have an LLM, split it now
                # so the resulting entries participate in deduplication
                refs_to_add = [ref]
                if ref.get("_possibly_compound") and ref.get("_raw_citation") and llm_config:
                    split = _split_compound_entry(ref, llm_config)
                    if split:
                        refs_to_add = split

                for r in refs_to_add:
                    if r is not ref:
                        # Assign citekey, generation etc to split entries
                        r_authors = r.get("author", [])
                        r_year = _extract_year(r.get("date", ""))
                        r["citekey"] = generate_citekey(r_authors, r_year)
                        r["generation"] = "F2"
                        r["cited_by"] = [f1_citekey]
                        r["_source_pdf"] = pdf_path.name
                        r = normalize_entry(r)

                    existing = self._find_duplicate(r)
                    if existing:
                        self._merge_into(existing, r)
                    else:
                        self.bibliography[r["citekey"]] = r
                        if llm_config and llm_config.get("_email"):
                            _enrich_entry(r, email=llm_config["_email"])

                    gid = ref.get("_grobid_id", "")
                    if gid and r is ref:
                        self.grobid_map[(pdf_path.name, gid)] = r["citekey"]

        # ── Detection ────────────────────────────────────────────────────────
        _fire("llm_body_start")
        _t0 = _t.time()
        detection = detect_all_citations(
            tei_xml=tei_xml,
            source_pdf=pdf_path.name,
            llm_config=llm_config,
            grobid_refs=grobid_refs,
            paragraphs=paragraphs,
        )
        _fire("llm_body_done", _t.time() - _t0)

        # ── Integrate detections ──────────────────────────────────────────────
        with _lock_ctx:
            unmatched = {}
            for key, info in detection["citations"].items():
                existing_ck = self._find_by_author_year(key[0], key[1])
                if existing_ck:
                    self._add_cited_by(existing_ck, f1_citekey)
                else:
                    unmatched[key] = info

            # Rich footnote entries
            for rich in detection.get("rich_entries", []):
                if not rich.get("_resolution_method"):
                    continue
                authors = rich.get("author", [])
                year = _extract_year(rich.get("date", ""))
                if not authors or not year:
                    continue
                citekey = generate_citekey(authors, year)
                rich["citekey"] = citekey
                rich["generation"] = "F2"
                rich["cited_by"] = [f1_citekey]
                rich["_source_pdf"] = pdf_path.name
                rich = normalize_entry(rich)
                existing = self._find_duplicate(rich)
                if not existing:
                    self.bibliography[citekey] = rich

        # ── Resolve (outside lock — LLM inference runs in parallel) ──────────
        if unmatched:
            _fire("resolve_start")
            _t0 = _t.time()
            resolved = resolve_citations(unmatched, email=email, llm_config=llm_config)
            _fire("resolve_done", _t.time() - _t0)

            with _lock_ctx:
                for record in resolved:
                    if record.get("_resolution_method") == "stub" and not record.get("title"):
                        continue
                    authors = record.get("author", [])
                    year = _extract_year(record.get("date", ""))
                    citekey = generate_citekey(authors, year)
                    record["citekey"] = citekey
                    record["generation"] = "F2"
                    record["cited_by"] = [f1_citekey]
                    record["_source_pdf"] = pdf_path.name
                    record = normalize_entry(record)
                    existing = self._find_duplicate(record)
                    if not existing:
                        self.bibliography[citekey] = record

        # ── Store processed paper ─────────────────────────────────────────────
        with _lock_ctx:
            self.processed_papers[pdf_path.name] = {
                "header": header,
                "references": grobid_refs,
                "paragraphs": paragraphs,
                "source_pdf": pdf_path.name,
                "language": detect_language(paragraphs),
                "grobid_id_to_citekey": {
                    gid: ck for (pdf, gid), ck in self.grobid_map.items()
                    if pdf == pdf_path.name
                },
                "detection": detection["method_counts"],
            }

        return True, ""

    # =========================================================================
    # Matching and deduplication
    # =========================================================================

    def _find_duplicate(self, ref: dict) -> str | None:
        """Check if a reference already exists. Returns existing citekey or None.

        Matching strategies applied in order:
        1. DOI match
        2. Exact title match (≥20 chars)
        3. Author + year + fuzzy title (≥60% token overlap)
        4. Cross-script: transliterated author + year match (Cyrillic ↔ Latin)
        """
        ref_doi = (ref.get("doi") or "").strip().lower()
        ref_title = _norm_title(ref.get("title", ""))
        ref_authors = ref.get("author", [])
        ref_year = _extract_year(ref.get("date", "") or ref.get("year", ""))

        # Transliterate ref's first author for cross-script comparison
        ref_fam_raw = ref_authors[0].get("family", "") if ref_authors else ""
        ref_fam_translit = _transliterate_author(ref_fam_raw)

        for ck, existing in self.bibliography.items():
            # 1. DOI match
            ex_doi = (existing.get("doi") or "").strip().lower()
            if ref_doi and ex_doi and ref_doi == ex_doi:
                return ck

            # 2. Exact title match (long titles only)
            ex_title = _norm_title(existing.get("title", ""))
            if ref_title and ex_title and ref_title == ex_title and len(ref_title) >= 20:
                return ck

            # 3. Author + year + fuzzy title
            ex_year = existing.get("year", "")
            ex_authors = existing.get("author", [])
            if ref_year and ex_year and ref_year == ex_year:
                if ref_authors and ex_authors:
                    r_fam = _norm_author(ref_authors[0].get("family", ""))
                    e_fam = _norm_author(ex_authors[0].get("family", ""))
                    if r_fam and e_fam and r_fam == e_fam:
                        if ref_title and ex_title and _token_overlap(ref_title, ex_title) >= 0.6:
                            return ck
                        elif not ref_title or not ex_title:
                            return ck

                    # 4. Cross-script: transliterate both sides and compare
                    if ref_fam_translit:
                        ex_fam_raw = ex_authors[0].get("family", "")
                        ex_fam_translit = _transliterate_author(ex_fam_raw)
                        if ex_fam_translit and ref_fam_translit == ex_fam_translit:
                            # Same transliterated author + year: flag as cross-script candidate
                            # and merge if titles also match or both lack titles
                            if ref_title and ex_title and _token_overlap(ref_title, ex_title) >= 0.5:
                                return ck
                            elif not ref_title and not ex_title:
                                return ck
                            else:
                                # Flag for audit but don't auto-merge — titles differ
                                existing.setdefault("_cross_script_duplicate_candidate", [])
                                if ref.get("citekey") and ref["citekey"] not in existing["_cross_script_duplicate_candidate"]:
                                    existing["_cross_script_duplicate_candidate"].append(ref.get("citekey", ""))

        return None

    def _find_by_author_year(self, author_norm: str, year: str) -> str | None:
        """Find a bibliography entry by normalized author + year.

        Matching rules (in order of strictness):
        1. Exact match on normalized family name
        2. One name is a prefix of the other, but only if the prefix is ≥5 chars
           (handles "Sindbaek" vs "Sindbæk" after normalization, but not "Lee" vs "Leech")
        """
        year4 = year[:4]
        for ck, entry in self.bibliography.items():
            ex_year = entry.get("year", "")
            if ex_year != year4:
                continue
            ex_authors = entry.get("author", [])
            if not ex_authors:
                continue
            ex_norm = _norm_author(ex_authors[0].get("family", ""))
            if not ex_norm:
                continue
            if author_norm == ex_norm:
                return ck
            # Prefix match only if the shared prefix is meaningful (≥5 chars)
            min_len = min(len(author_norm), len(ex_norm))
            if min_len >= 5 and author_norm[:min_len] == ex_norm[:min_len]:
                return ck
        return None

    def _match_to_existing(self, header: dict, pdf_name: str) -> str | None:
        """Match an F1 paper's header to an existing bibliography entry."""
        # Tier 0: Zotero CSV
        if self.zotero_map:
            from .zotero_csv import match_pdf_to_bibliography
            m = match_pdf_to_bibliography(pdf_name, self.zotero_map, self.bibliography)
            if m:
                return m

        h_doi = (header.get("doi") or "").strip().lower()
        h_title = _norm_title(header.get("title", ""))
        h_authors = header.get("author", [])
        h_year = _extract_year(header.get("date", ""))

        for ck, entry in self.bibliography.items():
            if entry.get("generation") == "P":
                continue

            # DOI
            e_doi = (entry.get("doi") or "").strip().lower()
            if h_doi and e_doi and h_doi == e_doi:
                return ck

            # Exact title
            e_title = _norm_title(entry.get("title", ""))
            if h_title and e_title and h_title == e_title and len(h_title) >= 20:
                return ck

            # Author + year
            e_authors = entry.get("author", [])
            e_year = entry.get("year", "")
            if h_year and e_year and h_year == e_year and h_authors and e_authors:
                h_fam = _norm_author(h_authors[0].get("family", ""))
                e_fam = _norm_author(e_authors[0].get("family", ""))
                if h_fam and e_fam and h_fam == e_fam:
                    return ck

        return None

    def _merge_into(self, existing_ck: str, new_ref: dict) -> None:
        """Merge metadata from new_ref into the existing entry."""
        existing = self.bibliography[existing_ck]

        # Merge scalar fields: prefer longer/more complete
        for field in ["title", "journaltitle", "booktitle", "publisher",
                       "location", "doi", "volume", "number", "pages", "series"]:
            new_val = new_ref.get(field, "")
            old_val = existing.get(field, "")
            if new_val and (not old_val or len(str(new_val)) > len(str(old_val))):
                existing[field] = new_val

        # Merge authors: prefer longer list
        if len(new_ref.get("author", [])) > len(existing.get("author", [])):
            existing["author"] = new_ref["author"]

        # Merge cited_by
        for cb in new_ref.get("cited_by", []):
            self._add_cited_by(existing_ck, cb)

        # Keep lowest generation
        gen_order = {"P": 0, "F1": 1, "F2": 2, "F3": 3}
        new_gen = new_ref.get("generation", "")
        old_gen = existing.get("generation", "")
        if gen_order.get(new_gen, 99) < gen_order.get(old_gen, 99):
            existing["generation"] = new_gen

    def _add_cited_by(self, citekey: str, citing_key: str) -> None:
        """Add citing_key to the cited_by list of citekey."""
        if citekey not in self.bibliography:
            return
        entry = self.bibliography[citekey]
        entry.setdefault("cited_by", [])
        if citing_key and citing_key not in entry["cited_by"]:
            entry["cited_by"].append(citing_key)

    # =========================================================================
    # Accessors
    # =========================================================================

    def get_bibliography(self) -> dict[str, dict]:
        return self.bibliography

    def get_processed_papers(self) -> dict[str, dict]:
        return self.processed_papers

    def get_grobid_map(self) -> dict[tuple[str, str], str]:
        return self.grobid_map


# =============================================================================
# Module-level helpers
# =============================================================================

def _extract_year(date_str: str) -> str:
    """Delegates to utils.extract_year."""
    return extract_year(date_str)


def _norm_title(title: str) -> str:
    if not title:
        return ""
    t = title.lower()
    t = re.sub(r"[^\w\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _norm_author(name: str) -> str:
    """Delegates to utils.norm_author."""
    return norm_author(name) if name else ""


def _token_overlap(a: str, b: str) -> float:
    stops = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "not", "but"}
    ta = {w for w in a.split() if len(w) >= 3 and w not in stops}
    tb = {w for w in b.split() if len(w) >= 3 and w not in stops}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))