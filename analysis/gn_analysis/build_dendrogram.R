#!/usr/bin/env Rscript
# ============================================================
# Reconstruct and plot the Girvan-Newman dendrogram from removal_log.csv.
#
# Usage:
#   Rscript build_dendrogram.R path/to/edgelist.csv path/to/removal_log.csv path/to/output_dir
#
# GN is divisive (top-down): the run removed edges from the full graph,
# fragmenting it over time. removal_log.csv records the order (round,
# from, to). This script replays that order forward and records every
# SPLIT event — a round where removing an edge breaks one component into
# two or more — since those splits are what a dendrogram actually shows.
# Most rounds don't split anything (the removed edge sits inside an
# already-fragmented piece, or its removal doesn't disconnect anything
# yet); only split rounds matter for tree structure.
#
# Output is an hclust-compatible merge tree, built bottom-up from the
# split events read in reverse (a divisive top-down process, replayed
# backwards, is a bottom-up agglomeration — the same structure hclust
# expects). "Height" in the plot is the round number at which two
# pieces were still joined, i.e. distance from full graph.
#
# This does not pick a cut point. It only builds and plots the tree
# structure. Cutting decisions are left to whoever's looking at the
# plot, same as any dendrogram.
# ============================================================

suppressPackageStartupMessages({
  library(igraph)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 3) {
  stop("Usage: Rscript build_dendrogram.R <edgelist_csv> <removal_log_csv> <output_dir>")
}
edgelist_file   <- args[1]
removal_log_file <- args[2]
output_dir      <- args[3]

# Optional 4th arg: only plot the top N split events (fewest components),
# since the full tree on a ~14k-node graph is unreadable as one figure.
# Default: top 300 splits. Pass 0 for no cap (full tree — slow to plot
# and likely unreadable, but written to disk either way).
top_n_splits <- if (length(args) >= 4) as.integer(args[4]) else 300L

dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)

cat("Loading edge list from", edgelist_file, "\n")
edges_df <- read.csv(edgelist_file, stringsAsFactors = FALSE)
g <- graph_from_data_frame(edges_df[, c("source", "target")], directed = TRUE)
if (is_directed(g)) g <- as.undirected(g, mode = "collapse")
cat("Graph loaded:", vcount(g), "nodes,", ecount(g), "edges\n")

cat("Loading removal log from", removal_log_file, "\n")
removal_log <- read.csv(removal_log_file, stringsAsFactors = FALSE)
removal_log <- removal_log[order(removal_log$round), ]
cat("Removal log:", nrow(removal_log), "rounds\n")

# ---------------------------------------------------------
# Replay removals forward, recording every split event.
# A split event: round r, the set of node names in each resulting
# piece when a component divides. We track components by an integer
# label per node, updated each time a division happens.
# ---------------------------------------------------------
cat("Replaying removals to find split events...\n")

node_names <- V(g)$name
comp_id <- rep(1L, length(node_names))
names(comp_id) <- node_names
next_comp_id <- 2L

g_live <- g

splits <- list()  # each entry: list(round=, pieces=list of character vectors of node names)

pb_step <- max(1, floor(nrow(removal_log) / 20))

for (i in seq_len(nrow(removal_log))) {
  r <- removal_log$round[i]
  from <- removal_log$from[i]
  to   <- removal_log$to[i]

  eid <- get.edge.ids(g_live, c(from, to))
  if (eid == 0) next  # edge already gone (shouldn't happen if log is well-formed, but guard anyway)
  g_live <- delete_edges(g_live, eid)

  # Only check the component containing 'from'/'to' for a split —
  # cheaper than recomputing global components every round on a
  # 14k-node graph over tens of thousands of rounds.
  parent_comp <- comp_id[[from]]
  members <- names(comp_id)[comp_id == parent_comp]
  sub_g <- induced_subgraph(g_live, vids = members)
  sub_comp <- components(sub_g)

  if (sub_comp$no > 1) {
    pieces <- split(V(sub_g)$name, sub_comp$membership)
    # relabel: first piece keeps parent_comp's id, rest get new ids
    for (k in seq_along(pieces)) {
      piece_names <- pieces[[k]]
      new_id <- if (k == 1) parent_comp else { id <- next_comp_id; next_comp_id <<- next_comp_id + 1L; id }
      comp_id[piece_names] <- new_id
    }
    splits[[length(splits) + 1]] <- list(round = r, pieces = pieces)
  }

  if (i %% pb_step == 0) cat("  ...", i, "/", nrow(removal_log), "rounds replayed,",
                              length(splits), "splits found so far\n")
}

cat("Total split events:", length(splits), "\n")

if (length(splits) == 0) {
  stop("No split events found — check that removal_log_file matches edgelist_file (same run).")
}

# ---------------------------------------------------------
# Save the raw split events regardless of plotting — this is the
# reusable artifact; the plot below is a rendering choice on top of it.
# ---------------------------------------------------------
split_summary <- do.call(rbind, lapply(splits, function(s) {
  data.frame(round = s$round, n_pieces = length(s$pieces),
             piece_sizes = paste(sapply(s$pieces, length), collapse = ","))
}))
write.csv(split_summary, file.path(output_dir, "split_events.csv"), row.names = FALSE)
cat("Wrote", file.path(output_dir, "split_events.csv"), "\n")

