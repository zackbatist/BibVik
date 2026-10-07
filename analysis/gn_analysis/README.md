# Girvan-Newman

This reimplements Girvan-Newman's outer loop by hand instead of calling `igraph::cluster_edge_betweenness()`. The betweenness computation itself is still igraph's C implementation (`edge_betweenness()`, Brandes' algorithm). The custom part is just the loop around it: remove one edge, checkpoint, repeat. `cluster_edge_betweenness()` runs that same loop internally in one call that R can't interrupt, and only returns once the full dendrogram is done. This script gives that up in exchange for being able to checkpoint: R gets control back after every single edge removal, so state can be written to disk and a long-duration run survives being killed, disconnected, or paused on purpose.

It keeps two things beyond the best-modularity partition: `modularity_trace.csv` (modularity and component count per round) and `removal_log.csv` (which edge got removed each round, in order). The removal log is enough to rebuild the full dendrogram afterward; replay removals 1..k against the original graph and you get the exact component structure at k removals, for any k, not just the one best-modularity cut in `communities.csv`. It's not stored as an igraph `communities` object, so there's no `plot_dendrogram()` for free, but the same information is in `removal_log.csv` if you need to build one.

A render never runs Girvan-Newman. GN on this graph takes a long time (several hours, and in earlier iterations, more than one full day), so it runs unattended on the remote server, separate from document rendering. `02_network_structure.qmd` reads finished runs from `results/<run>/` and builds the dendrogram and the subcluster files itself, only when they are missing or older than the removal log or the cut file.

Starting both graphs together, waiting for them, and running every step after them is covered in `../README.md`. This file describes the scripts, their arguments and their outputs.

## Scripts

| Script | Purpose | Arguments |
|---|---|---|
| `run_gn_analysis.R` | The Girvan-Newman loop, with checkpoints, logging and a `STOP` file | `<edgelist_csv> <checkpoint_dir>` |
| `start_gn.sh` | Starts one run in the tmux session `girvan_newman` | `<edgelist_csv> <checkpoint_dir>` |
| `start_gn_annotated_noseed.sh` | The same launcher, in the session `girvan_newman_annotated_noseed` | `<edgelist_csv> <checkpoint_dir>` |
| `generate_multi_cut_communities.R` | Detects every sustained fragmentation onset in the modularity trace and writes the partition at each | `<edgelist_csv> <removal_log_csv> <modularity_trace_csv> <output_dir> [window] [min_rate]` |
| `inspect_cuts.R` | Manual inspection. `scan` prints component sizes at rounds you choose. `subclusters` tracks the largest components forward to their first split. | `scan <edgelist_csv> <removal_log_csv> <round> ...` or `subclusters <edgelist_csv> <removal_log_csv> <start_round> <n_components> <max_rounds_ahead>` |

`window` defaults to 100 rounds and `min_rate` to 0.9 new components per round.

## Input format

Reads a plain two-column edge list CSV (header: `source,target`), not GraphML. Some igraph builds ship with GraphML support compiled out (missing `libxml2` at build time), and that can't always be fixed without root access, e.g., on a professionally maintained shared server.

Two lists are used.

- `../../data/citation_edgelist_annotated.csv` is the annotated graph with the seed paper (`lund2021`).
- `edgelist_annotated_no_seed.csv`, in this directory and committed, is the same list without the seed and its outgoing edges. It has 20,884 rows against 21,457.

Both keep Windows line endings.

## Running one graph by hand

```bash
cd analysis/gn_analysis
./start_gn.sh ../../data/citation_edgelist_annotated.csv results/annotated/
```

- Detach: `Ctrl+b`, `d`
- Reattach: `tmux attach -t girvan_newman`
- Stop cleanly (checkpoints, removes the file, then exits): `touch results/annotated/STOP`
- Resume after a stop or crash: rerun the same command. It picks up from `results/annotated/state.rds`.
- Progress: `cat results/annotated/run.log`

`start_gn.sh` uses one fixed session name, and tmux matches session names by prefix. It therefore reports "already running" and starts nothing whenever another session whose name begins with `girvan_newman` exists, such as `girvan_newman_noseed`. Use the launch command in `../README.md` to run both graphs.

## Output

Each run writes one folder, `results/<run>/`.

| File | Content | Read by |
|---|---|---|
| `communities.csv` | `node_id, community_id` at the best-scoring round. Modularity is scored against the shrinking graph, so this is not the cut the project uses. | 02, for the share of singleton communities at that cut |
| `modularity_trace.csv` | `round, edges_removed, n_components, modularity` | cut detection, 02 |
| `removal_log.csv` | `round, from, to`. The full removal order. Replay it to get the partition at any cut point. | cut detection, 02 (dendrogram and subclusters), `inspect_cuts.R` |
| `final_edgelist.csv` | The edge list the run used, copied through unchanged | reference |
| `state.rds`, `run.log`, `session_stdout.log` | Working files | the run itself |
| `multi_cut/communities_round_<N>.csv` | `node_id, community_id` at each detected onset round. The smallest N is the working partition. | 02, representative selection |
| `multi_cut/multi_cut_summary.csv` | `round, n_communities, n_singletons, largest_community` | 02 |
| `dendrogram/gn_dendrogram.png`, `.rds`, `split_events.csv` | The tree, up to the earliest onset. Built by 02 when it renders. | 02 |
| `subclusters/subcluster_splits.csv`, `subcluster_pieces.csv` | The removal log replayed inside every community of 10 or more papers. `subcluster_splits.csv` has one row per community and `subcluster_pieces.csv` one row per piece, with its size and top paper. Built by 02 when it renders. | 02 |
| `labels/` | `representatives.csv`, `cluster_labels.csv`, `node_labels.csv` from `../cluster_labelling/` | 02 |

02 reads each run from its folder. The files to commit are listed in `../README.md`.

## After a run, by hand

These are the steps the chain runs for each graph. Set `D` and `E` for the graph you want.

```bash
cd analysis/gn_analysis
D=results/annotated; E=../../data/citation_edgelist_annotated.csv
# or: D=results/annotated_no_seed; E=edgelist_annotated_no_seed.csv
rm -rf $D/multi_cut $D/dendrogram $D/subclusters
Rscript generate_multi_cut_communities.R $E $D/removal_log.csv $D/modularity_trace.csv $D/multi_cut
N=$(ls $D/multi_cut/communities_round_*.csv | sed -E 's/.*_round_([0-9]+)\.csv$/\1/' | sort -n | head -1); echo "earliest cut: $N"
```

The `rm -rf` matters. Detection never removes old `communities_round_<N>.csv` files, and a leftover file with a smaller round number would be taken as the earliest cut. 02 rebuilds the dendrogram and the subcluster files on its next render. Labelling continues in `../cluster_labelling/cluster_labelling_README.md`.

## Environment

- The server has igraph 1.6.0. These scripts call `get.edge.ids` and `as.undirected`, which that version has. The newer names `get_edge_ids` and `as_undirected` came in igraph 2.0 and do not exist there.
- `STOP` is a file in the run folder. The run checks for it after every round, checkpoints, deletes it and exits.
- Run each graph from this directory, because the scripts and the example paths are relative to it.

## Keep out of git

The checkpoint, the logs and the files 02 rebuilds stay out of git. `.gitignore` covers them with these lines, and its `*.log` lines cover the logs.

```
analysis/gn_analysis/results/**/state.rds
analysis/gn_analysis/results/**/subclusters/*.csv
analysis/gn_analysis/results/**/gn_dendrogram.rds
analysis/gn_analysis/results/**/split_events.csv
```

The files to track are listed in `../README.md`.