"""
bibvik.grobid_client — HTTP client for the GROBID service.

GROBID (GeneRation Of BIbliographic Data) is a machine-learning library for
extracting structured bibliographic information from scholarly PDFs. It uses
CRF and transformer models rather than regex/heuristics, making it robust
across diverse citation styles, languages, and document layouts.

This module handles:
- Health checks (is GROBID running?)
- Sending PDFs to GROBID's fulltext processing endpoint
- Returning raw TEI-XML for downstream parsing
- OCR fallback: when GROBID returns [NO_BLOCKS] (indicating a scanned PDF
  without a text layer), we run ocrmypdf to add a text layer and retry.

We use the `/api/processFulltextDocument` endpoint because it returns both:
  (a) parsed body text with inline citation markers (<ref> elements), and
  (b) a structured bibliography section (<listBibl>).
This dual output is essential for linking inline citations to their
bibliographic records and extracting citation contexts.

OCR fallback
------------
Some PDFs in the corpus are scanned images with no embedded text. GROBID
returns a valid 200 response for these, but the TEI-XML body contains only
the marker [NO_BLOCKS], meaning it found no text to process.

When this happens, process_fulltext() automatically:
  1. Detects the [NO_BLOCKS] marker in the response.
  2. Runs ocrmypdf on the original PDF to produce a new PDF with a text layer.
     The OCR'd copy is written to <original_stem>_ocr.pdf alongside the
     original, so the original is never modified.
  3. Retries the GROBID request with the OCR'd PDF.
  4. Reports the outcome (success or persistent failure) at INFO level.

This requires ocrmypdf to be installed and available on PATH:
    pip install ocrmypdf
  or:
    brew install ocrmypdf   # macOS
    apt install ocrmypdf    # Debian/Ubuntu
"""

import logging
import shutil
import subprocess
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Marker GROBID embeds in TEI when it cannot extract any text from a PDF.
# This is the reliable signal that the PDF has no text layer.
_NO_BLOCKS_MARKER = "[NO_BLOCKS]"


