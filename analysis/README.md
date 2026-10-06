# Analysis

The Quarto documents in this directory describe the corpus, the annotation data, the citation graph, and the three combined. Two toolsets run outside the render because they take hours or days or need a GPU. `gn_analysis/` runs Girvan-Newman community detection, and `cluster_labelling/` has a language model name the resulting clusters. Their finished outputs stay in their own results folders, and 02 reads each run from `analysis/gn_analysis/results/<run>/`.

## Documents

| File | Content | Reads | Writes |
|---|---|---|---|
| `index.qmd` | Shell that includes 00 to 03 in order, then a Future work section | the four documents | none |
| `00_overview.qmd` | Part 0, corpus overview | `data/bibliography.json`, `data/annotations.csv`, `data/citation_edgelist.csv`, `data/citation_edgelist_annotated.csv`, `CitationAnalysis/corrections.yaml` | none |
| `01_annotation.qmd` | Part 1, annotation descriptives | `data/annotations.csv`, `data/region_lookup.csv` | `analysis/bibvik_annotation_df.csv` (untracked); `data/region_lookup.csv` and `data/region_lookup_needs_review.csv`, only when the disabled Wikidata chunks are switched on |
| `02_network_structure.qmd` | Part 2, structure of the citation graph | `data/bibliography.json`, `data/citation_edgelist_annotated.csv`, `analysis/gn_analysis/edgelist_annotated_no_seed.csv`, `analysis/gn_analysis/results/<run>/` | `analysis/bibvik_node_table.csv` (untracked); layout images, only from disabled chunks |
| `03_joined_descriptions.qmd` | Part 3, structure joined to annotation | `analysis/bibvik_annotation_df.csv`, `analysis/bibvik_node_table.csv` | none |
| `open-scholarly-metadata.qmd` | Scratch notes on retrieving references from OpenCitations and Crossref | n/a | n/a |

`index.qmd` renders 00 to 03 in that order. 03 reads files that 01 and 02 write, so a single document rendered on its own needs 01 and 02 rendered first. `open-scholarly-metadata.qmd` is not part of the render.

## Where outputs live

Documents read committed files, so a render never needs a pipeline run. The exported bibliography and edge lists reach `data/` by a copy step, in the commits titled "Sync exported data files". Girvan-Newman runs are committed where they are written, the way the earlier single run was.

| Output | Produced by | Working location | Read from |
|---|---|---|---|
| Bibliography, edge lists, graph exports | the BibVik pipeline in `CitationAnalysis/` | `BibVik_output/` on the server | `data/` |
| Region lookup | the disabled Wikidata chunks in 01 | written straight into `data/` | `data/region_lookup.csv` |
| Girvan-Newman runs and their labels | `gn_analysis/` and `cluster_labelling/` | `analysis/gn_analysis/results/<run>/`, labels in `<run>/labels/` | the same folder |

## Girvan-Newman and cluster labelling

Two runs are made from the repaired annotated graph, one with the seed paper and one without it. The R and shell scripts live in `analysis/gn_analysis/`, and the Python scripts in `analysis/cluster_labelling/`. `launch_bibvik_llm.sh` is not in the repository, because `.gitignore` excludes it. It exists only on the machine where it was made, and the chain finds it by searching under `~/models/`.

| | With seed | No seed |
|---|---|---|
| Edge list | `data/citation_edgelist_annotated.csv` | `analysis/gn_analysis/edgelist_annotated_no_seed.csv` |
| Directed edges | 21,457 | 20,884 |
| Undirected edges GN sees | 21,456 | 20,883 |
| Nodes | 12,616 | 12,517 |
| Components at the start | 1 | 4 (12,430, 48, 27 and 12 nodes) |
| Run folder | `results/annotated` | `results/annotated_no_seed` |

