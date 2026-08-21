#!/usr/bin/env Rscript
# ============================================================
# Reconstruct and plot the Girvan-Newman dendrogram from removal_log.csv.
#
# Usage:
#   Rscript build_dendrogram.R path/to/edgelist.csv path/to/removal_log.csv path/to/output_dir [top_n_splits] [mark_round] [min_round] [modularity_trace_csv] [onset_window] [onset_min_rate]
#
# top_n_splits (default 300): cap on how many splits to include, taken
#   from the earliest available (after min_round filtering, if used).
# mark_round (default 0/off): draw a red dashed line at this round's
#   position on the tree. Ignored if min_round is set.
# min_round (default 0/off): only include splits at or after this
#   round, for zooming into a region. Only works as a fresh build
#   starting from that round; can't extract a subtree from an
#   already-built larger tree. R's cut(as.dendrogram(...)) fails on
#   this graph's shape (a round-based cut can produce a disconnected
#   forest, which plot.hclust() can't render). To see what happens
#   inside a specific community after a given round, use
#   build_subcluster_trees.R instead. It builds one tree per real
#   community, so it can't hit this problem.
# modularity_trace_csv (default unset): path to modularity_trace.csv.
#   If given, this script computes the earliest sustained
#   fragmentation-onset round from that trace (same rule as
#   generate_multi_cut_communities.R: n_components rising about 1 per
#   round, sustained for onset_window (default 100) rounds at rate
#   at least onset_min_rate (default 0.9)) and stops the tree there.
#   This computation is the only source of a cut round in this script.
#   It uses the earliest onset because that's the only one that
#   produces a singleton-free partition on this corpus (see
#   02_network_structure.qmd for the comparison across all detected
#   onsets). Without this arg, top_n_splits is the only stopping
#   control and this script makes no cut decision.
#
# GN is divisive (top-down): the run removed edges from the full graph,
# fragmenting it over time. removal_log.csv records the order (round,
# from, to). This script replays that order forward and records every
# SPLIT event, a round where removing an edge breaks one component into
# two or more, since those splits are what a dendrogram actually shows.
# Most rounds don't split anything (the removed edge sits inside an
# already-fragmented piece, or its removal doesn't disconnect anything
# yet); only split rounds matter for tree structure.
#
# Output is an hclust-compatible merge tree, built bottom-up from the
# split events read in reverse (a divisive top-down process, replayed
# backwards, is a bottom-up agglomeration, the same structure hclust
# expects). Height in the plot is merge order (1st, 2nd, ...), not
# round number. Raw round compresses the tree into an unreadable band
# since splits are unevenly spaced (see the heights comment below). The
# real round per merge is kept separately in round_at_height, saved
# alongside the tree in the .rds output.
#
# Pass modularity_trace_csv to have this script compute a cut round.
# Without it, this script only builds and plots tree structure and
# makes no cut decision. 02_network_structure.qmd documents and
# justifies the same cut round this script computes.

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
# Default: top 300 splits. Pass 0 for no cap (full tree, slow to plot
# and likely unreadable, but written to disk either way).
top_n_splits <- if (length(args) >= 4) as.integer(args[4]) else 300L

