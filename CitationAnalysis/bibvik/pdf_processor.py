"""
bibvik.pdf_processor — Orchestrate per-PDF reference extraction.

This module ties together the GROBID client, TEI parser, and biblatex model
to process a single PDF end-to-end:

1. Send PDF to GROBID → get TEI-XML
2. Parse TEI-XML → get raw reference dicts + body paragraphs
3. Normalize references → assign citekeys, set generation, clean fields
4. Return structured data for integration into the citation graph

The module also handles:
- Saving intermediate TEI-XML files for debugging (optional)
- Falling back to reference-only extraction if fulltext fails
- Extracting the document's own metadata (for building its bibliography entry)
"""

import logging
from pathlib import Path

from .grobid_client import GrobidClient
from .tei_parser import parse_tei_references, parse_tei_body, parse_tei_header
from .biblatex_model import normalize_record
from .utils import generate_citekey

logger = logging.getLogger(__name__)


class PDFProcessor:
    """
    Process a single PDF through GROBID and return structured data.

    Usage:
        processor = PDFProcessor(grobid_client, save_tei=True, tei_dir="./tei")
        result = processor.process("/path/to/paper.pdf", generation="F1")
    """

    def __init__(
        self,
        grobid: GrobidClient,
        save_tei: bool = False,
        tei_dir: str | Path = "./output/tei",
    ):
        """
        Args:
            grobid:   An initialized GrobidClient instance.
            save_tei: If True, save raw TEI-XML to disk for debugging.
            tei_dir:  Directory for saving TEI-XML files.
        """
        self.grobid = grobid
        self.save_tei = save_tei
        self.tei_dir = Path(tei_dir)

    def process(self, pdf_path: str | Path, generation: str = "F1") -> dict | None:
        """
        Process a single PDF and return its extracted data.

        Workflow:
        1. Send to GROBID for fulltext processing.
        2. If fulltext fails, try reference-only extraction as fallback.
        3. Parse the TEI-XML to extract bibliography and body text.
        4. Normalize each reference into a biblatex record with a citekey.
        5. Extract the document's own header metadata.

        Args:
            pdf_path:   Path to the PDF file.
            generation: Generation label for extracted references (e.g., "F1").
                        References found in this PDF are this generation.

        Returns:
            A dict with:
            - 'header': Dict of the document's own metadata (title, authors, etc.)
            - 'references': List of normalized biblatex reference dicts.
            - 'paragraphs': List of body-text paragraph dicts with citation markers.
            - 'tei_xml': Raw TEI-XML string (for downstream use).
            - 'source_pdf': Filename of the processed PDF.

            Returns None if GROBID processing completely fails.
        """
        pdf_path = Path(pdf_path)
        source_name = pdf_path.name

        # --- Step 1: Send to GROBID ---
        tei_xml = self.grobid.process_fulltext(pdf_path)

        if tei_xml is None:
            # Fallback: try reference-only extraction.
            logger.warning(
                "Fulltext extraction failed for %s. Trying references-only.",
                source_name,
            )
            tei_xml = self.grobid.process_references_only(pdf_path)
            if tei_xml is None:
                logger.error("All GROBID extraction failed for %s.", source_name)
                return None

        # --- Optional: save TEI-XML for debugging ---
        if self.save_tei:
            self._save_tei(tei_xml, pdf_path.stem)

        # --- Step 2: Parse TEI-XML ---
        raw_refs = parse_tei_references(tei_xml)
        paragraphs = parse_tei_body(tei_xml)
        header = parse_tei_header(tei_xml)

        logger.info(
            "Extracted %d references and %d paragraphs from %s.",
            len(raw_refs),
            len(paragraphs),
            source_name,
        )

        # --- Step 3: Normalize references ---
        # Each raw reference gets a citekey and is formatted into the
        # canonical biblatex structure.
        normalized_refs = []
        for raw_ref in raw_refs:
            authors = raw_ref.get("author", [])
            year = raw_ref.get("date", "")
            # Extract just the year from the date string for citekey generation.
            import re
            year_match = re.search(r"\b(\d{4})\b", str(year))
            year_for_key = year_match.group(1) if year_match else None

            citekey = generate_citekey(authors, year_for_key)
            record = normalize_record(raw_ref, citekey, generation, source_name)
            normalized_refs.append(record)

        # --- Step 4: Build GROBID-ID → citekey mapping ---
        # This mapping is critical for the context_extractor module: it allows
        # us to resolve {{CITE:b42}} placeholders in body text to actual
        # citekeys in the bibliography.
        grobid_id_to_citekey = {}
        for ref in normalized_refs:
            gid = ref.get("_grobid_id", "")
            if gid:
                grobid_id_to_citekey[gid] = ref["citekey"]

        return {
            "header": header,
            "references": normalized_refs,
            "paragraphs": paragraphs,
            "tei_xml": tei_xml,
            "source_pdf": source_name,
            "grobid_id_to_citekey": grobid_id_to_citekey,
        }

    def _save_tei(self, tei_xml: str, stem: str) -> None:
        """Save TEI-XML to disk for debugging purposes."""
        self.tei_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.tei_dir / f"{stem}.tei.xml"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(tei_xml)
        logger.debug("Saved TEI-XML: %s", out_path)
