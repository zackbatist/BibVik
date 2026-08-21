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
# Build an hclust-compatible merge tree from split events, over PIECES
# (blobs sized by member count), not individual node names — a piece
# that never splits again within the selected window stays a leaf,
# labeled by its size. This keeps the plotted tree's leaf count on the
# order of the number of splits used, not the whole 14k-node graph.
#
# Only the top_n_splits EARLIEST splits are used (coarsest structure —
# fewest components, closest to the full graph) unless top_n_splits==0.
#
# Construction: walk the selected splits in DECREASING round order
# (i.e. starting from the most-fragmented state within the window and
# working back toward the full graph). Each split, read this way, is a
# MERGE of its output pieces back into the piece that existed just
# before that round — which is exactly what hclust's merge matrix
# needs. A piece's hclust reference (negative = leaf position, positive
# = producing row index) is recorded the moment it's created, so later
# rows can look it up directly with no unresolved placeholders.
#
# Height = -round, so height increases as round decreases — required
# by hclust (ascending height toward the root) while keeping "smaller
# round = closer to the full graph = higher up the tree."
# ---------------------------------------------------------
splits_to_use <- if (top_n_splits > 0 && top_n_splits < length(splits)) {
  cat("Using earliest", top_n_splits, "of", length(splits),
      "splits (coarsest structure) for the plotted tree.\n")
  splits[seq_len(top_n_splits)]
} else {
  splits
}
splits_rev <- rev(splits_to_use)  # decreasing round order

next_id     <- 1L
piece_size  <- c()          # id (as character) -> size
name_to_id  <- new.env()    # node name -> current piece id
is_leaf     <- integer(0)   # ids that are genuine leaves (registered directly from a piece, never later a merge output... wait, see below)
hclust_ref  <- new.env()    # id (as character) -> hclust reference, set at creation
leaf_counter <- 0L

register_leaf <- function(members) {
  id <- next_id; next_id <<- next_id + 1L
  piece_size[as.character(id)] <<- length(members)
  is_leaf <<- c(is_leaf, id)
  for (nm in members) assign(nm, id, envir = name_to_id)
  leaf_counter <<- leaf_counter + 1L
  assign(as.character(id), -leaf_counter, envir = hclust_ref)
  id
}

get_id <- function(name) {
  v <- mget(name, envir = name_to_id, ifnotfound = NA, inherits = FALSE)[[1]]
  v  # NA if not yet seen — register_leaf() call site handles that
}

merge_a <- integer(0)
merge_b <- integer(0)   # store as hclust refs directly (signed), not raw ids
heights <- numeric(0)

for (s in splits_rev) {
  ids_here <- integer(0)
  for (p in s$pieces) {
    rep_name <- p[1]
    id <- get_id(rep_name)
    if (is.na(id)) id <- register_leaf(p)
    ids_here <- c(ids_here, id)
  }
  ids_here <- unique(ids_here)
  if (length(ids_here) < 2) next

  while (length(ids_here) >= 2) {
    a <- ids_here[1]; b <- ids_here[2]
    parent_id <- next_id; next_id <- next_id + 1L
    piece_size[as.character(parent_id)] <- piece_size[as.character(a)] + piece_size[as.character(b)]

    ref_a <- get(as.character(a), envir = hclust_ref)
    ref_b <- get(as.character(b), envir = hclust_ref)
    merge_a <- c(merge_a, ref_a)
    merge_b <- c(merge_b, ref_b)
    heights <- c(heights, -s$round)

    row_index <- length(merge_a)
    assign(as.character(parent_id), row_index, envir = hclust_ref)

    # repoint every name currently mapped to a or b onto parent_id
    for (nm in ls(name_to_id)) {
      v <- get(nm, envir = name_to_id)
      if (v == a || v == b) assign(nm, parent_id, envir = name_to_id)
    }

    ids_here <- c(parent_id, ids_here[-c(1, 2)])
  }
}

cat("Merges in plotted tree:", length(merge_a), "\n")
cat("Leaves in plotted tree (whole pieces, not individual nodes):", length(is_leaf), "\n")

if (length(merge_a) == 0) {
  stop("Could not build any merges from the selected splits — try a larger top_n_splits.")
}

leaf_size_by_pos <- vapply(is_leaf, function(id) piece_size[as.character(id)], numeric(1))
leaf_pos_order <- vapply(is_leaf, function(id) -get(as.character(id), envir = hclust_ref), integer(1))
leaf_labels <- character(length(is_leaf))
leaf_labels[leaf_pos_order] <- sprintf("n=%d", leaf_size_by_pos)

hc <- list(
  merge  = cbind(merge_a, merge_b),
  height = heights,
  order  = seq_along(leaf_labels),
  labels = leaf_labels,
  method = "girvan-newman-divisive"
)
class(hc) <- "hclust"

# ---------------------------------------------------------
# Plot. Labels off by default (unreadable at this leaf count unless
# top_n_splits is small) — turn on manually for a small subtree.
# ---------------------------------------------------------
png(file.path(output_dir, "gn_dendrogram.png"), width = 4000, height = 2000, res = 200)
plot(hc, labels = FALSE, hang = -1,
     main = sprintf("Girvan-Newman dendrogram (top %d splits, %d leaves)",
                     length(splits_to_use), length(is_leaf)),
     xlab = "", sub = "", ylab = "-Round (height increases away from full graph)")
dev.off()
cat("Wrote", file.path(output_dir, "gn_dendrogram.png"), "\n")

saveRDS(hc, file.path(output_dir, "gn_dendrogram.rds"))
cat("Wrote", file.path(output_dir, "gn_dendrogram.rds"), "— reload with readRDS() to replot,",
    "relabel, or pass to cutree() at any height/k.\n")

cat("Done.\n")