The no-seed list drops the seed paper (`lund2021`) and its 573 outgoing edges. That also removes 98 works that only the seed cites (86 F1, 12 F2), which makes 99 fewer nodes. Both lists have a `source,target` header and Windows line endings. GN collapses the directed edges to undirected before the first round. Representative selection reads the full edge list, `data/citation_edgelist.csv`, as `cluster_labelling/cluster_labelling_README.md` specifies.

### Layout while running

```text
analysis/gn_analysis/results/
├── annotated/                 with-seed run
├── annotated_no_seed/         no-seed run
└── post_all.log               log of the chain
```

Each run folder fills in as the pipeline advances.

```text
results/annotated/
├── removal_log.csv            one row per removed edge (round, from, to)
├── modularity_trace.csv       round, edges_removed, n_components, modularity
├── communities.csv            node_id, community_id at the best-scoring round
├── final_edgelist.csv         the edge list the run used
├── state.rds                  checkpoint that lets the run resume
├── run.log, session_stdout.log
├── multi_cut/                 communities_round_<N>.csv and multi_cut_summary.csv
├── dendrogram/                gn_dendrogram.png, gn_dendrogram.rds, split_events.csv
├── subclusters/               subcluster_summary.csv
└── labels/                    representatives.csv, cluster_labels.csv, node_labels.csv
```

### Launching everything

One paste starts both runs and the chain. It deletes nothing, so it is safe to paste at any time. Run it at a normal prompt, not inside tmux.

```bash
cd ~/models/BibVik/analysis/gn_analysis
pkill -f '[w]hile ! grep -q' 2>/dev/null
if ! pgrep -f 'file=[r]un_gn_analysis.R --args ../../data/citation_edgelist_annotated.csv' > /dev/null && ! grep -qs "Done\." results/annotated/run.log; then
  tmux kill-session -t =girvan_newman 2>/dev/null
  mkdir -p results/annotated
  tmux new-session -d -s girvan_newman "Rscript run_gn_analysis.R ../../data/citation_edgelist_annotated.csv results/annotated/ > results/annotated/session_stdout.log 2>&1; exec bash"
fi
if ! pgrep -f 'file=[r]un_gn_analysis.R --args edgelist_annotated_no_seed.csv' > /dev/null && ! grep -qs "Done\." results/annotated_no_seed/run.log; then
  tmux kill-session -t =girvan_newman_noseed 2>/dev/null
  mkdir -p results/annotated_no_seed
  tmux new-session -d -s girvan_newman_noseed "Rscript run_gn_analysis.R edgelist_annotated_no_seed.csv results/annotated_no_seed/ > results/annotated_no_seed/session_stdout.log 2>&1; exec bash"
fi
if [ "$(pgrep -fc '[w]hile ! \{ grep -q')" = 0 ] && ! grep -qs "all steps finished" results/post_all.log; then
nohup bash -c '
MODEL=qwen3.5:35b; W=100; R=0.9
A=results/annotated; B=results/annotated_no_seed
EA=../../data/citation_edgelist_annotated.csv; EB=edgelist_annotated_no_seed.csv
say() { echo "[$(date "+%F %T")] $*"; }
earliest() { ls $1/multi_cut/communities_round_*.csv | sed -E "s/.*_round_([0-9]+)\.csv$/\1/" | sort -n | head -1; }
post() {
  D=$1; E=$2
  rm -rf $D/multi_cut $D/dendrogram $D/subclusters $D/labels
  Rscript generate_multi_cut_communities.R $E $D/removal_log.csv $D/modularity_trace.csv $D/multi_cut $W $R || { say "$D: cluster detection failed"; return 1; }
  N=$(earliest $D); say "$D: earliest cut is round $N"
  Rscript build_dendrogram.R $E $D/removal_log.csv $D/dendrogram 300 0 0 $D/modularity_trace.csv $W $R || say "$D: dendrogram failed"
  Rscript build_subcluster_trees.R $E $D/removal_log.csv $D/multi_cut/communities_round_$N.csv $N $D/subclusters 3 20 || say "$D: subcluster trees failed"
  (cd ../cluster_labelling && python3 select_representatives.py ../../data/bibliography.json ../../data/citation_edgelist.csv ../gn_analysis/$D/labels --partition-csv ../gn_analysis/$D/multi_cut/communities_round_$N.csv) || { say "$D: representatives failed"; return 1; }
  say "$D: cuts, dendrogram, subcluster trees and representatives done"
}
say "waiting for both GN runs to finish"
while ! { grep -q "Done\." $A/run.log 2>/dev/null && grep -q "Done\." $B/run.log 2>/dev/null; }; do sleep 300; done
say "both GN runs finished"
post $A $EA; post $B $EB
TODO=""; for X in $A $B; do [ -f $X/labels/representatives.csv ] && TODO="$TODO $X"; done
[ -n "$TODO" ] || { say "nothing to label; stopping"; exit 1; }
python3 -c "import requests" 2>/dev/null || { say "python3 cannot import requests, which label_clusters.py needs; labelling skipped"; exit 1; }
LAUNCH=$(find ~/models/ -name launch_bibvik_llm.sh -not -path "*/.git/*" 2>/dev/null | head -1)
[ -n "$LAUNCH" ] || { say "launch_bibvik_llm.sh not found under ~/models; labelling skipped"; exit 1; }
say "waiting for a GPU with under 2000 MiB used and under 10 percent busy"
while true; do
  G=$(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits | awk -F", " "\$2 < 2000 && \$3 < 10 {print \$1; exit}")
  [ -n "$G" ] && break; sleep 300
done
say "GPU $G is free; starting Ollama with $MODEL"
(cd $(dirname $LAUNCH) && bash $LAUNCH --gpu $G --model $MODEL) || { say "launch_bibvik_llm.sh failed"; exit 1; }
curl -s -m 20 http://localhost:11440/api/tags | grep -q "\"$MODEL\"" || { say "Ollama is not serving $MODEL on port 11440; labelling skipped"; (cd $(dirname $LAUNCH) && bash $LAUNCH --stop); exit 1; }
for X in $TODO; do
  N=$(earliest $X); say "$X: labelling the round $N cut"
  (cd ../cluster_labelling && python3 label_clusters.py ../gn_analysis/$X/labels/representatives.csv ../gn_analysis/$X/multi_cut/communities_round_$N.csv ../gn_analysis/$X/labels --model $MODEL --base-url http://localhost:11440) || say "$X: labelling failed"
done
(cd $(dirname $LAUNCH) && bash $LAUNCH --stop)
say "all steps finished"
' > results/post_all.log 2>&1 &
fi
tmux ls
```

