"""
bibvik.llm_analyzer — LLM-based citation function analysis via Ollama.

This module uses a local LLM (qwen3:35b via Ollama) to perform qualitative
analysis on citation contexts. It handles two tasks:

1. **Citation function classification**: For each citation context, the LLM
   reads the verbatim text and classifies the function/quality of the citation.
   Rather than imposing a rigid taxonomy, we let the LLM reason about the
   citation's role and assign both a short label and an explanation.

2. **Content-enriched analysis**: When the cited paper's text is available
   (i.e., we have its PDF and extracted its abstract/introduction), we include
   that information in the prompt so the LLM can assess whether the citing
   author's characterization of the work is faithful, selective, or reframing.

All prompts are defined as constants in this module so they can be reviewed,
customized, or translated without modifying the logic.

Ollama API:
    We use Ollama's REST API at /api/generate (or /api/chat for chat-style
    models). The API expects JSON with 'model', 'prompt' (or 'messages'),
    and optional 'options' for temperature/token limits. Responses stream
    by default; we use stream=False for simplicity.
"""

import json
import logging

import requests
from unidecode import unidecode

logger = logging.getLogger(__name__)


# =============================================================================
# Prompt templates
# =============================================================================

# Citation function classification prompt.
# The LLM receives the verbatim context and must classify the citation's role.
#
# We deliberately avoid providing a closed list of categories because citation
# functions are nuanced and domain-specific. Instead, we give examples of
# common functions and ask the LLM to reason about the specific case.
CITATION_FUNCTION_PROMPT = """You are an expert in academic citation analysis. Your task is to analyze how a cited work is being used in its citing context.

## Citation context

The following is a passage from an academic paper. The citation of interest is marked with [TARGET CITATION]. Other cited works may appear in the same passage.

---
{context_text}
---

The target citation refers to: "{cited_title}" by {cited_authors} ({cited_year}).

## Task

Analyze the function of this citation. Consider these common citation functions as a starting point, but do not limit yourself to them:

- **Evidential support**: The cited work provides data, findings, or evidence that supports the citing author's claim.
- **Methodological basis**: The citing author adopts or adapts a method, framework, or tool from the cited work.
- **Theoretical framing**: The cited work provides a theoretical lens, concept, or model that frames the citing discussion.
- **Background/context**: The cited work is referenced to establish disciplinary context, prior knowledge, or the state of the art.
- **Contrast/critique**: The citing author disagrees with, qualifies, or contrasts their work against the cited work.
- **Extension/building-upon**: The citing author explicitly builds on, extends, or refines the cited work.
- **Example/illustration**: The cited work is used as an example or case study to illustrate a point.
- **Attribution**: A concept, term, or finding is attributed to the cited work without further elaboration.
- **Gap identification**: The cited work (or body of work it represents) is referenced to identify a research gap.

Respond in JSON format with exactly these fields:
{{
  "citation_function": "<short label, 1-3 words>",
  "citation_function_explanation": "<2-4 sentences explaining how and why this work is cited in this specific context>",
  "confidence": "<high/medium/low>"
}}

Respond ONLY with the JSON object. No preamble, no markdown fences."""



