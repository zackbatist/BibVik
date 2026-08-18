#!/usr/bin/env Rscript
# ============================================================
# Girvan-Newman community detection with checkpointing,
# logging, and interrupt handling.
#
# Usage:
#   Rscript run_gn_analysis.R path/to/edgelist.csv path/to/checkpoint_dir
#
# Reads a plain two-column edge list CSV (header: source,target) rather
# than GraphML, since some igraph builds have GraphML support disabled
# (missing libxml2 at compile time) and that can't always be fixed
# without root/sudo access on a shared server.
#
# Resumes automatically if checkpoint_dir already has state.
# Safe to Ctrl+C or `tmux kill-session` — next run picks up
# from the last completed round.
# ============================================================

suppressPackageStartupMessages({
  library(igraph)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  stop("Usage: Rscript run_gn_analysis.R <edgelist_csv> <checkpoint_dir>")
}
graph_file      <- args[1]
checkpoint_dir  <- args[2]

dir.create(checkpoint_dir, showWarnings = FALSE, recursive = TRUE)

state_file    <- file.path(checkpoint_dir, "state.rds")
log_file      <- file.path(checkpoint_dir, "run.log")
modularity_csv<- file.path(checkpoint_dir, "modularity_trace.csv")
removal_log_csv <- file.path(checkpoint_dir, "removal_log.csv")
result_csv    <- file.path(checkpoint_dir, "communities.csv")
result_edgelist_csv <- file.path(checkpoint_dir, "final_edgelist.csv")

# ---------------------------------------------------------
# Logging: timestamped, appended to file AND printed to stdout
# (stdout is what you'll see live in tmux; the file survives
# after the pane is gone)
# ---------------------------------------------------------
log_msg <- function(...) {
  msg <- sprintf(...)
  line <- sprintf("[%s] %s", format(Sys.time(), "%Y-%m-%d %H:%M:%S"), msg)
  cat(line, "\n")
  cat(line, "\n", file = log_file, append = TRUE)
}

# ---------------------------------------------------------
# Interrupt handling
# ---------------------------------------------------------
# A VPN drop or closed SSH session does NOT interrupt this script at all —
# it's running inside tmux on the remote server, detached from your terminal,
# so disconnects are irrelevant to it.
#
# For a *deliberate* stop (you want to pause it), R's signal handling
# inside long-running C calls (like edge_betweenness) is unreliable, so
# Ctrl+C may not register until the current round finishes anyway. Instead,
# use the marker-file approach below: touch STOP, and the script checks
# for it once per round (between betweenness calls), then checkpoints and
# exits cleanly.
stop_file <- file.path(checkpoint_dir, "STOP")  # touch this file to request a clean stop
log_msg("To stop cleanly at the next checkpoint, run: touch %s", stop_file)

check_stop_requested <- function() {
  if (file.exists(stop_file)) {
    log_msg("STOP file detected — will checkpoint after current round and exit.")
    file.remove(stop_file)
    return(TRUE)
  }
  FALSE
}

# ---------------------------------------------------------
# Load or resume state
# ---------------------------------------------------------
if (file.exists(state_file)) {
  log_msg("Resuming from checkpoint: %s", state_file)
  state <- readRDS(state_file)
  g <- state$graph
  round_num <- state$round_num
  modularity_trace <- state$modularity_trace
  removal_log <- state$removal_log
  best_modularity <- state$best_modularity
  best_membership <- state$best_membership
  best_round <- state$best_round
  log_msg("Resumed at round %d, %d edges remaining, best modularity so far: %.4f (round %d)",
          round_num, ecount(g), best_modularity, best_round)
} else {
  log_msg("Starting fresh run. Loading edge list from %s", graph_file)
  edges_df <- read.csv(graph_file, stringsAsFactors = FALSE)
  if (!all(c("source", "target") %in% names(edges_df))) {
    stop("Expected columns 'source' and 'target' in ", graph_file,
         " — found: ", paste(names(edges_df), collapse = ", "))
  }
  g <- graph_from_data_frame(edges_df[, c("source", "target")], directed = TRUE)
  if (is_directed(g)) {
    log_msg("Source edge list is directed — collapsing to undirected (mode='collapse') before GN.")
    g <- as.undirected(g, mode = "collapse")
  }
  log_msg("Graph loaded: %d nodes, %d edges", vcount(g), ecount(g))

  round_num <- 0
  modularity_trace <- data.frame(round = integer(0), edges_removed = integer(0),
                                  n_components = integer(0), modularity = double(0))
  # Full removal order — endpoint names of the edge removed each round, in
  # order. This is sufficient to reconstruct the complete dendrogram (the
  # partition at any cut point, not just the best-modularity one) after the
  # fact: replaying removals 1..k against the original graph reproduces the
  # exact component structure at k removals, for any k.
  removal_log <- data.frame(round = integer(0), from = character(0), to = character(0))
  best_modularity <- -Inf
  best_membership <- NULL
  best_round <- 0

  # header for the live CSV trace
  write.csv(modularity_trace, modularity_csv, row.names = FALSE)
  write.csv(removal_log, removal_log_csv, row.names = FALSE)
}

total_edges_start <- ecount(g) + round_num  # approx, only exact if resuming with no prior loss
start_time <- Sys.time()

# ---------------------------------------------------------
# Main loop
# ---------------------------------------------------------
log_msg("Beginning Girvan-Newman edge removal loop.")

while (ecount(g) > 0) {

  round_start <- Sys.time()

  eb <- edge_betweenness(g, directed = FALSE)
  max_idx <- which.max(eb)
  removed_endpoints <- ends(g, max_idx, names = TRUE)
  g <- delete_edges(g, max_idx)

  round_num <- round_num + 1
  comp <- components(g)
  mod <- modularity(g, comp$membership)

  round_elapsed <- as.numeric(difftime(Sys.time(), round_start, units = "secs"))

  modularity_trace <- rbind(modularity_trace,
                             data.frame(round = round_num,
                                        edges_removed = round_num,
                                        n_components = comp$no,
                                        modularity = mod))

  removal_log <- rbind(removal_log,
                        data.frame(round = round_num,
                                   from = removed_endpoints[1, 1],
                                   to = removed_endpoints[1, 2]))

  # append single row to each live CSV so progress is visible without
  # waiting for R to exit
  write.table(tail(modularity_trace, 1), modularity_csv, sep = ",",
              row.names = FALSE, col.names = FALSE, append = TRUE)
  write.table(tail(removal_log, 1), removal_log_csv, sep = ",",
              row.names = FALSE, col.names = FALSE, append = TRUE)

  if (mod > best_modularity) {
    best_modularity <- mod
    best_membership <- comp$membership
    best_round <- round_num
    log_msg("Round %d: NEW BEST modularity=%.4f, %d components, %.1fs this round, %d edges left",
            round_num, mod, comp$no, round_elapsed, ecount(g))
  } else if (round_num %% 25 == 0) {
    log_msg("Round %d: modularity=%.4f (best=%.4f @round %d), %d components, %.1fs this round, %d edges left",
            round_num, mod, best_modularity, best_round, comp$no, round_elapsed, ecount(g))
  }

  # ---- checkpoint every round (cheap relative to betweenness cost) ----
  state <- list(graph = g, round_num = round_num,
                modularity_trace = modularity_trace,
                removal_log = removal_log,
                best_modularity = best_modularity,
                best_membership = best_membership,
                best_round = best_round)
  saveRDS(state, state_file)

  if (check_stop_requested()) {
    log_msg("Stopping after round %d by request. State checkpointed — rerun the same command to resume.", round_num)
    quit(save = "no", status = 0)
  }
}

elapsed_total <- difftime(Sys.time(), start_time, units = "hours")
log_msg("Girvan-Newman complete after %d rounds (%.2f hours this session). Best modularity=%.4f at round %d.",
        round_num, as.numeric(elapsed_total), best_modularity, best_round)

# ---------------------------------------------------------
# Write final portable outputs
# ---------------------------------------------------------
edges_df_orig <- read.csv(graph_file, stringsAsFactors = FALSE)
g_orig <- graph_from_data_frame(edges_df_orig[, c("source", "target")], directed = TRUE)
if (is_directed(g_orig)) g_orig <- as.undirected(g_orig, mode = "collapse")
node_ids <- V(g_orig)$name
if (is.null(node_ids)) node_ids <- seq_len(vcount(g_orig))
out_df <- data.frame(node_id = node_ids, community_id = best_membership)
write.csv(out_df, result_csv, row.names = FALSE)
log_msg("Wrote community assignment: %s", result_csv)

write.csv(edges_df_orig[, c("source", "target")], result_edgelist_csv, row.names = FALSE)
log_msg("Wrote original edge list (for reference, unchanged from input): %s", result_edgelist_csv)
log_msg("Community assignment for each node is in %s — join on node_id to annotate the edge list yourself if needed.", result_csv)

log_msg("Full removal order (%d edges) written incrementally to: %s", round_num, removal_log_csv)
log_msg("That file, replayed against the original graph, reconstructs the full dendrogram — the partition at any cut point, not just the best-modularity one saved above.")

log_msg("Done.")