The command acts on whatever state it finds.

| State | Result |
|---|---|
| `results/` empty | Both runs start fresh and the chain starts. |
| Partial runs, nothing running (crash or reboot) | Both runs resume from their checkpoints. |
| One run dead, one alive | Only the dead run resumes. The live run keeps its process. |
| Both running | Nothing changes and no second chain starts. |
| Both finished | Nothing changes. The chain does not restart once its log says `all steps finished`. |

To restart from scratch on purpose, run `rm -rf results/*` first and paste the command again.

### What the chain does

| Step | Tool | Result |
|---|---|---|
| Wait | Polls both `run.log` files every 5 minutes for `Done.` | Starts when both runs have finished. |
| Cluster detection | `gn_analysis/generate_multi_cut_communities.R` | `multi_cut/communities_round_<N>.csv` for every sustained fragmentation onset, plus `multi_cut_summary.csv`. |
| Dendrogram | `gn_analysis/build_dendrogram.R` with the modularity trace | `dendrogram/`. The tree stops at the earliest onset. |
| Subcluster trees | `gn_analysis/build_subcluster_trees.R` seeded at the earliest cut, minimum community size 3, up to 20 trees | `subclusters/subcluster_summary.csv`. |
| Representatives | `cluster_labelling/select_representatives.py` on the earliest cut | `labels/representatives.csv`, the top 10 papers per cluster by within-cluster degree. |
| GPU | `nvidia-smi` every 5 minutes | The first GPU with under 2,000 MiB used and under 10% busy. |
| Ollama | `launch_bibvik_llm.sh --gpu <G> --model qwen3.5:35b` | A server on port 11440, checked through `/api/tags`. |
| Labelling | `cluster_labelling/label_clusters.py`, with-seed graph first, then no-seed | `labels/cluster_labels.csv` and `node_labels.csv`. |
| Shutdown | `launch_bibvik_llm.sh --stop` | Ollama stops. |