# Optional 7th arg: modularity_trace_csv, path to modularity_trace.csv
# (written by run_gn_analysis.R). If given, the tree is stopped at the
# EARLIEST sustained fragmentation-onset round, computed fresh from
# that trace using the same detection rule as
# generate_multi_cut_communities.R (a round after which n_components
# rises by ~1 every round, sustained for 100+ consecutive rounds). This
# is not a typed-in round number or a filename encoding one. It's
# recomputed from the actual data every run, so it stays correct if the
# GN run or detection parameters ever change. The earliest onset is
# used specifically because, on this corpus, it was established (see
# 02_network_structure.qmd's Girvan-Newman write-up) to be the only one
# producing a singleton-free partition. Later onsets are progressively
# more degenerate, not equally-valid alternatives.
max_round <- 0L
if (length(args) >= 7) {
  trace_file <- args[7]
  if (!file.exists(trace_file)) {
    stop("modularity_trace_csv '", trace_file, "' does not exist.")
  }
  onset_window   <- if (length(args) >= 8) as.integer(args[8]) else 100L
  onset_min_rate <- if (length(args) >= 9) as.numeric(args[9]) else 0.9

  detect_onsets <- function(trace, window, min_rate) {
    trace <- trace[order(trace$round), ]
    trace$comp_delta <- c(NA, diff(trace$n_components))
    n <- nrow(trace)
    is_run_start <- rep(FALSE, n)
    for (i in seq_len(n - window)) {
      if (is.na(trace$comp_delta[i])) next
      future_avg <- mean(trace$comp_delta[(i + 1):(i + window)], na.rm = TRUE)
      if (future_avg >= min_rate) is_run_start[i] <- TRUE
    }
    run_start_rounds <- trace$round[is_run_start]
    if (length(run_start_rounds) == 0) return(integer(0))
    gaps <- c(TRUE, diff(run_start_rounds) > window)
    run_start_rounds[gaps]
  }

  trace <- read.csv(trace_file, stringsAsFactors = FALSE)
  onsets <- detect_onsets(trace, onset_window, onset_min_rate)
  if (length(onsets) == 0) {
    stop("No sustained fragmentation onset detected in ", trace_file,
         " with window=", onset_window, ", min_rate=", onset_min_rate)
  }
  max_round <- min(onsets)
  cat("Detected", length(onsets), "onset(s) from", trace_file, ":",
      paste(onsets, collapse = ", "), "- using earliest:", max_round, "\n")
}

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

if (max_round > 0) {
  removal_log <- removal_log[removal_log$round <= max_round, ]
  cat("Restricted to round <=", max_round, ":", nrow(removal_log), "rows.",
      "Tree leaves will be exactly the real connected components at round", max_round, ".\n")
}

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

  # Only check the component containing 'from'/'to' for a split.
  # Cheaper than recomputing global components every round on a
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
  stop("No split events found. Check that removal_log_file matches edgelist_file (same run).")
}

# ---------------------------------------------------------
# Save the raw split events regardless of plotting. This is the
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
# (blobs sized by member count), not individual node names. A piece
# that never splits again within the selected window stays a leaf,
# labeled by its size. This keeps the plotted tree's leaf count on the
# order of the number of splits used, not the whole 14k-node graph.
#
# Only the top_n_splits EARLIEST splits are used (coarsest structure,
# fewest components, closest to the full graph) unless top_n_splits==0.
#
# Construction: walk the selected splits in DECREASING round order
# (i.e. starting from the most-fragmented state within the window and
# working back toward the full graph). Each split, read this way, is a
# MERGE of its output pieces back into the piece that existed just
# before that round. That's exactly what hclust's merge matrix
# needs. A piece's hclust reference (negative = leaf position, positive
# = producing row index) is recorded the moment it's created, so later
# rows can look it up directly with no unresolved placeholders.
#
# Height = -round, so height increases as round decreases. Required
# by hclust (ascending height toward the root) while keeping "smaller
# round = closer to the full graph = higher up the tree."
# ---------------------------------------------------------
# Optional 6th arg: min_round. After building the tree, zoom the
# plotted output to only the portion at or after this round (e.g. the
# branching region after a known fragmentation-onset round). This is
# NOT applied to the input splits: piece lineage tracking depends on
# processing splits in true order from the full graph, so filtering
# the input breaks it (a piece referenced by a later split may never
# get registered as a leaf if its origin split was filtered out,
# producing more leaves than merges+1 and a malformed tree). Filtering
# happens after construction instead. See the subtree extraction
# below, near the plotting code.
min_round <- if (length(args) >= 6) as.integer(args[6]) else 0L

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
is_leaf     <- integer(0)   # ids of genuine leaves (registered directly from a piece)
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

