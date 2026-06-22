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
- Alternate OCR fallback: when GROBID returns [BAD_INPUT_DATA] (structural
  PDF failure) or the extracted text is dominated by private-use Unicode
  (font encoding failure), we render the PDF to images with pdftoppm and
  run Tesseract OCR, then submit the resulting text-layer PDF to GROBID.

We use the `/api/processFulltextDocument` endpoint because it returns both:
  (a) parsed body text with inline citation markers (<ref> elements), and
  (b) a structured bibliography section (<listBibl>).
This dual output is essential for linking inline citations to their
bibliographic records and extracting citation contexts.

OCR fallback (ocrmypdf)
-----------------------
Some PDFs in the corpus are scanned images with no embedded text. GROBID
returns a valid 200 response for these, but the TEI-XML body contains only
the marker [NO_BLOCKS], meaning it found no text to process.

When this happens, process_fulltext() automatically:
  1. Detects the [NO_BLOCKS] marker in the response.
  2. Runs ocrmypdf on the original PDF, writing output to a temporary file.
  3. Moves the original to output/ocr/originals/<filename> as a backup.
  4. Moves the OCR'd version into the original's place under the original name.
  5. Retries the GROBID request with the now-replaced file.
  6. Reports the outcome (success or persistent failure) at INFO level.

Alternate OCR fallback (pdftoppm + Tesseract)
----------------------------------------------
Two additional failure modes require a different approach:

1. [BAD_INPUT_DATA]: GROBID's PDF parser crashes entirely (exit code 134).
   This happens when the PDF has structural issues that prevent GROBID from
   opening it at all. Examples: Paterson et al 2014.

2. Private-use Unicode font encoding failure: GROBID produces TEI but the
   extracted text consists largely of private-use Unicode characters (e.g.
   \uf731, \uf738) because the PDF uses a custom font with no standard Unicode
   mapping. The PDF looks fine visually but text extraction gets raw glyph
   codes. Examples: Feveile 2012.

In both cases, ocrmypdf cannot help — it either can't open the PDF or the
problem is in the font mapping, not the absence of a text layer. The solution
is to bypass the PDF's text layer entirely: render each page to an image with
pdftoppm and run Tesseract OCR on the images from pixels. This produces a
new text layer independent of the original PDF's structure or font maps.

The alternate OCR path:
  1. Detects [BAD_INPUT_DATA] in a GROBID HTTP 500 response, or detects
     private-use Unicode density above a threshold in the extracted TEI.
  2. Renders the PDF to page images with pdftoppm at 300 DPI.
  3. Runs Tesseract on each page image, collecting the OCR'd text.
  4. Writes a text-layer PDF using reportlab/fpdf2, or falls back to passing
     raw text directly if PDF generation is unavailable.
  5. Submits the new PDF to GROBID and returns the result.

This requires pdftoppm (from poppler) and tesseract on PATH:
    apt install poppler-utils tesseract-ocr tesseract-ocr-nor tesseract-ocr-swe
    brew install poppler tesseract