The labelled cut is the earliest sustained fragmentation onset, which is the smallest round number in `multi_cut/`. `02_network_structure.qmd` documents it as the only onset that gives a singleton-free partition on this corpus. Detection uses a window of 100 rounds and a minimum rate of 0.9 new components per round. `build_dendrogram.R` recomputes the earliest onset from the trace with the same rule, so both scripts need identical window and rate values.

The chain labels both graphs through one Ollama instance on one GPU, one graph after the other. `label_clusters.py` calls Ollama's `/api/generate` endpoint, so the Ollama backend is required and the `llama_server` tensor-parallel route cannot serve it.

Every failure writes a timestamped line to `results/post_all.log` and stops only the steps that depend on it. A failed detection skips that graph. A failed launcher, a missing `requests` module or a model that Ollama does not list stops the chain before labelling and leaves every earlier output in place.

### Monitoring

```bash
cd ~/models/BibVik/analysis/gn_analysis
echo "now $(date +%H:%M:%S) | GN processes (expect 2): $(pgrep -fc 'file=[r]un_gn_analysis') | chain (expect 1): $(pgrep -fc '[w]hile ! \{ grep -q')"
for p in "results/annotated 21456" "results/annotated_no_seed 20883"; do read d t <<< "$p"; n=$(( $(wc -l < $d/removal_log.csv) - 1 )); echo "$d: round $n of $t ($(( 100 * n / t ))%) | checkpoint last written $(ls -l --time-style=+%H:%M:%S $d/state.rds | awk '{print $6}')"; done
grep "^\[" results/post_all.log
```

A healthy state is 2 GN processes, 1 chain, and a checkpoint written within the last minute or two. Early rounds take about 19 seconds. Rounds speed up as the graph fragments, so the percentage understates how much is left and the finish time cannot be predicted from it. A checkpoint time that stops advancing means the run stalled.

The last line prints the chain's own timestamped messages. They name the current stage (`waiting for both GN runs to finish`, `waiting for a GPU`, `labelling the round <N> cut`) and any failure.

### Control and recovery

**Stop a run cleanly.** `touch results/annotated/STOP` or `touch results/annotated_no_seed/STOP`. The run checkpoints after its current round, removes the file and exits. Pasting the launch command resumes it.

**After a reboot or crash.** Paste the launch command. It restarts only what is dead, and a run resumes at the round where its checkpoint ends.

**Cancel the chain.** `pkill -f '[w]hile ! \{ grep -q'`. Pasting the launch command starts it again.

**Rerun detection with other parameters.** Delete the folder first with `rm -rf results/<run>/multi_cut`, then pass the same window and rate to `generate_multi_cut_communities.R` (arguments 5 and 6) and to `build_dendrogram.R` (arguments 8 and 9).

**Label by hand.** The chain stops before labelling when the launcher cannot be found, `requests` is missing, or Ollama does not serve the model. It waits without failing while no GPU is free. After the cause is fixed, paste the launch command again. It redoes the post-run steps and then labels. The rehearsal below runs the labelling steps by hand on a small sample.

**Check that the running chain is the tested script.** Terminal echo of a long pasted command can look scrambled. This reads the script back from the process.

