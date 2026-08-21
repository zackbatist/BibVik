#!/usr/bin/env Rscript
# ============================================================
# Build one dendrogram per community, seeded from a real
# communities_round_R.csv file (produced by
# generate_multi_cut_communities.R), showing only what happens to that
# community in rounds AFTER R.
#
# Why per-community, not a "zoomed" cut of the whole-graph tree: taking
# a whole-graph tree and cutting it at a height can legitimately
# produce a FOREST (multiple disconnected pieces), not one clean
# subtree, whenever a later merge joins two branches that both
# finished their own internal structure earlier. R's plot.hclust()
# can't render that, and does not fail gracefully (confirmed: cut()
# errors internally on this data's tree shape). Building one tree per
# real starting community sidesteps this entirely: each community is
# already its own natural root, so no artificial joining is needed and
# no forest can occur.
#
# This does NOT re-derive an arbitrary cut. The starting groups are
# read directly from communities_round_R.csv, the actual, already-
# computed component membership at that real round, produced earlier
# by generate_multi_cut_communities.R from the same removal_log.csv.
#
# Usage:
#   Rscript build_subcluster_trees.R <edgelist_csv> <removal_log_csv> <communities_round_csv> <seed_round> <output_dir> [min_community_size] [max_trees] [max_split_levels] [plot_trees]
#
# seed_round: the round communities_round_csv was generated at. Must
#   match, used to filter removal_log.csv to only rounds after it.
# min_community_size (default 3): skip communities smaller than this.
#   nothing to show for singletons/pairs.
# max_trees (default 12): only build trees for the N largest qualifying
#   communities, to keep output manageable. Pass 0 for all.
# max_split_levels (default 0, no limit): replays each community to
#   natural completion (full fragmentation or end of the log), the
#   same endpoint used for the whole-graph analysis. A positive value
#   stops early after that many split events instead, which is faster
#   but introduces an arbitrary depth cutoff, so 0 is the default.
#   Pass a positive number only if you want a faster, shallower look
#   and accept that its depth is a choice, not a derived value.
# plot_trees (default FALSE): whether to write a per-community
#   dendrogram PNG. Off by default: on this corpus, every community's
#   tree at any depth turned out to be the same single-branch staircase
#   (one node peeling off almost every round), differing only in
#   length, not shape. subcluster_summary.csv (always written) and its
#   first_split_sizes column carry the actual signal. Pass TRUE to
#   generate the plots anyway, e.g. to inspect a specific community.
# ============================================================

suppressPackageStartupMessages(library(igraph))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 5) {
  stop("Usage: Rscript build_subcluster_trees.R <edgelist_csv> <removal_log_csv> <communities_round_csv> <seed_round> <output_dir> [min_community_size] [max_trees] [max_split_levels] [plot_trees]")
}
edgelist_file        <- args[1]
removal_log_file     <- args[2]
communities_csv_file <- args[3]
seed_round           <- as.integer(args[4])
output_dir           <- args[5]
min_community_size   <- if (length(args) >= 6) as.integer(args[6]) else 3L
max_trees            <- if (length(args) >= 7) as.integer(args[7]) else 12L
max_split_levels     <- if (length(args) >= 8) as.integer(args[8]) else 0L
plot_trees           <- if (length(args) >= 9) as.logical(args[9]) else FALSE

dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)

cat("Loading real community membership from", communities_csv_file, "\n")
communities <- read.csv(communities_csv_file, stringsAsFactors = FALSE)
if (!all(c("node_id", "community_id") %in% names(communities))) {
  stop("Expected columns 'node_id' and 'community_id' in ", communities_csv_file)
}

groups <- split(communities$node_id, communities$community_id)
group_sizes <- sapply(groups, length)
cat("Loaded", length(groups), "communities at round", seed_round,
    "( sizes range", min(group_sizes), "-", max(group_sizes), ")\n")

qualifying <- names(group_sizes)[group_sizes >= min_community_size]
qualifying <- qualifying[order(-group_sizes[qualifying])]
if (max_trees > 0 && length(qualifying) > max_trees) {
  cat("Restricting to the", max_trees, "largest of", length(qualifying),
      "qualifying communities (size >=", min_community_size, ").\n")
  qualifying <- qualifying[seq_len(max_trees)]
}
cat("Building trees for", length(qualifying), "communities:",
    paste(qualifying, collapse = ", "), "\n")

cat("Loading removal log from", removal_log_file, "\n")
removal_log <- read.csv(removal_log_file, stringsAsFactors = FALSE)
removal_log <- removal_log[order(removal_log$round), ]
removal_log_after <- removal_log[removal_log$round > seed_round, ]
cat("Removal log after round", seed_round, ":", nrow(removal_log_after), "rounds\n")

