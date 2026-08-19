#!/usr/bin/env bash
# Launch (or resume) the GN run inside a named tmux session.
# Usage: ./start_gn.sh path/to/edgelist.csv path/to/checkpoint_dir
set -euo pipefail

GRAPH_FILE="$1"
CHECKPOINT_DIR="$2"
SESSION="girvan_newman"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
  exit 0
fi

mkdir -p "$CHECKPOINT_DIR"

# Run inside bash, redirect straight to a file (no pipe/tee) so the pane
# has a shell underneath it and survives even if Rscript dies unexpectedly.
tmux new-session -d -s "$SESSION" bash -c \
  "Rscript run_gn_analysis.R '$GRAPH_FILE' '$CHECKPOINT_DIR' > '$CHECKPOINT_DIR/session_stdout.log' 2>&1; echo EXITED WITH CODE \$?; exec bash"

echo "Started in tmux session '$SESSION'."
echo "Attach:  tmux attach -t $SESSION"
echo "Detach:  Ctrl+b then d"
echo "Stop cleanly: touch '$CHECKPOINT_DIR/STOP'"
echo "Tail log: tail -f '$CHECKPOINT_DIR/run.log'"