# Footnote bibliographic reference extraction prompt.
# The LLM receives the raw footnote text and must identify all distinct
# bibliographic references embedded in it, returning structured metadata.
FOOTNOTE_EXTRACTION_PROMPT = """You are an expert bibliographer specializing in humanities scholarship. Your task is to extract structured bibliographic references from a footnote in an academic paper.

## Footnote text

---
{footnote_text}
---

## Task

Identify every distinct bibliographic reference in this footnote. Footnotes in humanities papers often:
- Cite multiple works in a single footnote
- Mix prose commentary with bibliographic details
- Use abbreviations like "ed.", "eds.", "vol.", "pp.", "ibid.", "op. cit."
- Cite works in multiple languages (English, Norwegian, Swedish, Danish, German, French)
- Reference journal articles, edited volumes, book chapters, monographs, or grey literature

For EACH distinct reference you find, extract as many of the following fields as the text provides. Omit fields that are not present in the text — do not guess or fabricate values.

Fields:
- "title": Title of the article, chapter, or book
- "author": List of {{"family": "...", "given": "..."}} dicts (use initials for given if that's all that's available)
- "editor": List of editor name dicts (for edited volumes / book chapters)
- "date": Publication year (4-digit string, e.g. "2007")
- "journaltitle": Journal name (for articles)
- "booktitle": Book or volume title (for chapters in edited volumes)
- "volume": Volume number
- "number": Issue number
- "pages": Page range (e.g. "59-74" or "59--74")
- "publisher": Publisher name
- "location": Place of publication
- "series": Series title
- "doi": DOI if present
- "url": URL if present
- "entry_type": One of "article", "incollection", "book", "misc"
- "raw_text": The exact portion of the footnote text that describes this reference

Respond ONLY with a JSON array of reference objects. If there are no bibliographic references (the footnote is purely prose commentary), return an empty array [].

No preamble, no markdown fences, no explanation — just the JSON array."""
# Extends the basic analysis by including information about the cited paper.
CONTENT_ENRICHED_PROMPT = """You are an expert in academic citation analysis. Your task is to analyze how a cited work is being used in its citing context, and to assess whether the citing author's characterization is faithful to the cited work.

## Citation context (from the citing paper)

---
{context_text}
---

## Information about the cited work

Title: "{cited_title}" by {cited_authors} ({cited_year})

Abstract/summary of the cited work:
---
{cited_abstract}
---

## Task

1. Classify the function of this citation (see categories below).
2. Assess whether the citing author's use of the cited work is:
   - **Faithful**: Accurately represents the cited work's content/findings.
   - **Selective**: Highlights certain aspects while omitting others.
   - **Reframing**: Reinterprets or recontextualizes the cited work.
   - **Superficial**: Cites the work without engaging substantively with its content.

Common citation functions (use as starting points, not a closed list):
Evidential support, Methodological basis, Theoretical framing, Background/context,
Contrast/critique, Extension/building-upon, Example/illustration, Attribution,
Gap identification.

Respond in JSON format:
{{
  "citation_function": "<short label>",
  "citation_function_explanation": "<2-4 sentences>",
  "characterization_assessment": "<faithful/selective/reframing/superficial>",
  "characterization_explanation": "<2-3 sentences explaining the assessment>",
  "confidence": "<high/medium/low>"
}}

Respond ONLY with the JSON object. No preamble, no markdown fences."""


# =============================================================================
# Ollama client
# =============================================================================

