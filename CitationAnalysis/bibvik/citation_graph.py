"""
bibvik.citation_graph — Multi-generational citation graph builder.

This module is responsible for assembling individual PDF extraction results
into a unified bibliography with citation-graph metadata. It handles:

1. **Seed paper processing**: Extract references from the seed paper (P).
   These references are labeled as F1 (first generation).

2. **F1 iteration**: For each PDF in the F1 directory, extract its references
   (labeled F2) and merge them into the bibliography. Each reference records
   which papers cite it.

3. **Deduplication**: When the same source appears in multiple papers'
   bibliographies, records are merged (combining metadata and accumulating
   cited_by entries) rather than duplicated.

4. **Generational tracking**: Each reference knows its generation (distance
   from the seed paper) and which papers cite it. This structure is recursive
   and designed to extend to F3, F4, etc. in the future.

Deduplication strategy:
    Exact matching is hard because different papers may cite the same source
    with slight variations (e.g., different abbreviations, missing fields).
    We use a multi-tier matching approach:
    1. DOI match (most reliable)
    2. Title similarity (fuzzy, for cases where DOIs are missing)
    3. Author + year match (as a secondary signal)

    The matching is conservative: we only merge when confidence is high, to
    avoid incorrectly combining distinct sources.
"""

import logging
import re
from pathlib import Path

from tqdm import tqdm

from .biblatex_model import merge_records
from .pdf_processor import PDFProcessor
from .utils import generate_citekey, reset_citekey_registry, collect_pdfs

logger = logging.getLogger(__name__)


