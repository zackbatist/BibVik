#!/usr/bin/env bash
# Launch (or resume) the GN run inside a named tmux session.
# Usage: ./start_gn.sh path/to/graph.graphml path/to/checkpoint_dir
set -euo pipefail

GRAPH_FILE="$1"
CHECKPOINT_DIR="$2"
SESSION="girvan_newman"

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Session '$SESSION' already running. Attach with: tmux attach -t $SESSION"
  exit 0
fi

tmux new-session -d -s "$SESSION" \
  "Rscript run_gn_analysis.R '$GRAPH_FILE' '$CHECKPOINT_DIR' 2>&1 | tee -a '$CHECKPOINT_DIR/session_stdout.log'"

echo "Started in tmux session '$SESSION'."
echo "Attach:  tmux attach -t $SESSION"
echo "Detach:  Ctrl+b then d"
echo "Stop cleanly: touch '$CHECKPOINT_DIR/STOP'"
echo "Tail log: tail -f '$CHECKPOINT_DIR/run.log'"