```bash
PID=$(pgrep -f '[w]hile ! \{ grep -q' | head -1); echo "chain pid $PID"
tr '\0' '\n' < /proc/$PID/cmdline | tail -n +3 > /tmp/chain_as_run.txt
echo "lines: $(wc -l < /tmp/chain_as_run.txt) (expect 40)"
echo "sha256: $(sha256sum < /tmp/chain_as_run.txt | cut -c1-16) (expect 6f424e7a80b9ed95)"
bash -n /tmp/chain_as_run.txt && echo "syntax ok"
```

### Finish check

Every line should read `ok`, and the log should end with `all steps finished`.

```bash
cd ~/models/BibVik/analysis/gn_analysis
for D in results/annotated results/annotated_no_seed; do for f in communities.csv final_edgelist.csv modularity_trace.csv removal_log.csv multi_cut/multi_cut_summary.csv dendrogram/gn_dendrogram.png dendrogram/split_events.csv subclusters/subcluster_summary.csv labels/representatives.csv labels/cluster_labels.csv labels/node_labels.csv; do [ -s $D/$f ] && echo "ok       $D/$f" || echo "MISSING  $D/$f"; done; done
grep "^\[" results/post_all.log
```

### Committing finished runs

02 reads each run from `analysis/gn_analysis/results/<run>/`, so committing a run is a matter of choosing which files to track. Run this on the server when both runs and the chain have finished. It tracks the files the earlier single run tracked, plus the labels.

```bash
cd ~/models/BibVik
git pull
git add analysis/gn_analysis/results/*/communities.csv analysis/gn_analysis/results/*/final_edgelist.csv analysis/gn_analysis/results/*/modularity_trace.csv analysis/gn_analysis/results/*/removal_log.csv analysis/gn_analysis/results/*/multi_cut analysis/gn_analysis/results/*/labels
git commit -m "Add the finished Girvan-Newman runs"
git push
```

Then pull on the machine that renders and render 02 and 03, or `index.qmd`. 02 selects the earliest cut by sorting, so it needs no round number. For each graph it rebuilds the subcluster summary and the dendrogram image from the run's removal log when they are missing or older than the removal log. Each rebuild takes minutes. `.gitignore` excludes every `*.png`, so no dendrogram image is committed, and both images are rebuilt on the first render.

Leave the working files out of git. `.gitignore` names a few of them under `analysis/gn_analysis/results/`, and those lines do not match the nested run folders. These lines do.

```
analysis/gn_analysis/results/*/state.rds
analysis/gn_analysis/results/*/run.log
analysis/gn_analysis/results/*/session_stdout.log
analysis/gn_analysis/results/*/dendrogram/gn_dendrogram.rds
analysis/gn_analysis/results/*/dendrogram/split_events.csv
analysis/gn_analysis/results/*/subclusters/subcluster_summary.csv
```

`analysis/cluster_labelling/results/` still holds the labels from the earlier run on the old graph. No document reads it.

If the runs are later copied into `data/` instead, change `gn_root` in the `gn-paths` chunk of 02 to `here("data", "gn")` and nothing else.

### Rehearsal on an earlier run

These two blocks test the post-run steps on a finished earlier run found in `results_backup_*`, and they write to `~/models/rehearsal`. The first prints the real run time of detection, the dendrogram and the subcluster trees. The second runs representative selection, the GPU gate, the launcher, the model check, labelling of the 3 largest clusters, and shutdown. It holds one GPU for a few minutes.

```bash
cd ~/models/BibVik/analysis/gn_analysis
for d in results_backup_*/; do if [ -f ${d}removal_log.csv ] && [ -f ${d}modularity_trace.csv ] && [ -f ${d}final_edgelist.csv ]; then B=${d%/}; break; fi; done
echo "rehearsing on: ${B:-NO BACKUP HAS ALL THREE FILES}"
T=~/models/rehearsal; rm -rf $T; mkdir -p $T
time Rscript generate_multi_cut_communities.R $B/final_edgelist.csv $B/removal_log.csv $B/modularity_trace.csv $T/multi_cut
N=$(ls $T/multi_cut/communities_round_*.csv | sed -E "s/.*_round_([0-9]+)\.csv$/\1/" | sort -n | head -1); echo "earliest cut: $N"
time Rscript build_dendrogram.R $B/final_edgelist.csv $B/removal_log.csv $T/dendrogram 300 0 0 $B/modularity_trace.csv
time Rscript build_subcluster_trees.R $B/final_edgelist.csv $B/removal_log.csv $T/multi_cut/communities_round_$N.csv $N $T/subclusters 3 20
ls -R $T | head -30
```