class LLMAnalyzer:
    """
    Interface to a local LLM for citation analysis.

    Supports two backends:
    - "ollama": Ollama API at /api/generate (default, for local development)
    - "llama_server": llama.cpp server OpenAI-compatible API at /v1/chat/completions
      (preferred for performance; use on GPU cluster)

    Set backend in config.yaml under llm.backend.

    Usage:
        analyzer = LLMAnalyzer(base_url="http://localhost:11434", model="qwen3:35b")
        if analyzer.is_available():
            result = analyzer.classify_citation_function(context, ref_info)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3:35b",
        temperature: float = 0.3,
        max_tokens: int = 2048,
        timeout: int = 300,
        backend: str = "ollama",
    ):
        """
        Args:
            base_url:    LLM API base URL.
            model:       Model name.
            temperature: Sampling temperature. Lower = more deterministic.
            max_tokens:  Maximum response tokens.
            timeout:     Request timeout in seconds.
            backend:     "ollama" or "llama_server".
        """
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.backend = backend.lower()
        if self.backend not in ("ollama", "llama_server"):
            raise ValueError(f"Unknown LLM backend: {backend!r}. Use 'ollama' or 'llama_server'.")

    def is_available(self) -> bool:
        """Check whether the LLM backend is running and accessible."""
        if self.backend == "llama_server":
            return self._is_available_llama_server()
        return self._is_available_ollama()

    def _is_available_ollama(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=10)
            if resp.status_code != 200:
                logger.error("Ollama API returned status %d.", resp.status_code)
                return False

            models = resp.json().get("models", [])
            model_names = [m.get("name", "") for m in models]

            for name in model_names:
                if name.startswith(self.model) or self.model.startswith(name.split(":")[0]):
                    return True

            logger.error(
                "Model '%s' not found in Ollama. Available: %s. "
                "Run `ollama pull %s` to download it.",
                self.model,
                ", ".join(model_names),
                self.model,
            )
            return False

        except requests.ConnectionError:
            logger.error(
                "Cannot connect to Ollama at %s. Is it running? Try: ollama serve",
                self.base_url,
            )
            return False

    def _is_available_llama_server(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/health", timeout=10)
            if resp.status_code == 200:
                return True
            logger.error("llama-server health check returned status %d.", resp.status_code)
            return False
        except requests.ConnectionError:
            logger.error(
                "Cannot connect to llama-server at %s. Is it running?",
                self.base_url,
            )
            return False

    def classify_citation_function(
        self,
        context_text: str,
        cited_title: str,
        cited_authors: str,
        cited_year: str,
    ) -> dict | None:
        """
        Classify the function of a citation using the LLM.

        Sends the citation context and reference metadata to the LLM,
        which returns a structured classification.

        Args:
            context_text:  Verbatim citation context (from context_extractor).
            cited_title:   Title of the cited work.
            cited_authors: Authors of the cited work (formatted as a string).
            cited_year:    Publication year of the cited work.

        Returns:
            Dict with 'citation_function', 'citation_function_explanation',
            and 'confidence' keys, or None if the LLM call failed.
        """
        prompt = CITATION_FUNCTION_PROMPT.format(
            context_text=context_text,
            cited_title=cited_title,
            cited_authors=cited_authors,
            cited_year=cited_year,
        )

        return self._query_llm(prompt)

    def classify_citation_with_content(
        self,
        context_text: str,
        cited_title: str,
        cited_authors: str,
        cited_year: str,
        cited_abstract: str,
    ) -> dict | None:
        """
        Content-enriched citation function classification.

        Like classify_citation_function, but includes the cited paper's
        abstract so the LLM can assess whether the citing author's
        characterization is faithful to the original work.

        Args:
            context_text:   Verbatim citation context.
            cited_title:    Title of the cited work.
            cited_authors:  Authors string.
            cited_year:     Year string.
            cited_abstract: Abstract or summary of the cited work.

        Returns:
            Dict with classification fields plus 'characterization_assessment'
            and 'characterization_explanation', or None on failure.
        """
        prompt = CONTENT_ENRICHED_PROMPT.format(
            context_text=context_text,
            cited_title=cited_title,
            cited_authors=cited_authors,
            cited_year=cited_year,
            cited_abstract=cited_abstract or "Not available.",
        )

        return self._query_llm(prompt)

    def extract_references_from_footnote(
        self,
        footnote_text: str,
    ) -> list[dict] | None:
        """
        Extract structured bibliographic references from a footnote using the LLM.

        This handles the humanities convention where full bibliographic details
        appear in footnotes rather than a separate bibliography section. GROBID
        handles this poorly; the LLM is much better suited to reading
        semi-structured prose and extracting metadata fields from it.

        Args:
            footnote_text: Raw text content of a single footnote.

        Returns:
            List of reference dicts (possibly empty if no references found),
            or None if the LLM call failed entirely.
        """
        prompt = FOOTNOTE_EXTRACTION_PROMPT.format(footnote_text=footnote_text)
        raw_result = self._query_llm_raw(prompt)
        if raw_result is None:
            return None
        return _parse_llm_json_array(raw_result)

    def _query_llm_raw(self, prompt: str) -> str | None:
        """
        Send a prompt to the LLM and return the raw response text (after
        stripping think tags), without attempting JSON parsing.

        Routes to Ollama's /api/generate or llama-server's
        /v1/chat/completions depending on self.backend.
        """
        import re

        if self.backend == "llama_server":
            return self._query_llama_server(prompt, re)
        return self._query_ollama(prompt, re)

    def _query_ollama(self, prompt: str, re) -> str | None:
        """Query via Ollama /api/generate."""
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }

        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                logger.error("Ollama returned status %d: %s", resp.status_code, resp.text[:500])
                return None

            data = resp.json()
            response_text = data.get("response", "")

            response_text = re.sub(r"<think>[\s\S]*?</think>", "", response_text).strip()
            response_text = re.sub(r"<think>[\s\S]*$", "", response_text).strip()

            if not response_text:
                logger.warning("LLM response was empty after stripping think tags.")
                return None

            return response_text

        except requests.Timeout:
            logger.error("Ollama request timed out after %ds.", self.timeout)
            return None
        except requests.ConnectionError:
            logger.error("Lost connection to Ollama.")
            return None
        except Exception as e:
            logger.error("Unexpected error querying Ollama: %s", e)
            return None

    def _query_llama_server(self, prompt: str, re) -> str | None:
        """
        Query via llama-server's OpenAI-compatible /v1/chat/completions.

        llama-server uses the OpenAI chat completions format rather than
        Ollama's /api/generate format. The prompt is sent as a user message.
        Temperature and max_tokens map directly to OpenAI parameter names.
        """
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        try:
            resp = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                logger.error("llama-server returned status %d: %s", resp.status_code, resp.text[:500])
                return None

            data = resp.json()
            choices = data.get("choices", [])
            if not choices:
                logger.warning("llama-server returned no choices.")
                return None

            response_text = choices[0].get("message", {}).get("content", "")

            # Strip thinking blocks — some models emit <think>...</think> regardless
            # of backend.
            response_text = re.sub(r"<think>[\s\S]*?</think>", "", response_text).strip()
            response_text = re.sub(r"<think>[\s\S]*$", "", response_text).strip()

            if not response_text:
                logger.warning("llama-server response was empty after stripping think tags.")
                return None

            return response_text

        except requests.Timeout:
            logger.error("llama-server request timed out after %ds.", self.timeout)
            return None
        except requests.ConnectionError:
            logger.error("Lost connection to llama-server at %s.", self.base_url)
            return None
        except Exception as e:
            logger.error("Unexpected error querying llama-server: %s", e)
            return None


        """
        Send a prompt to Ollama and parse the JSON response.

        Uses the /api/generate endpoint with stream=False for simplicity.
        The response is expected to be a JSON object.

        Note on thinking models (qwen3, qwen3.5, etc.):
            These models default to "thinking mode" where they emit a
            <think>...</think> block before the actual response. We handle
            this in two ways:
            1. Pass /no_think in the prompt suffix to request no thinking
               (supported by qwen3+ models).
            2. Strip <think>...</think> blocks from the response as a
               fallback in case the model ignores the directive.

        Error handling:
        - Connection errors: logged and None returned.
        - Non-JSON response: we attempt to extract JSON from the text.
        - Timeout: logged and None returned.
        """
        # Disable thinking mode for qwen3/3.5 and other reasoning models.
        # The Ollama API supports a top-level "think" parameter: when set to
        # false, the model skips its internal reasoning chain and responds
        # directly. This dramatically reduces latency and token usage for
        # structured-output tasks like ours where we just need JSON.
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
            },
        }

        try:
            resp = requests.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=self.timeout,
            )

            if resp.status_code != 200:
                logger.error("Ollama returned status %d: %s", resp.status_code, resp.text[:500])
                return None

            data = resp.json()
            response_text = data.get("response", "")

            # --- Parse the LLM's JSON response ---
            return _parse_llm_json(response_text)

        except requests.Timeout:
            logger.error("Ollama request timed out after %ds.", self.timeout)
            return None
        except requests.ConnectionError:
            logger.error("Lost connection to Ollama.")
            return None
        except Exception as e:
            logger.error("Unexpected error querying Ollama: %s", e)
            return None


def analyze_all_contexts(
    contexts: dict[str, list[dict]],
    bibliography: dict[str, dict],
    analyzer: LLMAnalyzer,
    content_enriched: bool = False,
    limit: int | None = None,
    processed_papers: dict[str, dict] | None = None,
) -> dict[str, list[dict]]:
    """
    Run LLM analysis on all extracted citation contexts.

    For each context, sends it to the LLM for function classification and
    adds the results to the context record.

    Args:
        contexts:           Dict mapping citekeys to lists of context records
                            (from context_extractor.extract_all_contexts).
        bibliography:       The full bibliography dict.
        analyzer:           An initialized LLMAnalyzer instance.
        content_enriched:   If True, include the cited paper's actual content
                            in prompts so the LLM can assess how faithfully
                            the citing author represents the cited work.
        limit:              If set, only analyze this many contexts with the LLM.
                            All contexts are still present in the output; only
                            LLM classification is capped.
        processed_papers:   Dict mapping PDF filenames to their processed data
                            (from CitationGraph.get_processed_papers()). Required
                            for content-enriched analysis — provides the actual
                            body text of cited papers when we have their PDFs.

    Returns:
        The updated contexts dict with LLM analysis results added to each
        context record.
    """
    # --- Build a lookup from citekey → paper content for enriched mode ---
    # For each cited work whose PDF we have processed, extract a substantial
    # content summary from the actual paper text (abstract + intro paragraphs).
    citekey_to_content: dict[str, str] = {}
    if content_enriched and processed_papers:
        citekey_to_content = _build_content_lookup(bibliography, processed_papers)
        # Count how many distinct cited works appear in contexts.
        total_cited_works = len(contexts)
        logger.info(
            "Content-enriched mode: found paper text for %d of %d cited works.",
            len(citekey_to_content),
            total_cited_works,
        )

    # Count total contexts.
    total = sum(len(ctxs) for ctxs in contexts.values())
    effective_total = min(total, limit) if limit else total
    logger.info(
        "  Analyzing %d citation contexts%s",
        effective_total,
        f" (of {total} total)" if limit and limit < total else "",
    )

    analyzed_count = 0
    log_every = max(1, effective_total // 4)  # log ~4 progress updates

    for cited_citekey, ctx_list in contexts.items():
        # Look up the cited work's metadata.
        cited_entry = bibliography.get(cited_citekey, {})
        cited_title = cited_entry.get("title", "Unknown")
        cited_authors = _format_authors(cited_entry.get("author", []))
        cited_year = cited_entry.get("year", "n.d.")

        # For content-enriched analysis, get the actual paper content.
        cited_content = citekey_to_content.get(cited_citekey, "")

        for ctx in ctx_list:
            # Check if we've hit the limit.
            if limit and analyzed_count >= limit:
                break

            verbatim = ctx.get("verbatim_text", "")
            if not verbatim:
                continue

            # --- Call LLM: use enriched prompt if content available ---
            if content_enriched and cited_content:
                result = analyzer.classify_citation_with_content(
                    context_text=verbatim,
                    cited_title=cited_title,
                    cited_authors=cited_authors,
                    cited_year=cited_year,
                    cited_abstract=cited_content,
                )
                ctx["analysis_mode"] = "content_enriched"
            else:
                result = analyzer.classify_citation_function(
                    context_text=verbatim,
                    cited_title=cited_title,
                    cited_authors=cited_authors,
                    cited_year=cited_year,
                )
                ctx["analysis_mode"] = "context_only"

            if result:
                ctx["citation_function"] = result.get("citation_function", "unknown")
                ctx["citation_function_explanation"] = result.get(
                    "citation_function_explanation", ""
                )
                ctx["confidence"] = result.get("confidence", "low")

                # Content-enriched fields (only present when that prompt was used).
                if "characterization_assessment" in result:
                    ctx["characterization_assessment"] = result["characterization_assessment"]
                    ctx["characterization_explanation"] = result.get(
                        "characterization_explanation", ""
                    )
            else:
                ctx["citation_function"] = "analysis_failed"
                ctx["citation_function_explanation"] = "LLM analysis did not return a result."
                ctx["confidence"] = "none"

            analyzed_count += 1
            if analyzed_count % log_every == 0 or analyzed_count == effective_total:
                logger.info("  %d / %d contexts analyzed", analyzed_count, effective_total)

        # Break outer loop if limit reached.
        if limit and analyzed_count >= limit:
            break

    return contexts


# =============================================================================
# Internal helpers
# =============================================================================

def _build_content_lookup(
    bibliography: dict[str, dict],
    processed_papers: dict[str, dict],
) -> dict[str, str]:
    """
    Build a mapping from citekey → substantial text content of the cited paper.

    For each bibliography entry that has a corresponding processed PDF (i.e.,
    we have the paper's actual text), extract a meaningful content summary by
    combining:
    1. The abstract (if GROBID extracted one).
    2. The first N paragraphs of the body text (typically the introduction),
       up to a character limit.

    This content is what the LLM receives in the enriched analysis prompt,
    allowing it to assess whether the citing author's characterization is
    faithful to the cited work's actual content.

    Matching strategy:
        We need to link processed PDFs (keyed by filename) to bibliography
        entries (keyed by citekey). There are two paths:
        1. The bibliography entry has _source_pdf set (from successful F1
           matching) — direct lookup.
        2. The processed paper created its own bibliography entry (when
           matching failed) — we find it by matching the PDF filename
           against all entries' _source_pdf fields.

    Args:
        bibliography:     The full bibliography dict.
        processed_papers: Dict mapping PDF filenames → processed paper data,
                          each containing 'header' and 'paragraphs'.

    Returns:
        Dict mapping citekeys → content strings for papers we have PDFs for.
    """
    import re

    # Build a reverse lookup: source_pdf filename → citekey.
    # This captures ALL bibliography entries that have a _source_pdf,
    # including both successfully matched F1 papers and unmatched ones
    # that got their own new entries.
    pdf_to_citekey: dict[str, str] = {}
    for citekey, entry in bibliography.items():
        source_pdf = entry.get("_source_pdf", "")
        if source_pdf:
            pdf_to_citekey[source_pdf] = citekey

    content_lookup: dict[str, str] = {}

    # Maximum characters of body text to include. The LLM prompt has limited
    # context, and the intro/first sections are usually the most informative
    # about the paper's purpose and contributions.
    MAX_CONTENT_CHARS = 3000

    for pdf_name, paper_data in processed_papers.items():
        citekey = pdf_to_citekey.get(pdf_name)
        if not citekey:
            # This processed paper has no matching bibliography entry at all.
            # Skip it — we can't link its content to any citation context.
            logger.debug(
                "No bibliography entry found for processed PDF: %s", pdf_name
            )
            continue

        parts = []

        # --- Abstract ---
        header = paper_data.get("header", {})
        abstract = header.get("abstract", "")
        if abstract:
            parts.append(f"Abstract: {abstract}")

        # --- Body text (first N paragraphs) ---
        paragraphs = paper_data.get("paragraphs", [])
        body_chars = 0
        for para in paragraphs:
            para_text = para.get("text", "")
            # Strip citation placeholders for cleaner content.
            para_text = re.sub(r"\{\{CITE:\w*\}\}", "", para_text).strip()
            para_text = re.sub(r"  +", " ", para_text)

            if not para_text or len(para_text) < 30:
                # Skip very short fragments (likely headers, captions, etc.)
                continue

            parts.append(para_text)
            body_chars += len(para_text)

            if body_chars >= MAX_CONTENT_CHARS:
                break

        if parts:
            content_lookup[citekey] = "\n\n".join(parts)

    # --- Second pass: fuzzy-match unmatched processed papers to context citekeys ---
    # When F1 matching failed, the same work may exist under two citekeys:
    # one from the seed paper's bibliography, and one created when the F1 PDF
    # was processed. The citation contexts reference the seed paper's citekey,
    # but the content is keyed to the new citekey. We bridge this gap by
    # matching on title similarity.
    #
    # Build a title→content map from what we already have, then check each
    # bibliography entry that lacks content.

    def _norm(s):
        """Normalize a title for fuzzy comparison."""
        s = unidecode(s).lower()
        s = re.sub(r"[^\w\s]", "", s)
        return re.sub(r"\s+", " ", s).strip()

    # Map normalized titles to content strings.
    title_to_content: dict[str, str] = {}
    for ck, content in content_lookup.items():
        entry = bibliography.get(ck, {})
        title = _norm(entry.get("title", ""))
        if title and len(title) >= 20:
            title_to_content[title] = content

    # For each bibliography entry NOT yet in content_lookup, try title match.
    for citekey, entry in bibliography.items():
        if citekey in content_lookup:
            continue
        title = _norm(entry.get("title", ""))
        if title and title in title_to_content:
            content_lookup[citekey] = title_to_content[title]

    return content_lookup

def _format_authors(authors: list[dict]) -> str:
    """
    Format a list of author dicts into a readable string.

    Example: [{"family": "Smith", "given": "J."}, {"family": "Doe", "given": "A."}]
    → "J. Smith and A. Doe"
    """
    if not authors:
        return "Unknown"

    parts = []
    for a in authors:
        given = a.get("given", "")
        family = a.get("family", "")
        if given and family:
            parts.append(f"{given} {family}")
        elif family:
            parts.append(family)
        elif given:
            parts.append(given)

    if len(parts) == 0:
        return "Unknown"
    elif len(parts) == 1:
        return parts[0]
    elif len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    else:
        return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _parse_llm_json(text: str) -> dict | None:
    """
    Parse JSON from LLM response text.

    LLMs sometimes wrap JSON in markdown code fences, include preamble text,
    or (in the case of qwen3/3.5 "thinking" models) emit a <think>...</think>
    reasoning block before the actual response. We handle all of these.

    Strategies (tried in order):
    0. Strip <think>...</think> blocks (qwen3+ thinking mode).
    1. Direct JSON parse.
    2. Strip markdown fences and parse.
    3. Find the first { ... } block and parse.
    """
    # --- Strategy 0: Strip thinking blocks ---
    # qwen3.5 and similar models emit <think>reasoning</think> before the
    # actual response. The JSON we want comes after the closing tag.
    import re
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

    # Also handle unclosed think tags (model hit token limit mid-thought).
    text = re.sub(r"<think>[\s\S]*$", "", text).strip()

    if not text:
        logger.warning("LLM response was empty after stripping think tags.")
        return None

    # --- Strategy 1: Direct parse ---
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # --- Strategy 2: Strip markdown fences ---
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    # --- Strategy 3: Find first JSON object ---
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse LLM response as JSON: %s", text[:200])
    return None


def _parse_llm_json_array(text: str) -> list[dict] | None:
    """
    Parse a JSON array from LLM response text.

    Like _parse_llm_json but expects a list at the top level rather than
    a dict. Used for footnote extraction, where the LLM returns an array
    of reference objects.

    Returns:
        List of dicts, or None if parsing failed entirely.
    """
    import re

    # Strip markdown fences if present.
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Strategy 1: direct parse.
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        # Sometimes the LLM wraps the array in an object.
        if isinstance(result, dict):
            for val in result.values():
                if isinstance(val, list):
                    return val
    except json.JSONDecodeError:
        pass

    # Strategy 2: find first [...] block.
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            result = json.loads(match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    logger.warning("Could not parse LLM array response: %s", text[:200])
    return None