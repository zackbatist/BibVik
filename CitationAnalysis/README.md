# BibVik Citation Analysis Toolkit

A multi-generational citation graph analysis toolkit that extracts bibliographic
references from academic PDFs, builds a citation network, and analyzes how
authors use and relate their references to one another.

## Overview

This toolkit does four things:

1. **Reference Extraction** — Uses GROBID (machine-learning-based) to parse PDFs
   and extract structured bibliographic metadata, outputting biblatex-style JSON.
2. **Citation Graph Construction** — Iterates extraction across a seed paper and
   its referenced papers (F1 generation), building a multi-generational citation
   graph with provenance tracking.
3. **Citation Context Analysis** — For each citation, extracts the verbatim
   surrounding text, infers the function/quality of the citation, and identifies
   co-occurring references.
4. **Citation Cluster Analysis** — Identifies clusters of sources that are
   referenced in similar or related ways, characterizing relationship types
   (similarity, contrast/foil, building-upon, etc.) rather than simple
   co-occurrence counts. Produces two variants: one based solely on citing-paper
   context, and one enriched by the content of the cited papers (when PDFs are
   available).

## Architecture

```
BibVik-CitationAnalysis/
├── README.md                  # This file
├── requirements.txt           # Python dependencies
├── config.yaml                # User-configurable settings
├── run.py                     # Main entry point / CLI
├── bibvik/
│   ├── __init__.py
│   ├── grobid_client.py       # GROBID interaction layer
│   ├── tei_parser.py          # Parse GROBID's TEI-XML output
│   ├── biblatex_model.py      # Biblatex-conformant data model
│   ├── pdf_processor.py       # Orchestrates per-PDF extraction
│   ├── citation_graph.py      # Multi-generational graph builder
│   ├── context_extractor.py   # Verbatim citation context extraction
│   ├── llm_analyzer.py        # LLM-based citation function analysis
│   ├── cluster_analyzer.py    # Citation cluster detection & labelling
│   └── utils.py               # Shared utilities (I/O, citekey gen, etc.)
├── output/                    # Default output directory (created at runtime)
└── tests/                     # Placeholder for future tests
    └── __init__.py
```

## Prerequisites