```bash
cd ~/models/BibVik/analysis/cluster_labelling
T=~/models/rehearsal; N=$(ls $T/multi_cut/communities_round_*.csv | sed -E "s/.*_round_([0-9]+)\.csv$/\1/" | sort -n | head -1)
python3 select_representatives.py ../../data/bibliography.json ../../data/citation_edgelist.csv $T/labels --partition-csv $T/multi_cut/communities_round_$N.csv --max-clusters 3
python3 -c "import requests; print('requests ok')"
LAUNCH=$(find ~/models/ -name launch_bibvik_llm.sh -not -path "*/.git/*" | head -1); echo "launcher: $LAUNCH"
G=$(nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader,nounits | awk -F", " '$2 < 2000 && $3 < 10 {print $1; exit}'); echo "free GPU chosen: $G"
(cd $(dirname $LAUNCH) && bash $LAUNCH --gpu $G --model qwen3.5:35b)
curl -s http://localhost:11440/api/tags | grep -o '"name": *"[^"]*"'
python3 label_clusters.py $T/labels/representatives.csv $T/multi_cut/communities_round_$N.csv $T/labels --model qwen3.5:35b --base-url http://localhost:11440
cut -c1-120 $T/labels/cluster_labels.csv
(cd $(dirname $LAUNCH) && bash $LAUNCH --stop)
```

An empty line after `free GPU chosen:` means no GPU is free. A missing model name after the `curl` line means the launcher serves on another port, and the chain assumes port 11440.

### What was tested

The commands were tested on 2026-10-06 with the real R and Python scripts in a scratch copy of the repository (R 4.3.3, igraph 1.6.0). The test data was a synthetic seeded graph with 384 nodes and 1,598 edges, written with Windows line endings. A stand-in Ollama server answered in the format `label_clusters.py` parses. A stand-in launcher recorded its flags. A fake `nvidia-smi` reported the GPU figures from an nvtop screenshot in which GPUs 0 to 3 were busy (GPU 3 held 7.5 GB at 0% utilisation), GPUs 8 and 9 were partly used, and GPUs 4 to 7 were idle, so the gate chose GPU 4. `02_network_structure.qmd` was rendered with knitr against the same synthetic runs in `analysis/gn_analysis/results/`, with both runs present, with only the with-seed run, and with no run installed. All three rendered without errors.

| Scenario | Result |
|---|---|
| Both runs launched together | Each wrote its own folder with the expected files, headers and row counts. |
| Stop, move folders, resume | Rounds ran 1 to N with no gaps or repeats. |
| Empty, interrupted, running, finished and half-finished states | Each behaved as the launching table says. |
| With-seed run restarted while the no-seed run kept running | The no-seed process kept its process id. |
| Second run unfinished | The chain waited. |
| Every GPU busy | The chain waited and used the first GPU that freed up. |
| Launcher missing, `requests` missing, detection failing | Each logged one clear line, kept the CPU outputs, and left the GPU untouched. |
| Cross-file checks on both graphs | 12 of 12 passed. Node ids, community ids and titles agree from the GN output through the labels. |

Not exercised on real data or on the real machine are the real `launch_bibvik_llm.sh` with its flags and port 11440, whether `qwen3.5:35b` fits on one 24 GB GPU, the run time of cluster detection, the dendrogram and the subcluster trees at the real graph size, and the length of the GN runs themselves. The rehearsal covers the first three.

