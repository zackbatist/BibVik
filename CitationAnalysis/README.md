# BibVik Citation Analysis

Multi-generational citation graph analysis for studying citational practices in Viking Age archaeology. Built around Lund & Sindbæk (2022) "Crossing the Maelstrom" as the seed paper.

## What it does

1. **Detects every citation** in each paper using five complementary methods (GROBID bibliography, GROBID inline markers, regex, LLM body scan, LLM footnote extraction) — all applied to every paper, results merged and deduplicated.

2. **Builds a citation graph** across generations (P → F1 → F2) with deduplication, Zotero-assisted matching, and completeness scoring.

3. **Extracts verbatim citation contexts** with adaptive windowing and co-occurrence tracking.

4. **Classifies citation functions** using a local LLM — why each reference is cited and how faithfully it is characterized.

5. **Detects co-citation clusters** and characterizes the relationships between grouped sources.

6. **Reports coverage** — what we have, what's missing, and where to find it.

## Requirements

- Python 3.10+
- GROBID 0.8.1 via Docker
- Ollama with qwen3.5:35b (or another local model)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Start GROBID
docker run --rm -d -p 8070:8070 --name grobid lfoppiano/grobid:0.8.1

# Start Ollama (if not already running)
ollama serve
ollama pull qwen3.5:35b
```

## Usage

```bash
# Full pipeline
python run.py --all

# Test with 5 papers
python run.py --all --limit 5

# Stages separately
python run.py --extract                    # Seed paper
python run.py --iterate-f1                 # F1 papers
python run.py --contexts                   # Citation context analysis
python run.py --cluster                    # Cluster analysis
python run.py --coverage --email you@uni.edu  # Coverage + OA lookup
```

Edit `config.yaml` to set paths to your seed paper and F1 PDF directory.

## Graceful cancellation

Press Ctrl-C during any long-running stage. The current bibliography state is saved to `output/_partial_bibliography.json` so no work is lost.

## Output

All output goes to `./output/` (configurable):

- `bibliography.json` — Full bibliography with metadata, completeness scores, and provenance
- `citation_contexts.json` — Verbatim contexts with LLM function classifications
- `clusters_context_only.json` — Co-citation clusters with relationship characterizations
- `clusters_content_enriched.json` — Same, enriched with cited papers' content
- `coverage_report.json` — PDF availability and OA status
- `tei/` — Saved GROBID TEI-XML files

## Documentation

See `docs/architecture.qmd` for how the apparatus works.

## Module structure

```
bibvik/
├── detector.py          # 5-method citation detection
├── resolver.py          # CrossRef + LLM resolution
├── graph.py             # Citation graph builder
├── tei_parser.py        # GROBID TEI-XML parsing
├── grobid_client.py     # GROBID HTTP client
├── normalize.py         # Title and author normalization
├── context_extractor.py # Verbatim context extraction
├── llm_analyzer.py      # Citation function classification
├── cluster_analyzer.py  # Co-citation cluster analysis
├── coverage.py          # Coverage reporting
├── biblatex_model.py    # Data model + completeness scoring
├── metadata.py          # Controlled vocabularies
├── zotero_csv.py        # Zotero CSV import
└── utils.py             # Config, citekeys, I/O, signal handling
```

## Removed modules

The following were merged into `detector.py`, `resolver.py`, and `graph.py`:

- `citation_collector.py` → `detector.py`
- `footnote_extractor.py` → `detector.py` (Method 5)
- `reference_resolver.py` → `resolver.py`
- `reference_audit.py` → superseded (detection is now unified, not a separate audit)
- `pdf_processor.py` → `graph.py`
- `citation_graph.py` → `graph.py`