| Dependency | Purpose | Install |
|---|---|---|
| **Python ≥ 3.10** | Runtime | [python.org](https://www.python.org/downloads/) |
| **Docker** | Runs GROBID server | [docker.com](https://docs.docker.com/get-docker/) |
| **Ollama** | Runs local LLM | [ollama.com](https://ollama.com/download) |

### GROBID

GROBID is a machine-learning library for extracting structured data from PDFs.
We run it as a Docker container so there is nothing to compile locally.

```bash
# Pull and run GROBID (first time will download ~2 GB image)
docker pull lfoppiano/grobid:0.8.1
docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1
```

GROBID will be available at `http://localhost:8070`. You can verify by visiting
`http://localhost:8070/api/isalive` in a browser.

To stop GROBID when you're done:
```bash
docker stop grobid
```

### Ollama + qwen3.5:35b

```bash
# Install Ollama (see https://ollama.com/download for your OS)
# Then pull the model:
ollama pull qwen3:35b
```

Make sure the Ollama server is running (`ollama serve` or it runs automatically
on macOS after install). The model name in `config.yaml` defaults to
`qwen3:35b` — adjust if your local model name differs.

> **Note on model naming:** At time of writing, the Ollama model is listed as
> `qwen3:35b`. If Ollama lists it differently on your system (e.g.
> `qwen3.5:35b`), update the `llm.model` field in `config.yaml` accordingly.

## Setup

### 1. Clone / copy this directory

Place the `BibVik-CitationAnalysis` folder wherever you like.

### 2. Create a virtual environment and install

```bash
cd BibVik-CitationAnalysis

# Create venv
python3 -m venv .venv

# Activate it
# macOS / Linux:
source .venv/bin/activate
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (cmd):
.venv\Scripts\activate.bat

# Install the package in editable mode (this registers the bibvik package
# on your Python path AND installs all dependencies from pyproject.toml).
pip install -e .
```

> **Why `pip install -e .` instead of `pip install -r requirements.txt`?**
> An editable install makes the `bibvik` package properly importable from
> anywhere — not just when your working directory happens to be the project
> root. This avoids `ModuleNotFoundError` issues across platforms. The `-e`
> flag means edits to the source code take effect immediately without
> reinstalling.

### 3. Edit `config.yaml`

Open `config.yaml` and set the paths to your seed paper and F1 PDF folder.
The defaults match the paths you described:

```yaml
seed_paper: "/Users/zackbatist/Library/CloudStorage/Dropbox/zotero/BibVik_seed/SEED_PAPER.pdf"
f1_pdf_dir: "/Users/zackbatist/Library/CloudStorage/Dropbox/zotero/BibVik_seed"
```

Replace `SEED_PAPER.pdf` with the actual filename of your seed paper.

### 4. Start services

```bash
# Terminal 1 — GROBID
docker run --rm -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1

# Terminal 2 — Ollama (if not already running)
ollama serve
```

## Usage

All operations are run through `run.py`:

```bash
# Full pipeline: extract → build graph → analyze contexts → cluster
python run.py --all

# Or run individual stages:
python run.py --extract          # Stage 1: Extract references from seed paper
python run.py --iterate-f1       # Stage 2: Extract references from all F1 PDFs
python run.py --contexts         # Stage 3: Extract + analyze citation contexts
python run.py --cluster          # Stage 4: Cluster analysis
```

### Command-line options

| Flag | Description |
|---|---|
| `--all` | Run the full pipeline |
| `--extract` | Extract refs from seed paper only |
| `--iterate-f1` | Extract refs from F1 PDFs and merge into graph |
| `--contexts` | Extract citation contexts and analyze functions |
| `--cluster` | Run cluster analysis on citation relationships |
| `--config PATH` | Path to config file (default: `config.yaml`) |
| `--output-dir PATH` | Override output directory |
| `--seed PATH` | Override seed paper path |
| `--f1-dir PATH` | Override F1 PDF directory |
| `--verbose` | Enable debug logging |

## Output Files

All outputs go to `output/` (configurable):

| File | Contents |
|---|---|
| `bibliography.json` | Full biblatex-style bibliography with citation graph metadata |
| `citation_contexts.json` | Verbatim contexts, citation functions, co-occurrences |
| `clusters_context_only.json` | Cluster analysis using only citing-paper context |
| `clusters_content_enriched.json` | Cluster analysis enriched by cited-paper content |
| `processing_log.json` | Log of which PDFs were processed, warnings, errors |

### Bibliography JSON structure

Each entry in `bibliography.json` follows this structure:

```json
{
  "doe2020": {
    "citekey": "doe2020",
    "entry_type": "article",
    "title": "A Study of Important Things",
    "author": [
      {"family": "Doe", "given": "Jane"},
      {"family": "Smith", "given": "John"}
    ],
    "date": "2020",
    "journaltitle": "Journal of Important Studies",
    "volume": "12",
    "number": "3",
    "pages": "45--67",
    "doi": "10.1234/example",
    "langid": "english",
    "cited_by": [
      {
        "citekey": "seed_paper_key",
        "generation": "F1",
        "contexts": [
          {
            "context_id": "ctx_seed_paper_key_doe2020_001",
            "verbatim_text": "As Doe and Smith (2020) demonstrated...",
            "citation_function": "evidential_support",
            "citation_function_explanation": "The citing author uses this reference to provide empirical support for their claim about...",
            "co_occurring_citekeys": ["jones2019", "lee2021"]
          }
        ]
      }
    ],
    "generation": "F1"
  }
}
```

### Cluster JSON structure

```json
{
  "clusters": [
    {
      "cluster_id": "cluster_001",
      "relationship_type": "methodological_lineage",
      "relationship_name": "Successive refinements of excavation recording methods",
      "rationale": "These sources are cited in sequence to trace the evolution of...",
      "members": ["doe2020", "jones2019", "smith2018"],
      "relevant_contexts": ["ctx_seed_001", "ctx_seed_003"]
    }
  ],
  "analysis_mode": "context_only"
}
```

## How It Works — Technical Details

### Reference Extraction (GROBID)

We use GROBID's `/api/processFulltextDocument` endpoint, which returns TEI-XML.
GROBID uses deep learning (specifically, CRF and transformer models) to identify
bibliographic references in the body text and the reference list. This is far
more robust than regex, especially for diverse citation styles and non-Latin
scripts.

The TEI-XML is parsed to extract:
- Structured author names (given + family, preserving diacritics and non-Latin
  characters)
- Full journal/book titles
- Editors (for edited volumes)
- Publisher information
- Page ranges, volumes, issues
- DOIs and other identifiers

### Citekey Generation

Citekeys follow the format: `{first_author_family_lowercase}{year}`. When
duplicates arise, suffixes `a`, `b`, `c`... are appended. Non-ASCII characters
in author names are transliterated for the citekey (e.g., "Müller" → "muller")
while the full Unicode name is preserved in the `author` field.

### Citation Context Extraction

GROBID's fulltext processing identifies inline citation markers and links them
to reference list entries. We use these markers to locate citations in the
parsed body text, then extract surrounding context. The context window is
adaptive:

- Default: the enclosing paragraph.
- If the citation is at the paragraph boundary, we extend into the adjacent
  paragraph.
- If a paragraph is very long, we trim to ±3 sentences around the citation
  marker.

### LLM-Based Analysis

The local LLM (qwen3:35b via Ollama) is used for three tasks:

1. **Citation function classification** — Each citation context is sent to the
   LLM with a prompt asking it to classify the function (e.g., evidential
   support, methodological basis, theoretical framing, contrast/critique,
   background, etc.) and explain its reasoning.

2. **Cluster relationship characterization** — After co-occurrence and function
   data are assembled, the LLM analyzes groups of frequently co-occurring or
   functionally related references and assigns named relationship types.

3. **Content-enriched analysis** — When cited-paper PDFs are available, their
   abstracts/introductions are included in the prompt so the LLM can assess
   whether the citing author's characterization aligns with or diverges from
   the cited work's own framing.

All LLM prompts are defined in `bibvik/llm_analyzer.py` and can be customized.

### Scalability to Future Generations

The `generation` field and `cited_by` structure support arbitrary depth. To
extend beyond F2, you would:

1. Place F2-cited PDFs in a directory.
2. Run `--iterate-f1` pointed at that directory with `--generation F2` (a
   planned future flag).
3. The graph builder will correctly assign `F3` to newly discovered references.

The data model is recursive by design.

## Troubleshooting

**GROBID connection refused**
→ Make sure the Docker container is running: `docker ps` should show `grobid`.

**Ollama model not found**
→ Run `ollama list` to check available models. Update `config.yaml` if the
  model name differs.

**Encoding issues with non-Latin text**
→ All string handling uses UTF-8. If you see garbled text, ensure your terminal
  and editor support UTF-8.

**PDF not parsed correctly**
→ GROBID works best with born-digital PDFs. Scanned PDFs may produce incomplete
  results. Consider OCR preprocessing with Tesseract if needed.

**Rate limiting / slow LLM**
→ The 35B model requires significant RAM (~20+ GB). If too slow, you can
  temporarily switch to a smaller model in `config.yaml` for testing, then run
  the full model for final analysis.

## License

This toolkit is provided as-is for academic research purposes.
