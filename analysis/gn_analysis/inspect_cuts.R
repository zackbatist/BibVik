#!/usr/bin/env Rscript
# ============================================================
# Two modes:
#
# 1. Coarse scan — component size distribution at several candidate
#    rounds, by replaying removal_log.csv against the original graph.
#
#      Rscript inspect_cuts.R scan <edgelist_csv> <removal_log_csv> <round1> <round2> ...
#
#    Example:
#      Rscript inspect_cuts.R scan ../../data/citation_edgelist.csv results/removal_log.csv 1000 3000 5000 8000 9000 9500 10000
#
# 2. Subcluster discovery — starting from a coarse round, track each of
#    its N largest components forward, round by round, until each one
#    first splits into 2+ pieces. Reports the round and resulting
#    sizes for each split, so nested/hierarchical structure (clusters
#    within clusters) is visible, not just one flat partition.
#
#      Rscript inspect_cuts.R subclusters <edgelist_csv> <removal_log_csv> <start_round> <n_components_to_track> <max_rounds_ahead>
#
#    Example: starting at round 5000, track the 5 largest components,
#    looking up to 3000 rounds ahead for each one's first split:
#      Rscript inspect_cuts.R subclusters ../../data/citation_edgelist.csv results/removal_log.csv 5000 5 3000
# ============================================================

suppressPackageStartupMessages(library(igraph))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 1) {
  stop("Usage: Rscript inspect_cuts.R <scan|subclusters> ...")
}
mode <- args[1]
args <- args[-1]

load_graph_and_log <- function(edgelist_file, removal_log_file) {
  edges_df <- read.csv(edgelist_file, stringsAsFactors = FALSE)
  g_full <- graph_from_data_frame(edges_df[, c("source", "target")], directed = TRUE)
  g_full <- as.undirected(g_full, mode = "collapse")
  cat(sprintf("Loaded graph: %d nodes, %d edges\n", vcount(g_full), ecount(g_full)))

  removal_log <- read.csv(removal_log_file, stringsAsFactors = FALSE)
  removal_log <- removal_log[order(removal_log$round), ]
  list(g = g_full, removal_log = removal_log)
}

# Remove edges (from_round+1)..to_round from g, in order, batched in one
# call rather than one delete_edges() per edge.
advance_to_round <- function(g, removal_log, from_round, to_round) {
  if (to_round <= from_round) return(g)
  batch <- removal_log[(from_round + 1):to_round, c("from", "to")]
  eids <- get_edge_ids(g, as.vector(t(as.matrix(batch))))
  eids <- eids[eids > 0]
  if (length(eids) > 0) g <- delete_edges(g, eids)
  g
}

if (mode == "scan") {

  edgelist_file <- args[1]
  removal_log_file <- args[2]
  target_rounds <- sort(unique(as.integer(args[3:length(args)])))

  loaded <- load_graph_and_log(edgelist_file, removal_log_file)
  g <- loaded$g
  removal_log <- loaded$removal_log
  if (max(target_rounds) > nrow(removal_log)) {
    stop("Requested round ", max(target_rounds), " exceeds removal_log length ", nrow(removal_log))
  }

  cat("\nround | n_components | sizes of 10 largest components | n_singletons\n")
  cat("------|--------------|--------------------------------|-------------\n")

  removed_so_far <- 0
  for (r in target_rounds) {
    g <- advance_to_round(g, removal_log, removed_so_far, r)
    removed_so_far <- r

    comp <- components(g)
    sizes <- sort(comp$csize, decreasing = TRUE)
    top10 <- head(sizes, 10)
    n_singletons <- sum(sizes == 1)

    cat(sprintf("%6d | %12d | %-32s | %d\n",
                r, comp$no, paste(top10, collapse = ","), n_singletons))
  }

} else if (mode == "subclusters") {

  edgelist_file <- args[1]
  removal_log_file <- args[2]
  start_round <- as.integer(args[3])
  n_track <- as.integer(args[4])
  max_ahead <- as.integer(args[5])

  loaded <- load_graph_and_log(edgelist_file, removal_log_file)
  g <- loaded$g
  removal_log <- loaded$removal_log
  end_round <- min(start_round + max_ahead, nrow(removal_log))

  g <- advance_to_round(g, removal_log, 0, start_round)
  comp0 <- components(g)
  sizes0 <- sort(comp0$csize, decreasing = TRUE)
  # component ids ordered largest-first, matching sizes0
  top_comp_ids <- order(comp0$csize, decreasing = TRUE)[1:n_track]

  cat(sprintf("\nAt round %d: tracking the %d largest components (sizes: %s)\n\n",
              start_round, n_track, paste(head(sizes0, n_track), collapse = ",")))

  member_names <- lapply(top_comp_ids, function(cid) V(g)$name[comp0$membership == cid])

  # Whether the given node set is still a single component after removing
  # edges up to round r (starting fresh from the full graph each time keeps
  # this simple and correct, at the cost of re-deriving g up to r — fine
  # given binary search only needs O(log(max_ahead)) evaluations).
  is_still_one_piece <- function(nodes_i, r) {
    g_r <- advance_to_round(loaded$g, removal_log, 0, r)
    sub <- induced_subgraph(g_r, vids = nodes_i)
    components(sub)$no == 1
  }

  for (i in seq_along(top_comp_ids)) {
    nodes_i <- member_names[[i]]
    cat(sprintf("--- Component #%d (size %d at round %d) ---\n", i, length(nodes_i), start_round))

    if (length(nodes_i) == 1) {
      cat("  Already a singleton — nothing to split.\n")
      next
    }

    if (is_still_one_piece(nodes_i, end_round)) {
      cat(sprintf("  Does not split within %d rounds ahead (still 1 piece at round %d)\n",
                  max_ahead, end_round))
      next
    }

    # Binary search for the first round at which it's no longer 1 piece.
    # Monotonic: once split, removing more edges never re-merges it.
    lo <- start_round  # known: 1 piece
    hi <- end_round     # known: split (checked above)
    while (hi - lo > 1) {
      mid <- lo + (hi - lo) %/% 2
      if (is_still_one_piece(nodes_i, mid)) {
        lo <- mid
      } else {
        hi <- mid
      }
    }
    split_round <- hi

    g_r <- advance_to_round(loaded$g, removal_log, 0, split_round)
    sub <- induced_subgraph(g_r, vids = nodes_i)
    sub_sizes <- sort(components(sub)$csize, decreasing = TRUE)
    cat(sprintf("  First splits at round %d, into %d pieces, sizes: %s\n",
                split_round, length(sub_sizes), paste(sub_sizes, collapse = ",")))
  }

} else {
  stop("Unknown mode '", mode, "' — use 'scan' or 'subclusters'")
}