The original is preserved in output/ocr/originals/ as with the ocrmypdf path.
"""

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

# Marker GROBID embeds in TEI when it cannot extract any text from a PDF.
_NO_BLOCKS_MARKER = "[NO_BLOCKS]"

# Marker GROBID embeds when the PDF parser crashes entirely.
_BAD_INPUT_MARKER = "[BAD_INPUT_DATA]"

# Fraction of characters in extracted TEI text that are private-use Unicode
# (U+E000–U+F8FF) above which we conclude font encoding has failed.
_PRIVATE_USE_THRESHOLD = 0.05


class GrobidClient:
    """
    Client for interacting with a running GROBID service.

    Usage:
        client = GrobidClient(base_url="http://localhost:8070", timeout=120)
        if client.is_alive():
            tei_xml = client.process_fulltext("/path/to/paper.pdf")
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8070",
        timeout: int = 120,
        ocr_dir: str | Path | None = None,
        container_name: str = "grobid-server",
    ):
        """
        Args:
            base_url:       Root URL of the GROBID service (no trailing slash).
            timeout:        Request timeout in seconds. Large or complex PDFs may
                            need 120-300s depending on hardware.
            ocr_dir:        Directory for OCR'd PDF copies. Defaults to output/ocr/
                            relative to the current working directory.
            container_name: Docker container name for automatic restart on crash.
                            Set to empty string to disable automatic restart.
        """
        self.base_url       = base_url.rstrip("/")
        self.timeout        = timeout
        self.ocr_dir        = Path(ocr_dir) if ocr_dir else Path("output/ocr")
        self.container_name = container_name
        # Set by _submit_to_grobid when [BAD_INPUT_DATA] is detected,
        # so process_fulltext() knows to attempt the pdftoppm+Tesseract fallback.
        # Set by process_fulltext() when OCR fallback ran but TEI is still garbled.
        # Checked by graph.py to mark entries from this paper as _ocr_candidate.
        self._last_bad_input: bool = False

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

    def restart_if_down(self, wait_seconds: int = 120) -> bool:
        """
        Attempt to restart the GROBID Docker container and wait for it to
        come back up.

        Called automatically when a ConnectionError is detected during
        processing. Requires Docker to be installed and the container name
        to be configured.

        Args:
            wait_seconds: Maximum seconds to wait for GROBID to become
                          available after restart.

        Returns:
            True if GROBID is back up, False if restart failed or timed out.
        """
        import time

        if not self.container_name:
            logger.warning(
                "GROBID container name not configured — cannot attempt restart. "
                "Set grobid.container_name in config.yaml."
            )
            return False

        logger.warning(
            "GROBID appears to be down. Attempting to restart Docker container '%s'...",
            self.container_name,
        )

        try:
            result = subprocess.run(
                ["docker", "restart", self.container_name],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.error(
                    "docker restart failed: %s", result.stderr.strip()
                )
                return False
        except FileNotFoundError:
            logger.error(
                "docker command not found — cannot restart GROBID container."
            )
            return False
        except subprocess.TimeoutExpired:
            logger.error("docker restart timed out.")
            return False

        # Wait for GROBID to load its models and become available.
        # Models take ~60-90 seconds to load on first startup.
        logger.warning(
            "Waiting up to %ds for GROBID to restart...", wait_seconds
        )
        interval = 5
        elapsed  = 0
        while elapsed < wait_seconds:
            time.sleep(interval)
            elapsed += interval
            if self.is_alive():
                logger.warning(
                    "GROBID is back up after %ds. Resuming processing.", elapsed
                )
                return True
            logger.debug("GROBID not yet available (%ds elapsed)...", elapsed)

        logger.error(
            "GROBID did not come back up within %ds. "
            "Check container logs: docker logs %s",
            wait_seconds, self.container_name,
        )
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

        If GROBID returns [NO_BLOCKS], attempts ocrmypdf OCR and retries.
        If GROBID returns [BAD_INPUT_DATA] or the extracted text has high
        private-use Unicode density, attempts pdftoppm+Tesseract OCR and retries.

        Returns:
            TEI-XML string on success, or None if processing failed.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            logger.error("PDF file not found: %s", pdf_path)
            return None

        self.last_ocr_degraded = False
        tei = self._submit_to_grobid(pdf_path, include_coordinates)

        # ── [BAD_INPUT_DATA] fallback ──
        # GROBID's PDF parser crashed — try pdftoppm+Tesseract.
        if tei is None and self._last_bad_input:
            logger.warning(
                "[BAD_INPUT_DATA] for %s — attempting pdftoppm+Tesseract OCR.",
                pdf_path.name,
            )
            ocr_pdf = self._run_pdftoppm_tesseract(pdf_path, self.ocr_dir)
            if ocr_pdf:
                tei = self._submit_to_grobid(ocr_pdf, include_coordinates)
                if tei and not self._is_no_blocks(tei):
                    logger.info("pdftoppm+Tesseract OCR succeeded for %s", pdf_path.name)
                    return tei
            logger.error(
                "pdftoppm+Tesseract OCR failed for %s. PDF may be unrecoverable.",
                pdf_path.name,
            )
            return None

        if tei is None:
            return None

        # ── Private-use Unicode fallback ──
        # GROBID produced TEI but text is dominated by private-use Unicode
        # characters — font encoding failure. Try pdftoppm+Tesseract.
        if self._has_private_use_unicode(tei):
            logger.warning(
                "Private-use Unicode detected in TEI for %s — "
                "attempting pdftoppm+Tesseract OCR to bypass font mapping.",
                pdf_path.name,
            )
            ocr_pdf = self._run_pdftoppm_tesseract(pdf_path, self.ocr_dir)
            if ocr_pdf:
                tei2 = self._submit_to_grobid(ocr_pdf, include_coordinates)
                if tei2 and not self._is_no_blocks(tei2) and not self._has_private_use_unicode(tei2):
                    logger.info(
                        "pdftoppm+Tesseract OCR resolved font encoding failure for %s",
                        pdf_path.name,
                    )
                    return tei2
            logger.warning(
                "pdftoppm+Tesseract did not resolve font encoding failure for %s. "
                "Using original (garbled) TEI.",
                pdf_path.name,
            )
            self.last_ocr_degraded = True
            return tei  # Return garbled TEI — better than nothing

        # ── [NO_BLOCKS] fallback ──
        if not self._is_no_blocks(tei):
            logger.debug("GROBID processed successfully: %s", pdf_path.name)
            return tei

        ocr_pdf = self._run_ocr(pdf_path, self.ocr_dir)
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
                self._last_bad_input = False
                return resp.text
            elif resp.status_code == 500 and _NO_BLOCKS_MARKER in resp.text:
                self._last_bad_input = False
                return resp.text
            elif resp.status_code == 500 and _BAD_INPUT_MARKER in resp.text:
                self._last_bad_input = True
                logger.error(
                    "GROBID returned HTTP 500 for %s: %s",
                    pdf_path.name,
                    resp.text[:500],
                )
                return None
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
            # Attempt automatic restart and retry once.
            if self.restart_if_down():
                logger.warning("Retrying %s after GROBID restart...", pdf_path.name)
                return self._submit_to_grobid(pdf_path, include_coordinates)
            return None

    # =========================================================================
    # OCR fallback
    # =========================================================================

    @staticmethod
    def _is_no_blocks(tei_xml: str) -> bool:
        """Return True if the TEI response signals that GROBID found no text."""
        return _NO_BLOCKS_MARKER in tei_xml

    @staticmethod
    def _has_private_use_unicode(tei_xml: str, threshold: float = _PRIVATE_USE_THRESHOLD) -> bool:
        """
        Return True if the TEI text is dominated by private-use Unicode characters.

        Private-use Unicode (U+E000–U+F8FF) appears when GROBID extracts text
        from a PDF that uses a custom font with no standard Unicode mapping.
        The PDF looks correct visually but text extraction produces raw glyph
        codes rather than readable characters. When the fraction of such
        characters exceeds the threshold, we conclude font encoding has failed
        and attempt alternate OCR.
        """
        # Sample the first 5000 chars to avoid scanning huge TEI files
        sample = tei_xml[:5000]
        if not sample:
            return False
        private_use = sum(1 for c in sample if '\uE000' <= c <= '\uF8FF')
        return private_use / len(sample) >= threshold

    @staticmethod
    def _run_pdftoppm_tesseract(pdf_path: Path, ocr_dir: Path) -> "Path | None":
        """
        Render a PDF to images with pdftoppm and OCR with Tesseract.

        This approach bypasses the PDF's text layer entirely — rendering from
        pixels rather than extracting embedded text. It handles:
        - [BAD_INPUT_DATA]: PDFs that GROBID's parser cannot open at all
        - Font encoding failures: PDFs with custom fonts lacking Unicode maps

        The resulting text is assembled into a simple text file, then wrapped
        in a minimal PDF using a Tesseract PDF output mode. The result is
        submitted to GROBID as a new PDF with a proper text layer.

        Returns the path to the OCR'd PDF on success, or None on failure.
        """
        backup_dir = ocr_dir / "originals"
        backup_path = backup_dir / pdf_path.name
        alt_tag = pdf_path.stem + ".pdftoppm_ocr.pdf"
        alt_path = ocr_dir / alt_tag

        # If we've already done this, reuse the result
        if alt_path.exists():
            logger.debug("pdftoppm+Tesseract output already exists, reusing: %s", alt_tag)
            return alt_path

        if not shutil.which("pdftoppm"):
            logger.error("pdftoppm not found on PATH. Install poppler-utils.")
            return None
        if not shutil.which("tesseract"):
            logger.error("tesseract not found on PATH. Install tesseract-ocr.")
            return None

        logger.info("Running pdftoppm+Tesseract OCR on %s ...", pdf_path.name)

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # Step 1: Render PDF pages to images at 300 DPI
            img_prefix = str(tmp / "page")
            pdftoppm_cmd = [
                "pdftoppm",
                "-r", "300",       # 300 DPI — good balance of quality vs size
                "-png",            # PNG output
                str(pdf_path),
                img_prefix,
            ]
            result = subprocess.run(pdftoppm_cmd, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                logger.error(
                    "pdftoppm failed (exit %d) for %s: %s",
                    result.returncode, pdf_path.name,
                    (result.stderr or "(no output)").strip()[:300],
                )
                return None

            page_images = sorted(tmp.glob("page-*.png")) or sorted(tmp.glob("page*.png"))
            if not page_images:
                logger.error("pdftoppm produced no images for %s.", pdf_path.name)
                return None

            logger.debug("pdftoppm produced %d page images for %s", len(page_images), pdf_path.name)

            # Step 2: OCR each page with Tesseract, output as PDF
            # Use multiple languages likely in this corpus
            langs = "nor+swe+dan+deu+eng+fra+pol+ukr"
            page_pdfs = []
            for img in page_images:
                out_base = str(img.with_suffix(""))
                tess_cmd = [
                    "tesseract",
                    str(img),
                    out_base,
                    "-l", langs,
                    "--oem", "1",   # LSTM OCR engine
                    "--psm", "3",   # Fully automatic page segmentation
                    "pdf",          # Output as PDF with text layer
                ]
                result = subprocess.run(tess_cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    logger.debug(
                        "Tesseract failed for page %s of %s: %s",
                        img.name, pdf_path.name,
                        (result.stderr or "").strip()[:200],
                    )
                    continue
                page_pdf = img.with_suffix(".pdf")
                if page_pdf.exists():
                    page_pdfs.append(page_pdf)

            if not page_pdfs:
                logger.error("Tesseract produced no output for %s.", pdf_path.name)
                return None

            # Step 3: Merge page PDFs into one
            if len(page_pdfs) == 1:
                merged = page_pdfs[0]
            else:
                merged = tmp / "merged.pdf"
                if shutil.which("pdfunite"):
                    subprocess.run(
                        ["pdfunite"] + [str(p) for p in page_pdfs] + [str(merged)],
                        capture_output=True, timeout=120,
                    )
                elif shutil.which("gs"):
                    subprocess.run(
                        ["gs", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                         f"-sOutputFile={merged}"] + [str(p) for p in page_pdfs],
                        capture_output=True, timeout=120,
                    )
                else:
                    logger.error(
                        "Neither pdfunite nor gs available to merge page PDFs for %s. "
                        "Install poppler-utils or ghostscript.",
                        pdf_path.name,
                    )
                    return None

            if not merged.exists():
                logger.error("Failed to merge OCR'd pages for %s.", pdf_path.name)
                return None

            # Step 4: Copy result to ocr_dir
            ocr_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(merged), str(alt_path))

        logger.info(
            "pdftoppm+Tesseract OCR complete for %s → %s",
            pdf_path.name, alt_tag,
        )
        return alt_path

    @staticmethod
    def _run_ocr(pdf_path: Path, ocr_dir: Path) -> "Path | None":
        """
        Run ocrmypdf on pdf_path and write the result back to the original path.

        The original is moved to ocr_dir/originals/<filename> before being
        replaced, so it can be recovered if needed. If a backup already exists
        (from a previous run), the original is assumed to have already been
        replaced and the current file at pdf_path is used directly.

        Returns pdf_path on success (now pointing to the OCR'd version), or
        None if ocrmypdf failed or is not installed.
        """
        backup_dir = ocr_dir / "originals"
        backup_path = backup_dir / pdf_path.name

        if backup_path.exists():
            # Already processed on a previous run — pdf_path is already OCR'd.
            logger.debug("OCR backup already exists, reusing current file: %s", pdf_path.name)
            return pdf_path

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

        # Write OCR output to a temporary file first so we never leave
        # pdf_path in a half-written state if something goes wrong.
        tmp_path = pdf_path.with_suffix(".ocr_tmp.pdf")

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
            str(tmp_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            logger.error("ocrmypdf timed out after 300s for %s.", pdf_path.name)
            tmp_path.unlink(missing_ok=True)
            return None
        except FileNotFoundError:
            logger.error("ocrmypdf not found on PATH for %s.", pdf_path.name)
            return None

        if result.returncode not in (0, 5):
            logger.error(
                "ocrmypdf failed (exit %d) for %s:\n%s",
                result.returncode,
                pdf_path.name,
                (result.stderr or result.stdout or "(no output)").strip()[:500],
            )
            tmp_path.unlink(missing_ok=True)
            return None

        if not tmp_path.exists():
            logger.error("ocrmypdf produced no output for %s.", pdf_path.name)
            return None

        # Back up the original, then replace it with the OCR'd version.
        # Use Path.rename() rather than shutil.move() — on POSIX, rename() is
        # atomic when source and destination are on the same filesystem, closing
        # the window where pdf_path could be empty if the process is interrupted
        # between the two operations. shutil.move() is used as a fallback for
        # the backup step only, in case the Zotero directory and output/ are on
        # different volumes (cross-device rename raises OSError).
        backup_dir.mkdir(parents=True, exist_ok=True)
        try:
            pdf_path.rename(backup_path)
        except OSError:
            # Cross-device move — not atomic, but unavoidable in this case.
            shutil.move(str(pdf_path), str(backup_path))
        tmp_path.rename(pdf_path)  # same directory, always atomic

        logger.info(
            "OCR complete: %s (original backed up to output/ocr/originals/)",
            pdf_path.name,
        )
        return pdf_path

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