# ---------------------------------------------------------
# Find split events (round -> pieces) restricted to edges among a given
# group's members only. max_split_levels=0 means no cap: replay runs
# to natural completion (until the community is fully fragmented or
# the log is exhausted), the same endpoint the whole-graph analysis
# used, rather than an arbitrary chosen depth. A positive value stops
# early after that many split events instead, which is faster but
# introduces an arbitrary cutoff, so 0 is the default (see main
# argument parsing below).
# ---------------------------------------------------------
find_splits_within_group <- function(member_names, g_full, removal_log_after, max_split_levels) {
  sub_edges <- removal_log_after[
    removal_log_after$from %in% member_names & removal_log_after$to %in% member_names,
  ]
  if (nrow(sub_edges) == 0) return(list())

  g_sub <- induced_subgraph(g_full, vids = member_names)
  comp_id <- rep(1L, length(member_names))
  names(comp_id) <- member_names
  next_comp_id <- 2L
  splits <- list()

  for (i in seq_len(nrow(sub_edges))) {
    if (max_split_levels > 0 && length(splits) >= max_split_levels) break

    r <- sub_edges$round[i]
    from <- sub_edges$from[i]; to <- sub_edges$to[i]
    eid <- get_edge_ids(g_sub, c(from, to))
    if (eid == 0) next
    g_sub <- delete_edges(g_sub, eid)

    parent_comp <- comp_id[[from]]
    members_here <- names(comp_id)[comp_id == parent_comp]
    sub_g <- induced_subgraph(g_sub, vids = members_here)
    sub_comp <- components(sub_g)

    if (sub_comp$no > 1) {
      pieces <- split(V(sub_g)$name, sub_comp$membership)
      for (k in seq_along(pieces)) {
        piece_names <- pieces[[k]]
        new_id <- if (k == 1) parent_comp else { id <- next_comp_id; next_comp_id <<- next_comp_id + 1L; id }
        comp_id[piece_names] <- new_id
      }
      splits[[length(splits) + 1]] <- list(round = r, pieces = pieces)
    }
  }
  splits
}

# ---------------------------------------------------------
# Build an hclust tree from a list of splits, using the same verified
# lineage-tracking logic as build_dendrogram.R (piece identity resolved
# by checking ALL members for an existing id, not just the first).
# Returns NULL if there are no splits (community never divides further).
# ---------------------------------------------------------
build_tree_from_splits <- function(splits, all_members) {
  if (length(splits) == 0) return(NULL)
  splits_rev <- rev(splits)

  next_id <- 1L
  piece_size <- c()
  name_to_id <- new.env()
  is_leaf <- integer(0)
  hclust_ref <- new.env()
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
    for (nm in piece_members) {
      v <- mget(nm, envir = name_to_id, ifnotfound = NA, inherits = FALSE)[[1]]
      if (!is.na(v)) return(v)
    }
    NA_integer_
  }

  merge_a <- integer(0); merge_b <- integer(0); heights <- numeric(0)

  for (s in splits_rev) {
    ids_here <- integer(0)
    for (p in s$pieces) {
      id <- get_id_any_member(p)
      if (is.na(id)) {
        id <- register_leaf(p)
      } else {
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
      merge_a <- c(merge_a, ref_a); merge_b <- c(merge_b, ref_b)
      heights <- c(heights, length(merge_a))
      row_index <- length(merge_a)
      assign(as.character(parent_id), row_index, envir = hclust_ref)
      for (nm in ls(name_to_id)) {
        v <- get(nm, envir = name_to_id)
        if (v == a || v == b) assign(nm, parent_id, envir = name_to_id)
      }
      ids_here <- c(parent_id, ids_here[-c(1, 2)])
    }
  }

  # Members never touched by any split stay standalone leaves.
  untouched <- setdiff(all_members, ls(name_to_id))
  for (nm in untouched) register_leaf(nm)

  if (length(merge_a) == 0) return(NULL)

  leaf_size_by_pos <- vapply(is_leaf, function(id) piece_size[as.character(id)], numeric(1))
  leaf_pos_order <- vapply(is_leaf, function(id) -get(as.character(id), envir = hclust_ref), integer(1))
  leaf_labels <- character(length(is_leaf))
  leaf_labels[leaf_pos_order] <- sprintf("n=%d", leaf_size_by_pos)

  # hc$order must be a permutation that matches the tree's actual
  # branch structure (which leaf ends up where when drawn left to
  # right), not just 1:n. Using 1:n directly only works by accident on
  # trivial trees. Build first with a placeholder order, then derive
  # the correct one from the merge structure via as.dendrogram(). The
  # result must be a plain unnamed integer vector: order.dendrogram()
  # inherits names from cbind(merge_a, merge_b)'s column names
  # ("merge_a", "merge_b"), which as.dendrogram() accepts but
  # plot.hclust() rejects with "invalid dendrogram input". Confirmed by
  # inspecting hc$order directly: it printed as a Named int vector.
  hc <- list(merge = cbind(merge_a, merge_b), height = heights,
             order = seq_along(leaf_labels), labels = leaf_labels,
             method = "girvan-newman-divisive")
  class(hc) <- "hclust"
  hc$order <- unname(order.dendrogram(as.dendrogram(hc)))
  hc
}