get_id_any_member <- function(piece_members) {
  # Check ALL members for an existing id, not just the first. Using
  # only piece[1] was the actual bug: the giant remaining component's
  # first name changes every round (induced_subgraph re-orders
  # vertices), so it looked like a brand-new piece almost every time
  # instead of the same shrinking lineage. This inflated leaf count
  # toward the total node count instead of staying near the split count.
  for (nm in piece_members) {
    v <- mget(nm, envir = name_to_id, ifnotfound = NA, inherits = FALSE)[[1]]
    if (!is.na(v)) return(v)
  }
  NA_integer_
}

merge_a <- integer(0)
merge_b <- integer(0)   # store as hclust refs directly (signed), not raw ids
heights <- numeric(0)
round_at_height <- integer(0)  # parallel to heights, the real round each merge happened at

for (s in splits_rev) {
  ids_here <- integer(0)
  for (p in s$pieces) {
    id <- get_id_any_member(p)
    if (is.na(id)) {
      id <- register_leaf(p)
    } else {
      # Piece already has an id via some member(s), but this same split
      # may introduce members not yet indexed under that id (freshly
      # separated off a still-shrinking lineage). Index them too so
      # future lookups for THOSE names also resolve correctly.
      for (nm in p) {
        v <- mget(nm, envir = name_to_id, ifnotfound = NA, inherits = FALSE)[[1]]
        if (is.na(v)) assign(nm, id, envir = name_to_id)
      }
    }
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
    # Height is merge order (1, 2, 3, ...), not raw round number. Raw
    # round compresses the whole tree into an unreadable band: splits
    # within any fixed window are typically only a few hundred rounds
    # apart out of a ~24,600-round run, so a linear round-based axis
    # puts almost every merge at nearly the same height. Merge order
    # preserves the actual sequence and spaces every merge evenly, which
    # is what makes the tree shape legible. The tradeoff is that height
    # no longer represents literal round-distance, only relative order.
    # round_at_height keeps the real round number available per merge.
    heights <- c(heights, length(merge_a))
    round_at_height <- c(round_at_height, s$round)

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
cat("Round range covered:", min(round_at_height), "-", max(round_at_height), "\n")

if (length(merge_a) == 0) {
  stop("Could not build any merges from the selected splits. Try a larger top_n_splits.")
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
# Zoom: if min_round is set, extract the subtree covering only merges
# at or after that round, using R's built-in cut(as.dendrogram(...)).
# Cutting a dendrogram at a height is a well-defined, tested operation;
# reconstructing a valid hclust object by hand from a row subset is
# easy to get subtly wrong (as the earlier input-filtering attempt
# did). The cut height is the merge-order value of the last merge
# before min_round, so everything below that height becomes separate
# small subtrees. Keep only the ones whose own root height falls at or
# after min_round.
# ---------------------------------------------------------
plot_hc <- hc
plot_leaf_count <- length(leaf_labels)
if (min_round > 0) {
  if (min_round > max(round_at_height)) {
    stop("min_round ", min_round, " is beyond the built tree's round range (",
         min(round_at_height), "-", max(round_at_height),
         "). Increase top_n_splits so the tree reaches that round.")
  }
  cut_height <- heights[which.min(abs(round_at_height - min_round))] - 1
  dend <- as.dendrogram(hc)
  cut_result <- cut(dend, h = cut_height)
  # cut() returns $upper (the trimmed top, not useful here) and
  # $lower (a list of subtrees below the cut). We want whichever
  # lower subtree(s) actually contain the branching we're zooming to.
  # Since we're zooming to "everything after min_round", and the tree
  # is single-rooted, the relevant content is the single subtree whose
  # root height is closest to (at or above) our target. Take the
  # largest lower subtree as the zoomed view.
  lower_sizes <- vapply(cut_result$lower, function(d) attr(d, "members"), integer(1))
  zoomed_dend <- cut_result$lower[[which.max(lower_sizes)]]
  plot_hc <- zoomed_dend  # a dendrogram object, not hclust. plot() dispatches correctly on both
  plot_leaf_count <- attr(zoomed_dend, "members")
  cat("Zoomed to subtree at round >=", min_round, "(cut height", cut_height, "):",
      plot_leaf_count, "leaves.\n")
}

# ---------------------------------------------------------
# Plot. Height is merge order (evenly spaced, legible), not raw round.
# See the comment above where heights is built. round_at_height gives
# the real round for each merge if needed elsewhere (e.g. to mark a
# known fragmentation-onset round's approximate position on this tree).
# Labels off by default (unreadable at this leaf count unless
# top_n_splits is small). Turn on manually for a small subtree.
# ---------------------------------------------------------
# Optional 5th arg: a real round number to mark on the plot (e.g. an
# established fragmentation-onset/cut round). Drawn as a horizontal
# line at the closest merge-order height, with the round labeled.
# Pass 0 or omit to skip.
mark_round <- if (length(args) >= 5) as.integer(args[5]) else 0L

png(file.path(output_dir, "gn_dendrogram.png"), width = 4000, height = 2000, res = 200)
par(mar = c(5, 6, 4, 8))  # extra right margin so the round-mark label isn't clipped
plot(plot_hc, labels = FALSE, hang = -1,
     main = if (min_round > 0) {
       sprintf("Girvan-Newman dendrogram (zoomed to round >= %d, %d leaves)",
               min_round, plot_leaf_count)
     } else {
       sprintf("Girvan-Newman dendrogram (top %d splits, %d leaves, rounds %d-%d)",
               length(splits_to_use), length(is_leaf),
               min(round_at_height), max(round_at_height))
     },
     xlab = "", sub = "",
     ylab = "Merge order (evenly spaced; see round_at_height in the .rds for actual rounds)")

if (mark_round > 0 && min_round == 0) {
  if (mark_round >= min(round_at_height) && mark_round <= max(round_at_height)) {
    mark_height <- heights[which.min(abs(round_at_height - mark_round))]
    abline(h = mark_height, col = "red", lty = 2, lwd = 2)
    # Label placed well inside the plot area, left-aligned, offset from
    # the right edge. Anchoring flush at the exact boundary (usr[2])
    # clipped in practice even with xpd=TRUE and extra margin, so this
    # places it at 90% of the way across instead.
    usr <- par("usr")
    label_x <- usr[1] + 0.90 * (usr[2] - usr[1])
    text(x = label_x, y = mark_height, labels = sprintf("round %d", mark_round),
         col = "red", cex = 1.1, adj = c(0, -0.4), xpd = TRUE, font = 2)
    cat("Marked round", mark_round, "at merge-order height", mark_height, "\n")
  } else {
    cat("Requested mark_round", mark_round, "is outside the plotted range (",
        min(round_at_height), "-", max(round_at_height),
        "). Not marked. Increase top_n_splits to include it.\n")
  }
} else if (mark_round > 0 && min_round > 0) {
  cat("mark_round ignored: not meaningful when min_round zooming is active",
      "(the zoomed subtree uses different height coordinates).\n")
}
dev.off()
cat("Wrote", file.path(output_dir, "gn_dendrogram.png"), "\n")

hc$round_at_height <- round_at_height  # keep real rounds alongside the tree, not just merge order
saveRDS(hc, file.path(output_dir, "gn_dendrogram.rds"))
cat("Wrote", file.path(output_dir, "gn_dendrogram.rds"), ". Reload with readRDS() to replot,",
    "relabel, or pass to cutree() at any height/k. hc$round_at_height[k] gives the real round\n",
    "for merge row k, for annotating known rounds (e.g. a fragmentation-onset round) on the tree.\n")

cat("Done.\n")