# Girvan-Newman — standalone

Not part of the Quarto render chain. Nothing in `analysis/*.qmd` runs
anything here automatically. GN on this graph takes a long time
(multi-day, order-of-magnitude guess, not benchmarked on this graph yet),
so it runs unattended on the remote server instead, separate from
document rendering.

This reimplements Girvan-Newman's outer loop by hand instead of calling
`igraph::cluster_edge_betweenness()`. The betweenness computation itself
is still igraph's C implementation (`edge_betweenness()`, Brandes'
algorithm) — the custom part is just the loop around it: remove one
edge, checkpoint, repeat. `cluster_edge_betweenness()` runs that same
loop internally in one call that R can't interrupt, and only returns
once the full dendrogram is done. This script gives that up in exchange
for being able to checkpoint: R gets control back after every single
edge removal, so state can be written to disk and a multi-day run
survives being killed, disconnected, or paused on purpose.

It keeps two things beyond the best-modularity partition:
`results/modularity_trace.csv` (modularity and component count per
round) and `results/removal_log.csv` (which edge got removed each
round, in order). The removal log is enough to rebuild the full
dendrogram afterward — replay removals 1..k against the original graph
and you get the exact component structure at k removals, for any k, not
just the one best-modularity cut in `communities.csv`. It's not stored
as an igraph `communities` object, so there's no `plot_dendrogram()` for
free, but the same information is in `removal_log.csv` if you need to
build one.

## Input format

Reads a plain two-column edge list CSV (header: `source,target`), not
GraphML. Some igraph builds ship with GraphML support compiled out
(missing `libxml2` at build time), and that can't always be fixed
without root access on a shared server. `data/citation_edgelist.csv`
already exists in this repo in the right format — use that.

## Usage (remote server, inside tmux)

```bash
cd analysis/gn_analysis
./start_gn.sh ../../data/citation_edgelist.csv results/
```

- Detach: `Ctrl+b`, `d`
- Reattach: `tmux attach -t girvan_newman`
- Stop cleanly (checkpoints, then exits): `touch results/STOP`
- Resume after a stop or crash: rerun the same `start_gn.sh` command,
  it picks up from `results/state.rds`
- Watch progress live: `tail -f results/run.log`

## Output

- `results/communities.csv` — `node_id, community_id`. Main file the
  rest of the project should read.
- `results/removal_log.csv` — `round, from, to`. Full removal order;
  replay it to get the partition at any cut point.
- `results/final_edgelist.csv` — the input edge list, copied through
  unchanged, for reference alongside `communities.csv`.
- `results/state.rds`, `results/run.log`, `results/modularity_trace.csv`
  — working files, not for git.

`02_network_structure.qmd` joins `results/communities.csv` in if it
exists. GN is an optional input, not something the rest of the pipeline
waits on. That file currently loads the graph from an absolute path
(`/home/zack/models/BibVik_output/citation_graph.graphml`) rather than
`data/` — worth checking whether that's stale, and whether it hits the
same GraphML/libxml2 problem this script ran into.

## Keep out of git

Already in `.gitignore`:

```
analysis/gn_analysis/results/state.rds
analysis/gn_analysis/results/run.log
analysis/gn_analysis/results/session_stdout.log
analysis/gn_analysis/results/final_graph.graphml
```

Note `final_graph.graphml` there is now stale — this script writes
`final_edgelist.csv` instead, which isn't excluded (small, fine to
commit alongside `communities.csv` and `removal_log.csv`).