### Hazards

**Handled by the launch command, still true for manual work**

- **tmux matches session names by prefix.** `tmux has-session -t girvan_newman` succeeds when only `girvan_newman_noseed` exists, so `start_gn.sh` reports "already running" and starts nothing. A plain `kill-session -t girvan_newman` removes `girvan_newman_noseed` when the exact session is absent. The launch command targets sessions as `=girvan_newman` and starts the with-seed run with the same `tmux new-session` call that `start_gn.sh` makes. Use the `=` form and skip `start_gn.sh` when working by hand.
- **Process patterns.** `pgrep -f 'Rscript run_gn_analysis'` matches the tmux server and the `bash -c` wrappers. It reports 3 for two runs and still reports 1 after both end, while the tmux server lives. The pattern `file=[r]un_gn_analysis` matches only R. A bare `{` in a `pgrep` or `pkill` pattern is a regex error, so the chain pattern is `[w]hile ! \{ grep -q`. The bracket on the first letter keeps a pattern from matching its own command line.
- **Stale cuts.** Detection never removes old `communities_round_<N>.csv` files. A leftover file with a smaller round number becomes the earliest cut. The chain deletes `multi_cut/` before each detection, and manual reruns need the same deletion.

**Live**

- **No onset found.** With window 100 and rate 0.9, detection stops with `No sustained fast-fragmentation onsets detected` when the graph does not fragment fast enough. A small synthetic graph did this. Detection has not run on these two graphs yet, and the no-seed graph starts with four components. The earlier real runs found onsets with the defaults. If it fails, the chain logs the graph and skips it. Relax the two arguments and give the same values to `build_dendrogram.R`.
- **GN modularity.** `run_gn_analysis.R` scores modularity against the shrinking graph, so late rounds can score very high. Cut selection uses fragmentation onsets, so the chain's output is unaffected. The best-scoring partition in `communities.csv` and any modularity figure quoted from the traces are unreliable until a recomputation from the removal log against the original graph is written.
- **igraph versions.** The server has igraph 1.6.0. The GN scripts use `get.edge.ids` and `as.undirected`, which that version has. 02 calls `as_undirected()`, which igraph added in 2.0, so 02 renders on igraph 2.0 or later. On 1.6.0 the k-core chunk fails and the Louvain and Leiden chunks fail after it. Render 02 locally, or define `as_undirected <- igraph::as.undirected` before it.

## Not yet done

Each item appears once here. The documents no longer carry their own lists.

### Annotation and gender (01, 03)

- Choose the gender composition scheme for the topic, method and source cross-tabs. `composition_strict`, `composition_majority` and `composition_singleauthor_split` are computed side by side, and 01 charts the third. None has been chosen.
- Normalize the topic, method and source proportions by gender against the corpus-wide gender base rate. The cross-tabs report raw proportions.
- Add margin notes to Gender's By-year and Composition subsections, which have captions and Wilson fade only.
- Show mixed-gender team size, as each paper's own size, in Gender's own corpus-level sections. It exists only per topic, method and source.
- Include secondary topic, method and source values in the gender cross-tabs. Only primary values are compared.
- Fill in `fieldwork_lead_methods`, the mapping from coded method category to first-versus-last lead-author convention, with real codebook category names. It is a placeholder, so every paper defaults to "ambiguous" and the lead-author analysis means nothing yet.

### Region (01, 03)

- Cross-tab region against gender. Region is cross-tabbed against topic, method and source.

### Network and communities (02, 03)

- Run the title and annotation checks on individual Girvan-Newman communities in 02. The community with the most balanced first split is the strongest candidate, and any community whose smaller piece stays intact at first split needs a different check. Neither has been carried out on the new partition.
- Decide whether to implement bounded-depth, parallelized Girvan-Newman. No benchmark exists for how many splits give a stable, interpretable first division, and the full runs on the repaired graphs now give runtime figures to start from.
- Decide whether directed variants of Girvan-Newman, Louvain and Leiden are worth the added complexity.
- Decide whether 03 needs the same Annotated and seed-filtered tabs. It reads only `community_gn`, from the Annotated run.