class GrobidClient:
    """
    Client for interacting with a running GROBID service.

    Usage:
        client = GrobidClient(base_url="http://localhost:8070", timeout=120)
        if client.is_alive():
            tei_xml = client.process_fulltext("/path/to/paper.pdf")
    """

    def __init__(self, base_url: str = "http://localhost:8070", timeout: int = 120):
        """
        Args:
            base_url: Root URL of the GROBID service (no trailing slash).
            timeout:  Request timeout in seconds. Large or complex PDFs may
                      need 120-300s depending on hardware.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def is_alive(self) -> bool:
        """
        Check whether the GROBID service is reachable and healthy.

        Returns:
            True if GROBID responds to /api/isalive, False otherwise.
        """
        try:
            resp = requests.get(
                f"{self.base_url}/api/isalive",
                timeout=10,
            )
            return resp.status_code == 200
        except requests.ConnectionError:
            logger.error(
                "Cannot connect to GROBID at %s. "
                "Is the Docker container running? Try: docker ps",
                self.base_url,
            )
            return False
        except requests.Timeout:
            logger.error("GROBID health check timed out.")
            return False

    def process_fulltext(
        self,
        pdf_path: str | Path,
        include_coordinates: bool = False,
    ) -> str | None:
        """
        Send a PDF to GROBID for full-text processing.

        Uses the /api/processFulltextDocument endpoint, which returns TEI-XML
        containing:
        - <body>: Parsed full text with inline <ref type="bibr"> citation markers
        - <listBibl>: Structured bibliography with <biblStruct> entries

        The inline <ref> elements contain @target attributes that link to
        xml:id attributes on <biblStruct> entries, enabling us to connect
        citation contexts to specific references.

        If GROBID returns [NO_BLOCKS] (indicating a scanned PDF with no text
        layer), this method automatically attempts OCR via ocrmypdf and retries.

        Args:
            pdf_path:            Path to the PDF file.
            include_coordinates: If True, request bounding box coordinates for
                                 each element. Not needed for text extraction
                                 but useful for layout analysis.

        Returns:
            TEI-XML string on success, or None if processing failed.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            logger.error("PDF file not found: %s", pdf_path)
            return None

        tei = self._submit_to_grobid(pdf_path, include_coordinates)
        if tei is None:
            return None

        if not self._is_no_blocks(tei):
            logger.debug("GROBID processed successfully: %s", pdf_path.name)
            return tei

        # ── OCR fallback ──
        # GROBID found no text — this PDF has no text layer (scanned image).
        # Run ocrmypdf to add a text layer, then retry with GROBID.
        ocr_pdf = self._run_ocr(pdf_path)
        if ocr_pdf is None:
            logger.error("OCR failed for %s — skipping this paper.", pdf_path.name)
            return None

        tei = self._submit_to_grobid(ocr_pdf, include_coordinates)
        if tei and not self._is_no_blocks(tei):
            logger.info("OCR + GROBID succeeded for %s", pdf_path.name)
            return tei
        else:
            logger.error(
                "GROBID still found no text after OCR for %s. "
                "The PDF may be too degraded to process.",
                pdf_path.name,
            )
            return None

    def _submit_to_grobid(
        self,
        pdf_path: Path,
        include_coordinates: bool = False,
    ) -> str | None:
        """
        Send a single PDF to GROBID's processFulltextDocument endpoint.

        Low-level method used by process_fulltext() and the OCR retry path.
        Returns the raw TEI-XML response text, or None on any error.
        The caller is responsible for checking for [NO_BLOCKS].
        """
        logger.debug("Sending to GROBID: %s", pdf_path.name)

        # GROBID expects the PDF as a file upload named 'input'.
        # Additional parameters control processing behavior.
        try:
            with open(pdf_path, "rb") as pdf_file:
                files = {"input": (pdf_path.name, pdf_file, "application/pdf")}

                # consolidateHeader=1: use CrossRef/metadata lookup for header
                # consolidateCitations=1: use CrossRef lookup for each reference
                #   → This enriches metadata (DOIs, full titles, etc.)
                # includeRawCitations=1: also return the raw citation string
                #   → Useful as fallback if structured parsing fails
                data = {
                    "consolidateHeader": "1",
                    # consolidateCitations is intentionally disabled.
                    # It causes GROBID to make a CrossRef API call for every
                    # reference, which is extremely slow for papers with many
                    # references and frequently causes timeout-induced truncation.
                    # Reference enrichment is handled separately via --resolve.
                    "consolidateCitations": "0",
                    "includeRawCitations": "1",
                }

                if include_coordinates:
                    # Request coordinates for text, figures, tables, references
                    data["teiCoordinates"] = ["ref", "biblStruct", "figure"]

                resp = requests.post(
                    f"{self.base_url}/api/processFulltextDocument",
                    files=files,
                    data=data,
                    timeout=self.timeout,
                )

            if resp.status_code == 200:
                return resp.text
            elif resp.status_code == 500 and _NO_BLOCKS_MARKER in resp.text:
                # GROBID found no text layer — return the body so process_fulltext
                # can detect [NO_BLOCKS] and trigger the OCR fallback.
                return resp.text
            elif resp.status_code == 503:
                logger.warning(
                    "GROBID is busy (503). The service may be overloaded. "
                    "Try reducing concurrency or waiting. File: %s",
                    pdf_path.name,
                )
                return None
            else:
                logger.error(
                    "GROBID returned HTTP %d for %s: %s",
                    resp.status_code,
                    pdf_path.name,
                    resp.text[:500],
                )
                return None

        except requests.Timeout:
            logger.error(
                "GROBID request timed out after %ds for %s. "
                "Consider increasing grobid.timeout in config.yaml.",
                self.timeout,
                pdf_path.name,
            )
            return None
        except requests.ConnectionError:
            logger.error(
                "Lost connection to GROBID while processing %s. "
                "Is the Docker container still running?",
                pdf_path.name,
            )
            return None

    # =========================================================================
    # OCR fallback
    # =========================================================================

    @staticmethod
    def _is_no_blocks(tei_xml: str) -> bool:
        """Return True if the TEI response signals that GROBID found no text."""
        return _NO_BLOCKS_MARKER in tei_xml

    @staticmethod
    def _run_ocr(pdf_path: Path) -> "Path | None":
        """
        Run ocrmypdf on pdf_path to produce a text-layer PDF.

        The OCR'd copy is written to <stem>_ocr.pdf in the same directory as
        the original. If that file already exists (from a previous run), it is
        reused directly without re-running OCR.

        Returns the path to the OCR'd PDF, or None if ocrmypdf failed or is
        not installed.
        """
        ocr_path = pdf_path.with_name(pdf_path.stem + "_ocr.pdf")

        if ocr_path.exists():
            logger.debug("OCR copy already exists, reusing: %s", ocr_path.name)
            return ocr_path

        if not shutil.which("ocrmypdf"):
            logger.error(
                "ocrmypdf is not installed or not on PATH. "
                "Install it with: pip install ocrmypdf  (or brew/apt install ocrmypdf). "
                "Cannot apply OCR fallback for %s.",
                pdf_path.name,
            )
            return None

        logger.info(
            "Scanned PDF detected (no text layer): %s — running OCR...",
            pdf_path.name,
        )

        # --skip-text: don't fail on pages that already have some text
        #   (some PDFs are mixed: scanned body with a text-layer title page)
        # --rotate-pages: auto-correct page orientation, common in scanned PDFs
        # --deskew: straighten skewed pages before recognition
        # --output-type pdf: plain PDF, not PDF/A (simpler, no colour profile needed)
        # --quiet: suppress progress output; errors still go to stderr
        cmd = [
            "ocrmypdf",
            "--skip-text",
            "--rotate-pages",
            "--deskew",
            "--output-type", "pdf",
            "--quiet",
            str(pdf_path),
            str(ocr_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,  # OCR on a long scanned PDF can take a few minutes
            )
        except subprocess.TimeoutExpired:
            logger.error("ocrmypdf timed out after 300s for %s.", pdf_path.name)
            return None
        except FileNotFoundError:
            logger.error("ocrmypdf not found on PATH for %s.", pdf_path.name)
            return None

        if result.returncode != 0:
            # ocrmypdf exit codes: 0 = success, 1 = bad args, 2 = input error,
            # 3 = missing dependency, 4 = invalid output PDF, 5 = already OCR'd
            # (exit 5 shouldn't happen since we checked ocr_path.exists() above,
            # but handle gracefully)
            if result.returncode == 5:
                # "Input file already appears to have OCR" — treat as success
                # by falling through; the output file was still written.
                if ocr_path.exists():
                    logger.debug("ocrmypdf: file already had OCR, output written anyway.")
                    return ocr_path
            logger.error(
                "ocrmypdf failed (exit %d) for %s:\n%s",
                result.returncode,
                pdf_path.name,
                (result.stderr or result.stdout or "(no output)").strip()[:500],
            )
            return None

        logger.info("OCR complete: %s → %s", pdf_path.name, ocr_path.name)
        return ocr_path

    # =========================================================================
    # Reference-only processing
    # =========================================================================

    def process_references_only(self, pdf_path: str | Path) -> str | None:
        """
        Send a PDF to GROBID for reference-list-only processing.

        Uses /api/processReferences, which is faster than fulltext processing
        but only returns the bibliography — no body text or inline citations.

        This is useful as a fallback when fulltext processing fails or when
        you only need the reference list without citation contexts.

        Args:
            pdf_path: Path to the PDF file.

        Returns:
            TEI-XML string on success, or None if processing failed.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            logger.error("PDF file not found: %s", pdf_path)
            return None

        logger.debug("Sending to GROBID (references only): %s", pdf_path.name)

        try:
            with open(pdf_path, "rb") as pdf_file:
                files = {"input": (pdf_path.name, pdf_file, "application/pdf")}
                data = {"consolidateCitations": "0", "includeRawCitations": "1"}

                resp = requests.post(
                    f"{self.base_url}/api/processReferences",
                    files=files,
                    data=data,
                    timeout=self.timeout,
                )

            if resp.status_code == 200:
                return resp.text
            else:
                logger.error(
                    "GROBID /processReferences returned HTTP %d for %s",
                    resp.status_code,
                    pdf_path.name,
                )
                return None

        except (requests.Timeout, requests.ConnectionError) as e:
            logger.error("GROBID request failed for %s: %s", pdf_path.name, e)
            return None