cat("Loading edge list from", edgelist_file, "\n")
edges_df <- read.csv(edgelist_file, stringsAsFactors = FALSE)
g <- graph_from_data_frame(edges_df[, c("source", "target")], directed = TRUE)
g <- as.undirected(g, mode = "collapse")

summary_rows <- list()

for (gid in qualifying) {
  member_names <- as.character(groups[[gid]])
  cat("\n--- Community", gid, "(size", length(member_names), ") ---\n")

  splits <- find_splits_within_group(member_names, g, removal_log_after, max_split_levels)
  hc <- build_tree_from_splits(splits, member_names)

  if (is.null(hc)) {
    cat("  No further splits after round", seed_round, ". Stays one piece.\n")
    summary_rows[[length(summary_rows) + 1]] <- data.frame(
      community_id = gid, size = length(member_names), n_splits = 0, n_leaves = 1
    )
    next
  }

  n_leaves <- length(hc$labels)
  split_rounds <- vapply(splits, function(s) s$round, integer(1))
  piece_sizes_at_first_split <- sapply(splits[[1]]$pieces, length)
  cat("  ", length(splits), "split(s) at round(s)", paste(split_rounds, collapse = ", "),
      ", first split into sizes:", paste(piece_sizes_at_first_split, collapse = "/"), "\n")

  if (n_leaves < 3) {
    cat("   Only", n_leaves, "leaves, skipping plot (nothing to show beyond the split above).\n")
  } else if (!plot_trees) {
    # Per-community dendrogram plots are off by default: every one of
    # them, at any depth, turned out to be the same single-branch
    # staircase (one node peeling off almost every round, all the way
    # to full fragmentation), differing only in length, not shape. The
    # first-split sizes recorded below are the actual signal; see
    # 02_network_structure.qmd's "Internal structure of individual
    # communities" section. Pass plot_trees=TRUE (see usage) to
    # generate them anyway, e.g. to inspect one community directly.
    cat("   Plot skipped (plot_trees=FALSE; see summary below for the real signal).\n")
  } else {
    plot_path <- file.path(output_dir, sprintf("community_%s_tree.png", gid))
    plot_ok <- tryCatch({
      png(plot_path, width = 2400, height = 1400, res = 200)
      # labels must be a character vector or FALSE, not a boolean TRUE.
      # Passing labels=TRUE was the actual cause of "invalid dendrogram
      # input" here, confirmed directly: a bare plot(hc) with no labels
      # argument worked, and the only difference in the failing call
      # was labels=(n_leaves <= 40) evaluating to TRUE.
      show_labels <- if (n_leaves <= 40) hc$labels else FALSE
      plot(hc, labels = show_labels, hang = -1,
           main = sprintf("Community %s (size %d at round %d), first split at round %d",
                           gid, length(member_names), seed_round, split_rounds[1]),
           xlab = "", sub = "", ylab = "Merge order")
      dev.off()
      TRUE
    }, error = function(e) {
      if (dev.cur() != 1) dev.off()
      cat("   Plot failed for community", gid, ":", conditionMessage(e), "\n")
      FALSE
    })
    if (plot_ok) {
      cat("  Wrote", plot_path, "\n")
    } else {
      cat("   Data is still recorded in subcluster_summary.csv below.\n")
    }
  }

  summary_rows[[length(summary_rows) + 1]] <- data.frame(
    community_id = gid, size = length(member_names), n_splits = length(splits),
    n_leaves = n_leaves, first_split_round = split_rounds[1],
    first_split_sizes = paste(piece_sizes_at_first_split, collapse = "/")
  )
}

summary_df <- do.call(rbind, summary_rows)
write.csv(summary_df, file.path(output_dir, "subcluster_summary.csv"), row.names = FALSE)
cat("\nWrote", file.path(output_dir, "subcluster_summary.csv"), "\n")
print(summary_df)
cat("\nDone.\n")