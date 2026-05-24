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

logger = logging.getLogger(__name__)


class CitationGraph:
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
        tei_xml = self.grobid.process_fulltext(pdf_path)
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
        if phase_callback and llm_config:
            phase_callback("llm", len(paragraphs))
        detection = detect_all_citations(
            tei_xml=tei_xml,
            source_pdf=pdf_path.name,
            llm_config=llm_config,
            grobid_refs=grobid_refs,
            paragraphs=paragraphs,
        )
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
            resolved = resolve_citations(unmatched, email=email, llm_config=llm_config)
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
                    detection = paper_data.get("detection", {})
                    mc = detection.get("method_counts", {}) if detection else {}
                    n_bib_entries   = len(paper_data.get("references", []))
                    n_paragraphs    = len(paper_data.get("paragraphs", []))
                    n_crossref      = sum(1 for e in self.bibliography.values()
                                          if e.get("_source_pdf") == pdf_path.name
                                          and e.get("_resolution_method") == "crossref")
                    n_unresolved    = sum(1 for e in self.bibliography.values()
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
            # GROBID runs sequentially; LLM processing runs in parallel workers.
            # Strategy: GROBID-fetch each paper first (sequential), then submit
            # to a thread pool for LLM processing, round-robining across URLs.
            logger.info(
                "Multi-GPU mode: distributing %d papers across %d LLM endpoints.",
                len(remaining), n_workers,
            )

            # Phase 1: GROBID all papers sequentially, collect TEI XML
            tei_cache: dict[str, str | None] = {}
            for i, pdf_path in enumerate(remaining):
                idx = len(already) + i + 1
                if start_callback:
                    start_callback(index=idx, total=total, stem=pdf_path.stem)
                tei_cache[pdf_path.name] = _grobid_fetch(pdf_path)

            # Phase 2: Submit LLM processing in parallel
            with ThreadPoolExecutor(max_workers=n_workers) as llm_executor:
                futures = {}
                for i, pdf_path in enumerate(remaining):
                    url = llm_urls[i % n_workers]
                    idx = len(already) + i + 1
                    tei_xml = tei_cache.get(pdf_path.name)
                    future = llm_executor.submit(
                        _process_with_url, pdf_path, tei_xml, url, idx
                    )
                    futures[future] = (pdf_path, idx)

                for future in as_completed(futures):
                    pdf_path, idx = futures[future]
                    try:
                        ok, fail_reason, elapsed = future.result()
                    except Exception as exc:
                        logger.error("Error processing %s: %s", pdf_path.name, exc)
                        ok, fail_reason, elapsed = False, str(exc), 0.0

                    results[pdf_path.name] = ok
                    times.append(elapsed)

                    if progress_callback:
                        paper_data = self.processed_papers.get(pdf_path.name, {})
                        detection = paper_data.get("detection", {})
                        mc = detection.get("method_counts", {}) if detection else {}
                        n_bib_entries   = len(paper_data.get("references", []))
                        n_paragraphs    = len(paper_data.get("paragraphs", []))
                        n_crossref      = sum(1 for e in self.bibliography.values()
                                              if e.get("_source_pdf") == pdf_path.name
                                              and e.get("_resolution_method") == "crossref")
                        n_unresolved    = sum(1 for e in self.bibliography.values()
                                              if e.get("_source_pdf") == pdf_path.name
                                              and not e.get("_resolution_method")
                                              and e.get("generation") != "P")
                        eta = (sum(times) / len(times)) * max(0, len(remaining) - len(times))
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

        return results

        return results

    def _process_one_f1(
        self,
        pdf_path: Path,
        llm_config: dict | None = None,
        email: str = "",
        prefetched_tei: str | None = None,
        phase_callback=None,
    ) -> tuple[bool, str]:
        """Process one F1 paper. Returns (success, failure_reason)."""
        # GROBID — use pre-fetched result if available
        if prefetched_tei is not None:
            tei_xml = prefetched_tei
        else:
            tei_xml = self.grobid.process_fulltext(pdf_path)
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
        f1_citekey = self._match_to_existing(header, pdf_path.name)

        if f1_citekey:
            existing = self.bibliography[f1_citekey]
            if header.get("abstract") and not existing.get("abstract"):
                existing["abstract"] = header["abstract"]
            existing["_source_pdf"] = pdf_path.name
        else:
            # Create new entry
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

            existing = self._find_duplicate(ref)
            if existing:
                self._merge_into(existing, ref)
            else:
                self.bibliography[citekey] = ref

            gid = ref.get("_grobid_id", "")
            if gid:
                self.grobid_map[(pdf_path.name, gid)] = citekey

        # Full detection
        if phase_callback and llm_config:
            phase_callback("llm", len(paragraphs))
        detection = detect_all_citations(
            tei_xml=tei_xml,
            source_pdf=pdf_path.name,
            llm_config=llm_config,
            grobid_refs=grobid_refs,
            paragraphs=paragraphs,
        )

        # Integrate detections
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

        # Resolve
        if unmatched:
            resolved = resolve_citations(unmatched, email=email, llm_config=llm_config)
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

        # Store
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
        """Check if a reference already exists. Returns existing citekey or None."""
        ref_doi = (ref.get("doi") or "").strip().lower()
        ref_title = _norm_title(ref.get("title", ""))
        ref_authors = ref.get("author", [])
        ref_year = _extract_year(ref.get("date", "") or ref.get("year", ""))

        for ck, existing in self.bibliography.items():
            # DOI match
            ex_doi = (existing.get("doi") or "").strip().lower()
            if ref_doi and ex_doi and ref_doi == ex_doi:
                return ck

            # Exact title match (long titles only)
            ex_title = _norm_title(existing.get("title", ""))
            if ref_title and ex_title and ref_title == ex_title and len(ref_title) >= 20:
                return ck

            # Author + year + fuzzy title
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
                            # Author + year match without title — accept if both have authors
                            return ck

        return None

    def _find_by_author_year(self, author_norm: str, year: str) -> str | None:
        """Find a bibliography entry by normalized author + year."""
        year4 = year[:4]
        for ck, entry in self.bibliography.items():
            ex_year = entry.get("year", "")
            if ex_year != year4:
                continue
            ex_authors = entry.get("author", [])
            if ex_authors:
                ex_norm = _norm_author(ex_authors[0].get("family", ""))
                if ex_norm and (
                    author_norm == ex_norm
                    or author_norm[:4] == ex_norm[:4]
                    or author_norm in ex_norm
                    or ex_norm in author_norm
                ):
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