### Planned comparisons (03)

- Test whether centrality (`in_degree`, `betweenness`, `pagerank`) within a cluster correlates with author gender, that is, whether the most central or bridging works in a cluster are disproportionately authored by one gender even when the cluster's dominant topic is not gender-skewed.
- Analyze self-citation by gender, cluster and unique individual, and test whether the rate tracks centrality or prevalence across clusters. Test whether women-led work is cited more in some clusters than others, using average out-degree within each cluster filtered by the recipient's author information and divided further by the sender's gender.
- Test direct citation homophily, whether papers (or, at author level, authors) cite others who share their coded gender, topic or method. It is computable from the edge list and the join, with no cluster output.
- Build per-F2-node citer profiles. For F2 works with `in_degree` above a threshold, characterize the gender, topic and method of their F1 citer set and whether the set is structurally clustered or scattered. This shows whether a source works as a shared touchstone across the field's divisions or as an in-group marker.

### Open decisions

- Choose which unit of analysis beyond the paper to pursue first (author, venue or kind of work). `index.qmd` records the options in its Future work section.
- Decide how explicitly to frame the seed-paper-testing angle in writeups, given the PI and co-author relationship.

### Corpus documentation (00)

- Itemize and categorize the exclusion reasons for the remaining works in the seed's reference list. Only two criteria are documented (full books and edited volumes with consolidated bibliographies, and methodological or technical reports outside the study's scope).

### Scratch notes (`open-scholarly-metadata.qmd`)

- Recover the code that generated `seed_refs_bib` and fill in the step that generates author lists indexed by DOI.

### Pipeline and data

- Commit the finished Girvan-Newman runs (the commands above), then re-render 02 and 03.
- Write the modularity recomputation from the removal log against the original graph.
- Consolidate the `gn_analysis` and `cluster_labelling` directories and outputs into one layout after both runs are installed. Labels now travel with their run, in `<run>/labels/`.
- Add the ignore lines for the run working files to `.gitignore` (see Committing finished runs).
- Clear out old results. `~/models/gn_results_old_*` and the two untracked `results_backup_*` folders in `analysis/gn_analysis/` are on the server only.
- Patch `_find_duplicate()` and `_merge_into()` in the pipeline. Do not run `--extract` or `--iterate-f1` until then.
- Resolve the 59 field corrections that no online source can settle, and split the `bradley2002` title, which glues two references together.

## Re-check after the first render with the new runs

02 and 03 contain statements typed in from earlier renders. The repaired graph and the new runs can change them. Figures that 02 used to quote by hand, such as the in-degree range, the pre-2000 betweenness boundary, the coreness-9 works, the single component and the singleton share, are now computed from each graph.

- 02, degree distribution: "most works are cited once or twice, a much smaller number several times".
- 02, betweenness and publication year: the closing paragraph treats the split between older and recent works as a boundary. Read it against the two pre-2000 shares printed above it.
- 02, internal structure of communities: "a single dominant piece sheds one node at a time for hundreds of further rounds" and the "same long single-branch shape" for every community. The count of communities that shed one node at first split is computed.
- 02, appendix: the seed is "cited directly by hundreds of F1 papers".
- `index.qmd` describes 02 as "GN excluded", although 02 now includes Girvan-Newman.
- 03 reads `analysis/bibvik_node_table.csv`, which 02 writes. Read its prose against the new tables. The file has a new column, `community_gn_seed_filtered`, which 03 does not use.
- The comparison section in 02 ("What the seed filter changes") and every Girvan-Newman tab have not rendered on real runs yet.

## Related documents

- `gn_analysis/README.md` describes each Girvan-Newman script, its arguments and its outputs.
- `cluster_labelling/cluster_labelling_README.md` describes representative selection, labelling and the earlier methods.