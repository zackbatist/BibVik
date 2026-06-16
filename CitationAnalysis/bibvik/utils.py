"""
bibvik.utils — Shared utilities.

Responsibilities:
- Configuration loading and validation
- Citekey generation (lastnameyear with a/b/c disambiguation)
- JSON I/O with UTF-8 guarantees
- Logging setup
- Graceful cancellation (SIGINT handler that writes partial state)
"""

import json
import logging
import re
import signal
import sys
from pathlib import Path
from typing import Any, Callable

import yaml
from unidecode import unidecode


# =============================================================================
# Configuration
# =============================================================================

def load_config(config_path: str = "config.yaml") -> dict:
    """Load and validate the YAML configuration file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    required = ["seed_paper", "f1_pdf_dir"]
    for field in required:
        if field not in config or not config[field]:
            raise ValueError(f"Required config field '{field}' is missing. Edit {config_path}.")

    # Defaults
    config.setdefault("output_dir", "./output")
    config.setdefault("log_level", "INFO")
    config.setdefault("save_tei_xml", True)

    g = config.setdefault("grobid", {})
    g.setdefault("base_url", "http://localhost:8070")
    g.setdefault("timeout", 120)
    g.setdefault("container_name", "grobid-server")

    llm = config.setdefault("llm", {})
    llm.setdefault("base_url", "http://localhost:11434")
    llm.setdefault("backend", "ollama")
    llm.setdefault("model", "qwen3.5:35b")
    llm.setdefault("detection_model", "")
    llm.setdefault("detection_batch_size", 4)
    llm.setdefault("temperature", 0.3)
    llm.setdefault("max_tokens", 2048)
    llm.setdefault("timeout", 300)
    llm.setdefault("extra_urls", [])  # Additional LLM endpoints for multi-GPU

    ctx = config.setdefault("context", {})
    ctx.setdefault("sentence_window", 3)
    ctx.setdefault("boundary_threshold", 150)

    cl = config.setdefault("clustering", {})
    cl.setdefault("min_cooccurrence", 2)
    cl.setdefault("run_content_enriched", True)

    return config


# =============================================================================
# Logging
# =============================================================================

# =============================================================================
# Logging
# =============================================================================

def setup_logging(level: str = "INFO", log_file: Path | None = None) -> logging.Logger:
    """
    Configure logging with two handlers:

    - File handler (DEBUG): full detail, timestamps, module names.
      Written to log_file if provided (default: output/bibvik.log).
      This is the complete record for post-run inspection.

    - Stream handler (WARNING+): only warnings and errors to stdout.
      Clean terminal output during runs comes from explicit print() calls,
      not from INFO log messages. This avoids the noisy timestamp+module
      prefix on every line of terminal output.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove any existing handlers (e.g. from basicConfig calls)
    root.handlers.clear()

    # ── File handler: full detail ─────────────────────────────────────────────
    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
        root.addHandler(fh)

    # ── Stream handler: warnings and errors only ──────────────────────────────
    # INFO and DEBUG go to the log file only; terminal output is via print().
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(sh)

    return logging.getLogger("bibvik")


# =============================================================================
# Citekey generation
# =============================================================================

_citekey_registry: dict[str, int] = {}


def reset_citekey_registry():
    """Clear citekey disambiguation state."""
    global _citekey_registry
    _citekey_registry = {}


