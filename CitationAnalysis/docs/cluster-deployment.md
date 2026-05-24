# Cluster Deployment

This document describes how to run the BibVik citation analysis pipeline on a
GPU cluster. The cluster setup separates compute-intensive tasks (GROBID, LLM
inference) from the local development environment.

## Architecture

```
Laptop                          Cluster
──────                          ───────
run.py ──── GROBID ────────────► grobid-server (Docker, CPU)
       ──── LLM ──────────────►  ollama_bibvik_gpu* (Docker, GPU × N)
       ◄─── results ────────────  /path/to/BibVik_output/
```

GROBID runs on CPU and is fast on native Linux x86 (no ARM emulation overhead).
LLM inference runs on GPU via Ollama or llama-server. PDFs are stored on the
cluster and processed there; output is written to a designated output directory.

## Prerequisites

- Docker installed on the cluster
- NVIDIA Container Toolkit (for GPU access in Docker)
- Python 3.12+ with a virtual environment
- The BibVik repository cloned to the cluster
- PDFs copied to the cluster (see below)

## Storage Layout

The pipeline expects three directories:

| Path | Contents |
|------|----------|
| `<pdf_dir>/` | F1 PDF corpus and seed paper |
| `<output_dir>/` | Pipeline outputs (generated, gitignored) |
| `<models_dir>/` | LLM model weights (Ollama blobs or GGUF files) |

These paths are set in `config.yaml` (see Configuration below).

## Initial Setup

### 1. Clone the repository

```bash
git clone https://github.com/zackbatist/BibVik.git
cd BibVik/CitationAnalysis
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install ocrmypdf
```

### 2. Install system dependencies

OCR fallback requires Tesseract. Ask your system administrator to install:

```
tesseract-ocr
```

### 3. Copy PDFs to the cluster

From your local machine:

```bash
rsync -avz /path/to/local/pdf/corpus/ user@cluster:/path/to/cluster/pdf/dir/
```

### 4. Create config.yaml

```bash
cp config.example.yaml config.yaml
# Edit config.yaml — see Configuration section below
```

## Starting GROBID

GROBID runs as a Docker container. Start it once and leave it running:

```bash
docker run --rm -d \
  -p 8070:8070 \
  -e JAVA_OPTS="-Xmx4g" \
  --name grobid-server \
  lfoppiano/grobid:0.8.1
```

Wait 60–90 seconds for models to load, then verify:

```bash
curl http://localhost:8070/api/isalive
# → true
```

Notes:
- `-Xmx4g` limits Java heap to 4GB — increase if you see memory errors
- `--rm` removes the container when stopped; omit if you want it to persist across restarts
- The container name `grobid-server` matches the default in `config.yaml`

## Starting LLM Servers

Use `launch_bibvik_llm.sh` to start one or more Ollama instances:

```bash
# Single GPU (auto-selects most free)
bash launch_bibvik_llm.sh

# Specific GPU
bash launch_bibvik_llm.sh --gpu 7

# Multiple GPUs — one instance per GPU, parallel paper processing
bash launch_bibvik_llm.sh --gpus 4,5,6,7

# llama-server with tensor parallelism (one large model across 2 GPUs)
bash launch_bibvik_llm.sh --gpu 6 --tensor 2 --backend llama_server

# Stop all BibVik LLM containers
bash launch_bibvik_llm.sh --stop
```

The script assigns ports sequentially starting from the base port (default 11440).
Four instances on GPUs 4, 5, 6, 7 use ports 11440, 11441, 11442, 11443.

After launching, update `config.yaml` with the assigned ports (see below).

### Checking GPU status

```bash
nvidia-smi                    # GPU utilisation and memory
nvtop                         # Live GPU activity
docker ps                     # Running containers
docker logs -f grobid-server  # GROBID request log
docker logs -f bibvik_llm_ollama_gpu7  # Ollama log for GPU 7
```

### Model storage

Ollama stores model weights in the configured models directory, shared across
all instances. The first `pull` downloads the model; subsequent instances reuse
the cached weights. With a NAS-mounted models directory this means one download,
available to all containers.

## Configuration

Create `config.yaml` from the template. Key fields for cluster use:

```yaml
seed_paper: "/path/to/pdfs/seed-paper.pdf"
f1_pdf_dir: "/path/to/pdfs/"
output_dir: "/path/to/output/"
zotero_csv: "/path/to/CitationAnalysis/Exported_Items.csv"

email: "your@email.com"

grobid:
  base_url: "http://localhost:8070"
  timeout: 180
  container_name: "grobid-server"

llm:
  base_url: "http://localhost:11440"
  backend: "ollama"
  model: "qwen2.5:7b"
  detection_batch_size: 5

  # Multi-GPU: add one URL per additional instance
  extra_urls:
    - "http://localhost:11441"
    - "http://localhost:11442"
    - "http://localhost:11443"
```

`config.yaml` is gitignored — never commit it.

## Running the Pipeline

```bash
source .venv/bin/activate

# Full pipeline: extract + build citation graph
python3 run.py --extract --iterate-f1

# Limit to N papers (testing)
python3 run.py --extract --iterate-f1 --limit 10

# Resume interrupted run (cached papers skipped automatically)
python3 run.py --iterate-f1

# Enrich bibliography via CrossRef
python3 run.py --enrich --email your@email.com

# Generate audit sample
python3 run.py --audit

# Copy audit to local machine for review
# (run from laptop)
scp user@cluster:/path/to/output/audit_sample.md ~/Desktop/
```

## Multi-GPU Parallel Processing

When `extra_urls` is set in `config.yaml`, the pipeline distributes papers
across all LLM endpoints in parallel:

1. GROBID processes all papers sequentially (one GROBID instance)
2. LLM processing runs in parallel — one worker thread per endpoint
3. Papers are assigned round-robin across endpoints

With 4 GPUs and ~90 seconds per paper, throughput is approximately 4×:

| GPUs | Papers | Estimated time |
|------|--------|----------------|
| 1    | 382    | ~9 hours       |
| 2    | 382    | ~4.5 hours     |
| 4    | 382    | ~2.5 hours     |

These estimates assume qwen2.5:7b and `detection_batch_size: 5`. Larger models
or batch size 1 will be slower.

## Remote Access from Laptop

To run locally while using the cluster's LLM:

```bash
# Open SSH tunnel (one per LLM port)
ssh -L 11440:localhost:11440 \
    -L 11441:localhost:11441 \
    -L 11442:localhost:11442 \
    -L 11443:localhost:11443 \
    user@cluster

# Run locally with --remote flag
python3 run.py --iterate-f1 --limit 10 --remote
```

Set `llm.remote_url` in your local `config.yaml` to `http://localhost:11440`
and `llm.remote_backend` to `ollama`.

## Troubleshooting

**GROBID unavailable**
```bash
curl http://localhost:8070/api/isalive
docker logs grobid-server | tail -20
docker restart grobid-server  # auto-restart also built into pipeline
```

**Port already allocated**
```bash
docker ps | grep <port>
docker rm -f <container-name>
```

**LLM running on CPU instead of GPU**
```bash
docker inspect <container-name> | grep -i runtime
# Should show "nvidia", not "runc"
# If "runc": the container lacks GPU access — restart with --gpus flag
```

**Out of GPU memory**
```bash
nvidia-smi  # check memory usage per GPU
# Use a smaller model or fewer parallel instances
```

**Tesseract not found (OCR fallback)**
```
Ask system administrator: apt install tesseract-ocr
```

**Resuming an interrupted run**
The pipeline saves state after each paper. Simply rerun — already-processed
papers are skipped automatically via the cache in `_graph_state.json`.
