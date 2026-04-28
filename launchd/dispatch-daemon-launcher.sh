#!/bin/bash
# launchd entrypoint for the dispatch daemon.
#
# Bypasses the bash+uv wrapper in bin/claude-assistant so macOS Full Disk
# Access is attributed to the venv's Python interpreter (the binary that
# actually opens ~/Library/Messages/chat.db). The wrapper is fine for
# Terminal launches (which inherit FDA) but uv-spawned children don't
# inherit FDA reliably under launchd.
#
# Writes the pidfile via $$ so `claude-assistant status` keeps working
# (the same PID persists across exec).

set -euo pipefail

DISPATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$DISPATCH_DIR/state/daemon.pid"

mkdir -p "$DISPATCH_DIR/state" "$DISPATCH_DIR/logs"
echo "$$" > "$PID_FILE"

cd "$DISPATCH_DIR"
exec "$DISPATCH_DIR/.venv/bin/python" -m assistant.manager
