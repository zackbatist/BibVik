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

    llm = config.setdefault("llm", {})
    llm.setdefault("base_url", "http://localhost:11434")
    llm.setdefault("backend", "ollama")
    llm.setdefault("model", "qwen3.5:35b")
    llm.setdefault("detection_model", "")  # If empty, uses main model
    llm.setdefault("detection_batch_size", 4)  # Paragraphs per LLM call for body scan
    llm.setdefault("temperature", 0.3)
    llm.setdefault("max_tokens", 2048)
    llm.setdefault("timeout", 300)

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


def generate_citekey(authors: list[dict], year: str | None) -> str:
    """
    Generate a biblatex-style citekey: lastnameyear with a/b/c disambiguation.

    Non-ASCII is transliterated for the key but preserved in the record.
    """
    if authors and authors[0].get("family"):
        family = unidecode(authors[0]["family"]).lower()
        family = re.sub(r"[^a-z]", "", family)
    else:
        family = "unknown"

    year_str = str(year).strip()[:4] if year else "nd"
    base = f"{family}{year_str}"

    if base not in _citekey_registry:
        _citekey_registry[base] = 1
        return base
    else:
        count = _citekey_registry[base]
        _citekey_registry[base] = count + 1
        return f"{base}{chr(ord('a') + count - 1)}"


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

    Applies unidecode transliteration then strips all non-alphabetic characters
    and lowercases. Used consistently across detector, resolver, graph, and
    zotero_csv for author-key matching.

    Examples:
        "Sindbæk"  → "sindbaek"
        "de Vries" → "devries"
        "Müller"   → "muller"
    """
    import re
    return re.sub(r"[^a-z]", "", unidecode(name).lower())