"""
bibvik.utils — Shared utilities for the BibVik toolkit.

Responsibilities:
- Configuration loading and validation
- Citekey generation (biblatex-style: lowercased first-author family name + year,
  with a/b/c disambiguation)
- JSON I/O with UTF-8 guarantees
- Logging setup
"""

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from unidecode import unidecode


# =============================================================================
# Configuration
# =============================================================================

def load_config(config_path: str = "config.yaml") -> dict:
    """
    Load and validate the YAML configuration file.

    Args:
        config_path: Path to the YAML config file.

    Returns:
        Dictionary of configuration values.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If required fields are missing.
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # --- Validate required fields ---
    required = ["seed_paper", "f1_pdf_dir"]
    for field in required:
        if field not in config or not config[field]:
            raise ValueError(
                f"Required config field '{field}' is missing or empty. "
                f"Please edit {config_path}."
            )

    # --- Apply defaults for optional fields ---
    config.setdefault("output_dir", "./output")
    config.setdefault("log_level", "INFO")
    config.setdefault("save_tei_xml", False)

    grobid = config.setdefault("grobid", {})
    grobid.setdefault("base_url", "http://localhost:8070")
    grobid.setdefault("timeout", 120)
    grobid.setdefault("concurrency", 1)
    grobid.setdefault("include_coordinates", False)

    llm = config.setdefault("llm", {})
    llm.setdefault("base_url", "http://localhost:11434")
    llm.setdefault("model", "qwen3:35b")
    llm.setdefault("temperature", 0.3)
    llm.setdefault("max_tokens", 2048)
    llm.setdefault("timeout", 300)

    ctx = config.setdefault("context", {})
    ctx.setdefault("sentence_window", 3)
    ctx.setdefault("boundary_threshold", 150)

    clustering = config.setdefault("clustering", {})
    clustering.setdefault("min_cooccurrence", 2)
    clustering.setdefault("run_content_enriched", True)

    return config


# =============================================================================
# Logging
# =============================================================================

def setup_logging(level: str = "INFO") -> logging.Logger:
    """
    Configure logging for the BibVik toolkit.

    User-facing messages (from the 'bibvik' logger used in run.py) are
    formatted cleanly without the module name. Internal library messages
    (from bibvik.* sub-loggers) are suppressed at INFO level and only
    shown at DEBUG level, keeping the terminal readable during normal runs.

    Format:
        INFO    →  HH:MM:SS  message
        WARNING →  HH:MM:SS  ⚠ message
        ERROR   →  HH:MM:SS  ✗ message

    Args:
        level: Logging level string (DEBUG, INFO, WARNING, ERROR).

    Returns:
        The configured root logger.
    """
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    class _BibVikFormatter(logging.Formatter):
        """Clean formatter: timestamp + level tag + message, left-justified."""

        def format(self, record: logging.LogRecord) -> str:
            time_str = self.formatTime(record, "%H:%M:%S")
            msg = record.getMessage()

            # Section headers embed a leading \n for visual separation.
            prefix = ""
            if msg.startswith("\n"):
                prefix = "\n"
                msg = msg.lstrip("\n")

            if record.levelno >= logging.ERROR:
                tag = "ERROR  "
                return f"{prefix}{time_str}  {tag}{msg}"
            elif record.levelno >= logging.WARNING:
                tag = "WARN   "
                return f"{prefix}{time_str}  {tag}{msg}"
            else:
                # Indented lines (continuations/details) suppress the timestamp
                # so the eye can track structure without repeating clock noise.
                if msg.startswith("  "):
                    return f"{prefix}{'':10}{msg}"
                return f"{prefix}{time_str}  {'':7}{msg}"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_BibVikFormatter())

    # Root 'bibvik' logger: user-facing, shown at the configured level.
    bibvik_log = logging.getLogger("bibvik")
    bibvik_log.setLevel(numeric_level)
    bibvik_log.addHandler(handler)
    bibvik_log.propagate = False

    # Sub-module loggers (bibvik.tei_parser, bibvik.citation_graph, etc.):
    # only shown at DEBUG level so internal detail stays out of normal runs.
    sub_level = logging.DEBUG if numeric_level <= logging.DEBUG else logging.WARNING
    for name in [
        "bibvik.tei_parser",
        "bibvik.citation_graph",
        "bibvik.pdf_processor",
        "bibvik.grobid_client",
        "bibvik.context_extractor",
        "bibvik.cluster_analyzer",
        "bibvik.reference_audit",
        "bibvik.reference_resolver",
        "bibvik.footnote_extractor",
        "bibvik.normalize",
        "bibvik.coverage",
        "bibvik.zotero_csv",
        "bibvik.llm_analyzer",
    ]:
        sub = logging.getLogger(name)
        sub.setLevel(sub_level)
        if not sub.handlers:
            sub_handler = logging.StreamHandler(sys.stdout)
            sub_handler.setFormatter(_BibVikFormatter())
            sub.addHandler(sub_handler)
        sub.propagate = False

    return bibvik_log


# =============================================================================
# Citekey Generation
# =============================================================================

# Registry to track assigned citekeys and handle disambiguation.
# Maps base keys (e.g., "doe2020") to a counter of how many times they've been
# used, so we can append a, b, c, etc.
_citekey_registry: dict[str, int] = {}


def reset_citekey_registry():
    """
    Clear the citekey registry. Call this when starting a fresh extraction
    session to avoid stale disambiguation state.
    """
    global _citekey_registry
    _citekey_registry = {}


def generate_citekey(authors: list[dict], year: str | None) -> str:
    """
    Generate a biblatex-style citekey from author metadata and publication year.

    Format: {first_author_family_lowercase}{year}
    Disambiguation: a, b, c... appended when base key collides.

    Non-ASCII characters in the author's family name are transliterated to ASCII
    for the citekey (e.g., "Müller" → "muller", "Иванов" → "ivanov"), while the
    original Unicode name is preserved in the bibliographic record.

    Args:
        authors: List of author dicts, each with 'family' and optionally 'given'.
                 Example: [{"family": "Müller", "given": "Hans"}]
        year:    Publication year as a string, e.g., "2020". May be None.

    Returns:
        A unique citekey string, e.g., "muller2020" or "muller2020a".
    """
    # --- Extract and normalize the first author's family name ---
    if authors and authors[0].get("family"):
        # Transliterate to ASCII, lowercase, strip non-alphanumeric
        family = unidecode(authors[0]["family"]).lower()
        family = re.sub(r"[^a-z]", "", family)
    else:
        family = "unknown"

    # --- Normalize year ---
    year_str = str(year).strip() if year else "nd"  # "nd" = no date

    # --- Build base key ---
    base_key = f"{family}{year_str}"

    # --- Disambiguate ---
    if base_key not in _citekey_registry:
        # First occurrence: register it and return without suffix.
        _citekey_registry[base_key] = 1
        return base_key
    else:
        # Collision: increment counter and append suffix letter.
        # On the second occurrence, we also need to retroactively note that
        # the first use should logically be "base_key" (no suffix) — but since
        # citekeys are assigned sequentially, we keep the first one as-is and
        # start suffixing from the second.
        count = _citekey_registry[base_key]
        _citekey_registry[base_key] = count + 1
        # count=1 means one already exists, so this is the 2nd → suffix 'a'
        # count=2 → 'b', etc.
        # We use 0-indexed: suffix = chr(ord('a') + count - 1)
        suffix = chr(ord("a") + count - 1)
        return f"{base_key}{suffix}"


# =============================================================================
# JSON I/O
# =============================================================================

def write_json(data: Any, path: str | Path, indent: int = 2) -> None:
    """
    Write data to a JSON file with UTF-8 encoding and no ASCII escaping.

    This ensures that non-Latin characters (Greek, Cyrillic, CJK, diacritics,
    etc.) are preserved as-is in the output, rather than being escaped to
    \\uXXXX sequences.

    Args:
        data:   Any JSON-serializable Python object.
        path:   Destination file path.
        indent: Indentation level for pretty-printing.
    """
    filepath = Path(path)
    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def read_json(path: str | Path) -> Any:
    """
    Read a JSON file with UTF-8 encoding.

    Args:
        path: Source file path.

    Returns:
        Parsed JSON data.

    Raises:
        FileNotFoundError: If the file does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    filepath = Path(path)
    if not filepath.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")

    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# Miscellaneous
# =============================================================================

def collect_pdfs(directory: str | Path, exclude: str | Path | None = None) -> list[Path]:
    """
    Collect all PDF files in a directory (non-recursive).

    Args:
        directory: Path to the directory to scan.
        exclude:   Optional path to a PDF to exclude (e.g., the seed paper,
                   which is processed separately).

    Returns:
        Sorted list of Path objects for each PDF found.
    """
    dirpath = Path(directory)
    if not dirpath.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")

    exclude_resolved = Path(exclude).resolve() if exclude else None

    pdfs = []
    for p in sorted(dirpath.glob("*.pdf")):
        if exclude_resolved and p.resolve() == exclude_resolved:
            continue
        pdfs.append(p)

    return pdfs
