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

We use the `/api/processFulltextDocument` endpoint because it returns both:
  (a) parsed body text with inline citation markers (<ref> elements), and
  (b) a structured bibliography section (<listBibl>).
This dual output is essential for linking inline citations to their
bibliographic records and extracting citation contexts.
"""

import logging
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


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

        logger.info("Sending to GROBID: %s", pdf_path.name)

        # --- Build the multipart form data ---
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
                    "consolidateCitations": "1",
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
                logger.info("GROBID processed successfully: %s", pdf_path.name)
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

        logger.info("Sending to GROBID (references only): %s", pdf_path.name)

        try:
            with open(pdf_path, "rb") as pdf_file:
                files = {"input": (pdf_path.name, pdf_file, "application/pdf")}
                data = {"consolidateCitations": "1"}

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