class CitationGraph:
    """
    Build and manage a multi-generational citation graph.

    The graph is stored as a dictionary mapping citekeys to biblatex records.
    Each record includes a 'cited_by' list tracking which papers reference it,
    and a 'generation' field indicating its distance from the seed paper.

    Usage:
        graph = CitationGraph(processor)
        graph.process_seed_paper("/path/to/seed.pdf")
        graph.process_f1_papers("/path/to/f1_pdfs/")
        graph.save("output/bibliography.json")
    """

    def __init__(self, processor: PDFProcessor, zotero_map: dict[str, dict] | None = None):
        """
        Args:
            processor:   An initialized PDFProcessor instance.
            zotero_map:  Optional Zotero CSV mapping (from zotero_csv.parse_zotero_csv).
                         If provided, used for exact PDF↔bibliography matching
                         during F1 processing.
        """
        self.processor = processor
        self.zotero_map = zotero_map or {}

        # The bibliography: citekey → normalized record dict.
        self.bibliography: dict[str, dict] = {}

        # Mapping from (source_pdf, grobid_id) → citekey in bibliography.
        # Used for resolving inline citations to bibliography entries.
        self.grobid_map: dict[tuple[str, str], str] = {}

        # Store processed data per PDF for context extraction later.
        self.processed_papers: dict[str, dict] = {}

        # The seed paper's own citekey (set after processing).
        self.seed_citekey: str | None = None

    def process_seed_paper(self, pdf_path: str | Path) -> dict | None:
        """
        Process the seed paper and populate the bibliography with its references.

        References extracted from the seed paper are labeled as generation F1
        (they are one step removed from the seed).

        The seed paper itself is also added to the bibliography as generation
        "seed" (or "P" for primary), using its header metadata.

        Args:
            pdf_path: Path to the seed paper PDF.

        Returns:
            The processing result dict, or None if processing failed.
        """
        pdf_path = Path(pdf_path)
        logger.info("Processing seed paper: %s", pdf_path.name)

        # Reset citekey registry for a fresh start.
        reset_citekey_registry()

        result = self.processor.process(pdf_path, generation="F1")
        if result is None:
            logger.error("Failed to process seed paper.")
            return None

        # --- Add the seed paper itself to the bibliography ---
        header = result["header"]
        seed_authors = header.get("author", [])
        seed_year_match = re.search(r"\b(\d{4})\b", str(header.get("date", "")))
        seed_year = seed_year_match.group(1) if seed_year_match else None
        self.seed_citekey = generate_citekey(seed_authors, seed_year)

        seed_record = {
            "citekey": self.seed_citekey,
            "entry_type": "article",  # Assumed; could be inferred more carefully.
            "title": header.get("title", ""),
            "author": seed_authors,
            "date": header.get("date", ""),
            "year": seed_year or "",
            "abstract": header.get("abstract", ""),
            "doi": header.get("doi", ""),
            "generation": "P",  # Primary / seed
            "cited_by": [],
            "_source_pdf": pdf_path.name,
        }
        self.bibliography[self.seed_citekey] = seed_record

        # --- Add references from the seed paper ---
        for ref in result["references"]:
            citekey = ref["citekey"]

            # Check for duplicates (DOI or title match).
            existing_key = self._find_duplicate(ref)
            if existing_key:
                # Merge into existing record.
                merge_records(self.bibliography[existing_key], ref)
                citekey = existing_key
            else:
                self.bibliography[citekey] = ref

            # Record the GROBID ID mapping for this source PDF.
            gid = ref.get("_grobid_id", "")
            if gid:
                self.grobid_map[(pdf_path.name, gid)] = citekey

        # Store processed data for later context extraction.
        self.processed_papers[pdf_path.name] = result

        logger.info(
            "Seed paper processed. Bibliography now has %d entries.",
            len(self.bibliography),
        )

        return result

    def process_f1_papers(
        self,
        f1_dir: str | Path,
        seed_pdf_path: str | Path | None = None,
        limit: int | None = None,
    ) -> dict[str, bool]:
        """
        Process all F1 PDFs and add their references to the bibliography.

        For each F1 PDF:
        1. Extract its references (these are F2 generation).
        2. Try to match the F1 paper itself to an existing F1 entry in the
           bibliography (so we know its citekey and can set up cited_by links).
        3. Merge new references into the bibliography, recording that they
           are cited by this F1 paper.

        Args:
            f1_dir:        Directory containing F1 PDFs.
            seed_pdf_path: Path to the seed paper (to exclude from F1 processing).
            limit:         If set, only process this many F1 PDFs. Useful for
                           testing with a small subset (e.g., limit=5).

        Returns:
            Dict mapping PDF filenames to success/failure booleans.
        """
        f1_dir = Path(f1_dir)
        pdfs = collect_pdfs(f1_dir, exclude=seed_pdf_path)

        if limit is not None and limit < len(pdfs):
            logger.info(
                "Limiting to %d of %d F1 PDFs (--limit flag).", limit, len(pdfs)
            )
            pdfs = pdfs[:limit]

        logger.info("Found %d F1 PDFs to process.", len(pdfs))
        results = {}

        for pdf_path in tqdm(pdfs, desc="Processing F1 papers"):
            try:
                result = self._process_one_f1(pdf_path)
                results[pdf_path.name] = result is not None
            except Exception as e:
                logger.error("Error processing %s: %s", pdf_path.name, e)
                results[pdf_path.name] = False

        logger.info(
            "F1 processing complete. %d/%d succeeded. Bibliography has %d entries.",
            sum(results.values()),
            len(results),
            len(self.bibliography),
        )

        return results

    def _process_one_f1(self, pdf_path: Path) -> dict | None:
        """
        Process a single F1 paper.

        References found in this paper are generation F2 (two steps from seed).
        We also try to match this paper to its existing F1 entry in the
        bibliography so we can correctly attribute cited_by links.
        """
        result = self.processor.process(pdf_path, generation="F2")
        if result is None:
            return None

        # --- Try to match this PDF to an existing bibliography entry ---
        # The F1 paper should already be in the bibliography (added during
        # seed paper processing). We match by comparing the header metadata
        # against existing entries.
        header = result["header"]
        f1_citekey = self._match_f1_to_existing(header, pdf_path.name)

        if f1_citekey:
            logger.info("Matched %s to bibliography entry: %s", pdf_path.name, f1_citekey)
            # Update the existing record with any additional metadata from the
            # paper's own header (e.g., abstract, if not already present).
            existing = self.bibliography[f1_citekey]
            if header.get("abstract") and not existing.get("abstract"):
                existing["abstract"] = header["abstract"]
            existing["_source_pdf"] = pdf_path.name
        else:
            # Could not match — this PDF might not be one of the seed's references,
            # or the matching heuristic failed. We still process its references
            # but log a warning.
            logger.warning(
                "Could not match %s to any existing bibliography entry. "
                "Its references will still be extracted but cited_by links "
                "may be incomplete.",
                pdf_path.name,
            )
            # Create a minimal entry for this paper.
            f1_authors = header.get("author", [])
            year_match = re.search(r"\b(\d{4})\b", str(header.get("date", "")))
            f1_year = year_match.group(1) if year_match else None
            f1_citekey = generate_citekey(f1_authors, f1_year)
            self.bibliography[f1_citekey] = {
                "citekey": f1_citekey,
                "entry_type": "article",
                "title": header.get("title", ""),
                "author": f1_authors,
                "date": header.get("date", ""),
                "year": f1_year or "",
                "generation": "F1",
                "cited_by": [],
                "_source_pdf": pdf_path.name,
            }

        # --- Add F2 references ---
        for ref in result["references"]:
            ref_citekey = ref["citekey"]

            # Check for duplicates.
            existing_key = self._find_duplicate(ref)
            if existing_key:
                ref_citekey = existing_key
                merge_records(self.bibliography[existing_key], ref)
            else:
                self.bibliography[ref_citekey] = ref

            # Record GROBID mapping.
            gid = ref.get("_grobid_id", "")
            if gid:
                self.grobid_map[(pdf_path.name, gid)] = ref_citekey

        # Store processed data.
        self.processed_papers[pdf_path.name] = result

        return result

    def _find_duplicate(self, ref: dict) -> str | None:
        """
        Check if a reference already exists in the bibliography.

        Matching tiers (in order of reliability):
        1. DOI: Exact match (case-insensitive). DOIs are globally unique
           identifiers, so this is the most reliable signal.
        2. Title: Exact normalized string comparison.
        3. Title token overlap + author/year confirmation: Fuzzy title match
           with additional signals to avoid false positives.

        Returns the existing citekey if a match is found, or None.
        """
        ref_doi = ref.get("doi", "").strip().lower()
        ref_title = self._normalize_title(ref.get("title", ""))
        ref_authors = ref.get("author", [])
        ref_year = ref.get("year", "") or ref.get("date", "")

        best_fuzzy_key = None
        best_fuzzy_score = 0.0

        for key, existing in self.bibliography.items():
            # --- Tier 1: DOI match ---
            existing_doi = existing.get("doi", "").strip().lower()
            if ref_doi and existing_doi and ref_doi == existing_doi:
                return key

            # --- Tier 2: Exact title match ---
            existing_title = self._normalize_title(existing.get("title", ""))
            if ref_title and existing_title and ref_title == existing_title:
                # Additional check: titles must be at least 20 chars to avoid
                # false matches on very short/generic titles like "Introduction".
                if len(ref_title) >= 20:
                    return key

            # --- Tier 3: Fuzzy title + author AND year confirmation ---
            if ref_title and existing_title:
                score = self._token_overlap_score(ref_title, existing_title)
                if score >= 0.7:
                    existing_authors = existing.get("author", [])
                    existing_year = existing.get("year", "")
                    year_match = (
                        ref_year and existing_year
                        and re.search(r"\d{4}", ref_year)
                        and re.search(r"\d{4}", existing_year)
                        and re.search(r"\d{4}", ref_year).group() == re.search(r"\d{4}", existing_year).group()
                    )
                    author_match = (
                        ref_authors and existing_authors
                        and ref_authors[0].get("family", "").lower() == existing_authors[0].get("family", "").lower()
                    )
                    # Require at least one confirming signal.
                    if year_match or author_match:
                        combined = score + (0.2 if year_match else 0) + (0.15 if author_match else 0)
                        if combined > best_fuzzy_score and combined >= 0.85:
                            best_fuzzy_score = combined
                            best_fuzzy_key = key

        return best_fuzzy_key

    def _match_f1_to_existing(self, header: dict, pdf_name: str) -> str | None:
        """
        Try to match an F1 paper's header metadata to an existing F1 entry.

        This is how we link a processed PDF to the bibliography entry that
        was created when the seed paper was processed. We use a multi-tier
        matching strategy, from most to least reliable:

        0. Zotero CSV mapping (if provided — uses known metadata from the
           user's reference manager for exact matching)
        1. DOI (exact, most reliable)
        2. Title (exact normalized match)
        3. First author + year from GROBID header metadata
        4. Title token overlap (fuzzy — catches partial title matches when
           GROBID parses titles differently from different source PDFs)
        5. Filename parsing (fallback — extracts author, year, and title
           tokens from the PDF filename, which often follows conventions
           like "Author YYYY - Title of the Paper.pdf")
        """
        # --- Tier 0: Zotero CSV mapping ---
        if self.zotero_map:
            from .zotero_csv import match_pdf_to_bibliography
            zotero_match = match_pdf_to_bibliography(
                pdf_name, self.zotero_map, self.bibliography
            )
            if zotero_match:
                logger.info(
                    "Zotero CSV matched %s to %s", pdf_name, zotero_match
                )
                return zotero_match

        header_doi = header.get("doi", "").strip().lower()
        header_title = self._normalize_title(header.get("title", ""))
        header_authors = header.get("author", [])
        header_year = ""
        date_match = re.search(r"\b(\d{4})\b", str(header.get("date", "")))
        if date_match:
            header_year = date_match.group(1)

        # --- Pre-parse filename for tier 5 ---
        fn_author, fn_year, fn_title_tokens = self._parse_pdf_filename(pdf_name)

        # Track best fuzzy match for tiers 4 and 5.
        best_fuzzy_key = None
        best_fuzzy_score = 0.0

        for key, existing in self.bibliography.items():
            # Skip the seed paper itself.
            if existing.get("generation") == "P":
                continue

            existing_doi = existing.get("doi", "").strip().lower()
            existing_title = self._normalize_title(existing.get("title", ""))
            existing_authors = existing.get("author", [])
            existing_year = existing.get("year", "")

            # --- Tier 1: DOI match (exact) ---
            if header_doi and existing_doi and header_doi == existing_doi:
                return key

            # --- Tier 2: Title match (exact normalized) ---
            if header_title and existing_title and header_title == existing_title:
                if len(header_title) >= 20:
                    return key

            # --- Tier 3: First author + year from GROBID header ---
            if (
                header_authors
                and existing_authors
                and header_year
                and existing_year
                and header_year == existing_year
            ):
                h_family = header_authors[0].get("family", "").lower()
                e_family = existing_authors[0].get("family", "").lower()
                if h_family and e_family and h_family == e_family:
                    return key

            # --- Tier 4: Title token overlap (fuzzy) ---
            # Only accept fuzzy title matches if author OR year also matches,
            # to prevent cross-author false positives (e.g., Abrams→Barrett).
            if header_title and existing_title:
                score = self._token_overlap_score(header_title, existing_title)
                if score >= 0.7:
                    # Require at least one confirming signal.
                    year_ok = header_year and existing_year and header_year == existing_year
                    author_ok = False
                    if header_authors and existing_authors:
                        h_fam = header_authors[0].get("family", "").lower()
                        e_fam = existing_authors[0].get("family", "").lower()
                        author_ok = h_fam and e_fam and h_fam == e_fam
                    if (year_ok or author_ok) and score > best_fuzzy_score:
                        best_fuzzy_score = score
                        best_fuzzy_key = key

            # --- Tier 5: Filename-based matching ---
            # PDF filenames often follow "Author YYYY - Title.pdf" conventions.
            # Require BOTH year AND author match from the filename.
            if fn_title_tokens and existing_title:
                fn_score = self._token_overlap_score(
                    " ".join(fn_title_tokens), existing_title
                )
                year_match_fn = fn_year and existing_year and fn_year == existing_year
                author_match_fn = False
                if fn_author and existing_authors:
                    e_family = existing_authors[0].get("family", "").lower()
                    author_match_fn = (
                        fn_author.lower() in e_family or e_family in fn_author.lower()
                    )

                if fn_score >= 0.5 and year_match_fn and author_match_fn:
                    combined = fn_score + 0.2 + 0.2  # Both confirmed
                    if combined > best_fuzzy_score:
                        best_fuzzy_score = combined
                        best_fuzzy_key = key

        # Return best fuzzy match if we found one above threshold.
        if best_fuzzy_key and best_fuzzy_score >= 0.8:
            logger.info(
                "Fuzzy-matched %s to %s (score: %.2f)",
                pdf_name, best_fuzzy_key, best_fuzzy_score,
            )
            return best_fuzzy_key

        return None

    @staticmethod
    def _parse_pdf_filename(filename: str) -> tuple[str, str, list[str]]:
        """
        Extract author, year, and title tokens from a PDF filename.

        Handles common naming conventions:
        - "Author YYYY - Title of Paper.pdf"
        - "Author_YYYY_Title.pdf"
        - "Author (YYYY) Title.pdf"
        - "YYYY - Author - Title.pdf"

        Returns:
            Tuple of (author_str, year_str, title_tokens).
            Any component may be empty if not parseable.
        """
        # Strip .pdf extension.
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename

        # Extract year (4-digit number).
        year = ""
        year_match = re.search(r"\b(\d{4})\b", stem)
        if year_match:
            year = year_match.group(1)

        # Try "Author YYYY - Title" pattern (most common in Zotero exports).
        # Split on common delimiters around the year.
        parts = re.split(r"\s*[-–—]\s*", stem, maxsplit=2)

        author = ""
        title_tokens = []

        if len(parts) >= 2:
            # First part likely contains author (and possibly year).
            author_part = parts[0].strip()
            # Remove year from author part.
            author_part = re.sub(r"\s*\(?\d{4}\)?\s*", " ", author_part).strip()
            # Remove underscores used as spaces.
            author_part = author_part.replace("_", " ").strip()
            author = author_part

            # Remaining parts are the title.
            title_str = " ".join(parts[1:]).strip()
            # Remove year if embedded in title.
            title_str = re.sub(r"\s*\(?\d{4}\)?\s*", " ", title_str).strip()
            # Tokenize: lowercase, split on non-alphanumeric.
            title_tokens = [
                t.lower() for t in re.split(r"[^a-zA-Z0-9]+", title_str) if len(t) >= 3
            ]
        elif len(parts) == 1:
            # No delimiter — try splitting on year.
            if year:
                before, _, after = stem.partition(year)
                author = before.strip().rstrip("(").strip().replace("_", " ")
                title_str = after.strip().lstrip(")").strip()
                title_tokens = [
                    t.lower() for t in re.split(r"[^a-zA-Z0-9]+", title_str) if len(t) >= 3
                ]

        return (author, year, title_tokens)

    @staticmethod
    def _token_overlap_score(text_a: str, text_b: str) -> float:
        """
        Compute token-level Jaccard-like overlap between two normalized strings.

        Filters out very short tokens (< 3 chars) and common stop words to
        focus on content-bearing terms. Returns a float between 0 and 1.
        """
        stop_words = {
            "the", "and", "for", "with", "from", "that", "this", "its",
            "are", "was", "were", "been", "has", "have", "had", "not",
            "but", "can", "will", "into", "than", "also", "about",
        }

        def tokenize(text):
            tokens = set(re.split(r"\s+", text.lower()))
            return {t for t in tokens if len(t) >= 3 and t not in stop_words}

        tokens_a = tokenize(text_a)
        tokens_b = tokenize(text_b)

        if not tokens_a or not tokens_b:
            return 0.0

        intersection = tokens_a & tokens_b
        # Use the size of the smaller set as denominator so that a short title
        # matching most of its tokens against a longer title still scores high.
        smaller = min(len(tokens_a), len(tokens_b))
        return len(intersection) / smaller if smaller > 0 else 0.0

    @staticmethod
    def _normalize_title(title: str) -> str:
        """
        Normalize a title for comparison: lowercase, strip punctuation and
        extra whitespace.
        """
        if not title:
            return ""
        t = title.lower()
        t = re.sub(r"[^\w\s]", "", t)  # Remove punctuation
        t = re.sub(r"\s+", " ", t).strip()  # Normalize whitespace
        return t

    def get_bibliography(self) -> dict[str, dict]:
        """Return the full bibliography dictionary."""
        return self.bibliography

    def get_processed_papers(self) -> dict[str, dict]:
        """Return all processed paper data (for context extraction)."""
        return self.processed_papers

    def get_grobid_map(self) -> dict[tuple[str, str], str]:
        """Return the (source_pdf, grobid_id) → citekey mapping."""
        return self.grobid_map