# ---------------------------------------------------------
# Build an hclust-compatible merge tree from the split events, read in
# reverse. Only feasible/readable for the top_n_splits earliest splits
# (== the coarsest structure, fewest components) unless top_n_splits==0.
# ---------------------------------------------------------
splits_to_use <- if (top_n_splits > 0 && top_n_splits < length(splits)) {
  cat("Using earliest", top_n_splits, "of", length(splits),
      "splits (coarsest structure) for the plotted tree.\n")
  splits[seq_len(top_n_splits)]
} else {
  splits
}

# Collect the set of node names actually touched by the splits we're
# using, to build a compact hclust object over just those leaves rather
# than all 14k nodes (which would make labels/plotting meaningless).
touched_names <- unique(unlist(lapply(splits_to_use, function(s) unlist(s$pieces))))
cat("Leaves in plotted tree:", length(touched_names), "\n")

# hclust wants: merge matrix (bottom-up), height vector, order, labels.
# We build this by walking splits_to_use in REVERSE (last split first —
# smallest pieces merge first, going back toward the full graph).
rev_splits <- rev(splits_to_use)

# cluster_of[name] tracks which working node (leaf index, or negative
# internal merge index) currently contains that name.
leaf_idx <- setNames(seq_along(touched_names), touched_names)
cluster_of <- leaf_idx  # positive = leaf index; will become negative merge refs conceptually via merge matrix

merge_mat <- matrix(NA_integer_, nrow = length(rev_splits), ncol = 2)
heights   <- numeric(length(rev_splits))
# track, for each currently-active cluster label (positive=leaf idx,
# or i for the i-th merge row), which names belong to it
active_members <- as.list(setNames(touched_names, touched_names))  # name -> name initially; replaced below
active_members <- lapply(seq_along(touched_names), function(i) touched_names[i])
names(active_members) <- as.character(leaf_idx)  # keyed by current cluster label

cur_label_of <- leaf_idx  # name -> current active cluster label

for (m in seq_along(rev_splits)) {
  s <- rev_splits[[m]]
  # pieces that split apart going forward are, in reverse, pieces that
  # MERGE at this step. Only pieces with >=1 touched member matter.
  piece_labels <- unique(unlist(lapply(s$pieces, function(p) {
    members_here <- intersect(p, names(cur_label_of))
    if (length(members_here) == 0) return(NULL)
    unique(cur_label_of[members_here])
  })))
  piece_labels <- piece_labels[!is.na(piece_labels)]

  if (length(piece_labels) < 2) next  # nothing to merge at this step for our tracked leaves

  # Merge pairwise if more than 2 pieces reunite at once (rare but
  # possible) — chain them into sequential binary merges at the same
  # height for simplicity.
  while (length(piece_labels) >= 2) {
    a <- piece_labels[1]; b <- piece_labels[2]
    merge_mat[m, ] <- c(a, b)
    heights[m] <- s$round
    new_label <- m  # hclust convention: positive = merge row index, will negate leaves below
    all_members <- unlist(c(active_members[[as.character(a)]], active_members[[as.character(b)]]))
    active_members[[as.character(new_label)]] <- all_members
    for (nm in all_members) cur_label_of[nm] <- new_label
    piece_labels <- c(new_label, piece_labels[-c(1, 2)])
    if (length(piece_labels) < 2) break
  }
}

# Convert to hclust's signed convention: negative = original leaf,
# positive = row index of an earlier merge.
merge_signed <- merge_mat
for (i in seq_len(nrow(merge_mat))) {
  for (j in 1:2) {
    v <- merge_mat[i, j]
    if (is.na(v)) next
    merge_signed[i, j] <- if (v <= length(touched_names)) -v else (v - length(touched_names))
  }
}

valid_rows <- !is.na(merge_signed[, 1])
merge_signed <- merge_signed[valid_rows, , drop = FALSE]
heights_valid <- heights[valid_rows]

if (nrow(merge_signed) == 0) {
  stop("Could not build any merges from the selected splits — try a larger top_n_splits.")
}

hc <- list(
  merge  = merge_signed,
  height = heights_valid,
  order  = order(leaf_idx),   # placeholder order; hclust plot recomputes a sensible order itself via as.dendrogram
  labels = touched_names,
  method = "girvan-newman-divisive"
)
class(hc) <- "hclust"

# ---------------------------------------------------------
# Plot. Labels off by default (unreadable at this leaf count) —
# turn on manually if plotting a small subtree.
# ---------------------------------------------------------
png(file.path(output_dir, "gn_dendrogram.png"), width = 4000, height = 2000, res = 200)
plot(hc, labels = FALSE, hang = -1,
     main = sprintf("Girvan-Newman dendrogram (top %d splits, %d leaves)",
                     length(splits_to_use), length(touched_names)),
     xlab = "", sub = "", ylab = "Round (distance from full graph)")
dev.off()
cat("Wrote", file.path(output_dir, "gn_dendrogram.png"), "\n")

saveRDS(hc, file.path(output_dir, "gn_dendrogram.rds"))
cat("Wrote", file.path(output_dir, "gn_dendrogram.rds"), "— reload with readRDS() to replot,",
    "zoom into a subtree, or pass to cutree() at any height/k.\n")

cat("Done.\n")
