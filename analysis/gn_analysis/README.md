# Girvan-Newman — standalone

This directory is **not part of the Quarto render chain**. Nothing in
`analysis/*.qmd` executes anything here automatically. It exists because
GN on this graph runs for a long time (multi-day, order-of-magnitude
estimate — not benchmarked on this specific graph yet) and needs to run
unattended on the remote server, independent of document rendering.

This is a from-scratch, manually-checkpointed implementation of
Girvan-Newman — it does **not** use `igraph::cluster_edge_betweenness()`.
That was a deliberate choice: `cluster_edge_betweenness()` computes the
full dendrogram in one non-interruptible call, which makes it impossible
to checkpoint mid-run. This implementation removes edges one at a time in
an explicit loop, checkpointing after every removal, so a multi-day run
survives being killed, disconnected, or deliberately paused.

## Usage (on the remote server, inside tmux)

```bash
cd analysis/gn_analysis
./start_gn.sh /home/zack/models/BibVik_output/citation_graph.graphml results/
```

(Same graph file `02_network_structure.qmd` loads — see its `load-graph`
chunk. Note that file is directed; this script's betweenness/removal loop
runs on the undirected projection, consistent with how Louvain/Leiden/k-core
treat it elsewhere in the project — convert first if the source file isn't
already undirected.)

- Detach: `Ctrl+b`, `d`
- Reattach: `tmux attach -t girvan_newman`
- Stop cleanly (checkpoints, then exits): `touch results/STOP`
- Resume after a stop or crash: rerun the same `start_gn.sh` command —
  it picks up automatically from `results/state.rds`
- Watch progress live: `tail -f results/run.log`

## Output

- `results/communities.csv` — `node_id, community_id`. The only file this
  produces that the rest of the project should read. Small, portable,
  git-friendly.
- `results/final_graph.graphml` — the original graph annotated with the
  chosen community assignment, for use outside R if needed.
- `results/state.rds`, `results/run.log`, `results/modularity_trace.csv` —
  working files. Not meant for git; see `.gitignore` note below.

`02_network_structure.qmd` joins `results/communities.csv` in if and only
if that file exists — GN is an optional input to the rest of the pipeline,
not a dependency it waits on.

## Keep out of git

Add to `.gitignore` (or the project's, if not already covered):

```
analysis/gn_analysis/results/state.rds
analysis/gn_analysis/results/run.log
analysis/gn_analysis/results/session_stdout.log
analysis/gn_analysis/results/final_graph.graphml
```

`results/communities.csv` is small and worth committing once produced.
