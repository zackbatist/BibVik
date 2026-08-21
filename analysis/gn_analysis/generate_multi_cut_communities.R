#!/usr/bin/env Rscript
# ============================================================
# Generate community assignments at multiple GN cut points, rather
# than committing to one flat partition. The graph fragments in
# several sustained bursts, not one clean onset — so instead of
# picking one round as "the" cut, this DETECTS every sustained
# fast-fragmentation onset directly from modularity_trace.csv (same
# rule used earlier: a round after which n_components rises by ~1
# every round, sustained for 100+ consecutive rounds — not a
# hardcoded list) and produces a partition at each one.
#
# For each detected round r, replays removal_log.csv up through round
# r against the original graph and writes out the resulting connected
# components as node_id, community_id — same format as the existing
# communities.csv from run_gn_analysis.R, so it drops into the same
# join pattern in 02_network_structure.qmd.
#
# Usage:
#   Rscript generate_multi_cut_communities.R path/to/edgelist.csv path/to/removal_log.csv path/to/modularity_trace.csv path/to/output_dir [window] [min_rate]
#
# window (default 100): how many consecutive rounds must sustain the
#   rate for it to count as a real onset, not a blip.
# min_rate (default 0.9): minimum average components-gained-per-round
#   over that window to qualify as "fast" fragmentation.
# ============================================================

suppressPackageStartupMessages(library(igraph))

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 4) {
  stop("Usage: Rscript generate_multi_cut_communities.R <edgelist_csv> <removal_log_csv> <modularity_trace_csv> <output_dir> [window] [min_rate]")
}
edgelist_file       <- args[1]
removal_log_file    <- args[2]
modularity_trace_file <- args[3]
output_dir          <- args[4]
window   <- if (length(args) >= 5) as.integer(args[5]) else 100L
min_rate <- if (length(args) >= 6) as.numeric(args[6]) else 0.9

dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)

# ---------------------------------------------------------
# Detect sustained fast-fragmentation onset rounds directly from
# modularity_trace.csv — same logic used interactively earlier, now
# as a real function instead of a one-off terminal command.
# ---------------------------------------------------------
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

cat("Loading modularity trace from", modularity_trace_file, "\n")
trace <- read.csv(modularity_trace_file, stringsAsFactors = FALSE)
target_rounds <- detect_onsets(trace, window, min_rate)

if (length(target_rounds) == 0) {
  stop("No sustained fast-fragmentation onsets detected with window=", window,
       ", min_rate=", min_rate, " — try a smaller window or min_rate.")
}
cat("Detected", length(target_rounds), "sustained fast-fragmentation onset(s):",
    paste(target_rounds, collapse = ", "), "\n")

cat("Loading edge list from", edgelist_file, "\n")
edges_df <- read.csv(edgelist_file, stringsAsFactors = FALSE)
g <- graph_from_data_frame(edges_df[, c("source", "target")], directed = TRUE)
g <- as.undirected(g, mode = "collapse")
cat("Graph loaded:", vcount(g), "nodes,", ecount(g), "edges\n")

cat("Loading removal log from", removal_log_file, "\n")
removal_log <- read.csv(removal_log_file, stringsAsFactors = FALSE)
removal_log <- removal_log[order(removal_log$round), ]

target_rounds <- sort(target_rounds)
cat("Target rounds:", paste(target_rounds, collapse = ", "), "\n")

max_target <- max(target_rounds)
g_live <- g
next_target_idx <- 1L

summary_rows <- list()

for (i in seq_len(nrow(removal_log))) {
  if (removal_log$round[i] > max_target) break

  eid <- get.edge.ids(g_live, c(removal_log$from[i], removal_log$to[i]))
  if (eid > 0) g_live <- delete_edges(g_live, eid)

  if (next_target_idx <= length(target_rounds) &&
      removal_log$round[i] == target_rounds[next_target_idx]) {

    r <- target_rounds[next_target_idx]
    comp <- components(g_live)
    out_df <- data.frame(node_id = V(g_live)$name, community_id = comp$membership)
    out_path <- file.path(output_dir, sprintf("communities_round_%d.csv", r))
    write.csv(out_df, out_path, row.names = FALSE)

    n_singletons <- sum(comp$csize[comp$membership] == 1)
    cat(sprintf("Round %d: %d communities, %d singletons, largest=%d -- wrote %s\n",
                r, comp$no, n_singletons, max(comp$csize), out_path))

    summary_rows[[length(summary_rows) + 1]] <- data.frame(
      round = r, n_communities = comp$no, n_singletons = n_singletons,
      largest_community = max(comp$csize)
    )

    next_target_idx <- next_target_idx + 1L
  }
}

if (next_target_idx <= length(target_rounds)) {
  warning("Not all target rounds were reached in the removal log — check target rounds against actual round range.")
}

summary_df <- do.call(rbind, summary_rows)
write.csv(summary_df, file.path(output_dir, "multi_cut_summary.csv"), row.names = FALSE)
cat("\nWrote summary:", file.path(output_dir, "multi_cut_summary.csv"), "\n")
print(summary_df)

cat("\nDone. Each communities_round_R.csv has the same node_id,community_id\n")
cat("format as the existing communities.csv — join into node_table under a\n")
cat("column like community_gn_R for each round you want to keep.\n")