def generate_citekey(authors: list[dict], year: str | None, editors: list[dict] | None = None) -> str:
    """
    Generate a biblatex-style citekey with disambiguation.

    Normal case (author present): lastnameyear with a/b/c suffix
      e.g. sindbæk2022 → sindbaek2022, sindbaek2022a, sindbaek2022b

    No author, editor present: first editor's surname + year
      e.g. ahola2014, barrett2012

    No author, no editor: NOAUTHOR with sequential number
      e.g. NOAUTHOR1, NOAUTHOR2, NOAUTHOR3

    Non-ASCII is transliterated for the key but preserved in the record.
    """
    # Fall back to editors if no author
    name_source = authors if (authors and authors[0].get("family")) else (editors or [])

    if name_source and name_source[0].get("family"):
        family = unidecode(name_source[0]["family"]).lower()
        family = re.sub(r"[^a-z]", "", family)
        year_str = str(year).strip()[:4] if year else "nd"
        base = f"{family}{year_str}"

        if base not in _citekey_registry:
            _citekey_registry[base] = 1
            return base
        else:
            count = _citekey_registry[base]
            _citekey_registry[base] = count + 1
            if count <= 26:
                suffix = chr(ord("a") + count - 1)
            else:
                first = chr(ord("a") + (count - 27) // 26)
                second = chr(ord("a") + (count - 27) % 26)
                suffix = first + second
            return f"{base}{suffix}"
    else:
        # No author or editor — sequential NOAUTHOR key
        n = _citekey_registry.get("__noauthor__", 0) + 1
        _citekey_registry["__noauthor__"] = n
        return f"NOAUTHOR{n}"


# =============================================================================
# JSON I/O
# =============================================================================

def write_json(data: Any, path: str | Path, indent: int = 2) -> None:
    """Write JSON with UTF-8 encoding, no ASCII escaping."""
    filepath = Path(path)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def read_json(path: str | Path) -> Any:
    """Read a JSON file with UTF-8 encoding."""
    with open(Path(path), "r", encoding="utf-8") as f:
        return json.load(f)


# =============================================================================
# File collection
# =============================================================================

def collect_pdfs(directory: str | Path, exclude: str | Path | None = None) -> list[Path]:
    """Collect all PDF files in a directory (non-recursive)."""
    dirpath = Path(directory)
    if not dirpath.is_dir():
        raise NotADirectoryError(f"Not a directory: {directory}")
    exclude_resolved = Path(exclude).resolve() if exclude else None
    return sorted(
        p for p in dirpath.glob("*.pdf")
        if not (exclude_resolved and p.resolve() == exclude_resolved)
    )


# =============================================================================
# Graceful cancellation
# =============================================================================

_cancel_callbacks: list[Callable] = []


def register_cancel_callback(fn: Callable) -> None:
    """Register a function to call on Ctrl-C before exiting."""
    _cancel_callbacks.append(fn)


def clear_cancel_callbacks() -> None:
    """Remove all cancel callbacks."""
    _cancel_callbacks.clear()


def _sigint_handler(signum, frame):
    """Handle Ctrl-C: run registered callbacks, then exit."""
    print("\n\n⚠ Interrupted. Saving partial results...", flush=True)
    for fn in _cancel_callbacks:
        try:
            fn()
        except Exception as e:
            print(f"  Error saving partial state: {e}", file=sys.stderr)
    print("  Partial results saved. Exiting.", flush=True)
    sys.exit(130)


def install_signal_handler():
    """Install the SIGINT handler for graceful cancellation."""
    signal.signal(signal.SIGINT, _sigint_handler)


# =============================================================================
# Shared bibliographic helpers
# =============================================================================

def extract_year(date_str: str) -> str:
    """Extract a 4-digit year from a date string. Returns empty string if none found."""
    import re
    m = re.search(r"\b((?:19|20)\d{2})\b", str(date_str))
    return m.group(1) if m else ""


def norm_author(name: str) -> str:
    """
    Normalise an author surname for deduplication.

    For Cyrillic names, applies ALA-LC transliteration via domovyk before
    unidecode, so Cyrillic characters produce meaningful Latin equivalents
    rather than empty strings. Falls back to unidecode for all other scripts.

    Examples:
        "Sindbæk"      → "sindbaek"
        "de Vries"     → "devries"
        "Müller"       → "muller"
        "Непомнящий"   → "nepomniashchii" (via domovyk ALA-LC)
        "Коваленко"    → "kovalenko"
    """
    import re as _re
    import unicodedata as _ud

    if not name:
        return ""

    # Detect Cyrillic content
    if any('\u0400' <= c <= '\u04FF' for c in name):
        try:
            from domovyk import translit as _domovyk_translit
            try:
                transliterated = _domovyk_translit.transliterate(name, 'rus')
            except Exception:
                transliterated = _domovyk_translit.transliterate(name, 'ukr')
            name = transliterated
        except ImportError:
            pass  # Fall through to unidecode

    return _re.sub(r"[^a-z]", "", unidecode(name).lower())