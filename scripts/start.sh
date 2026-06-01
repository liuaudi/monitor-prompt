#!/usr/bin/env bash
# Start monitor-claude-code proxy + viewer in the background, open the viewer.
# Idempotent: detects an existing instance and just opens the browser.
set -euo pipefail

PORT="${MONITOR_PROMPT_PORT:-9999}"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROXY="$SKILL_DIR/scripts/proxy.py"
LOG="/tmp/monitor-claude-code-$PORT.log"
PIDFILE="/tmp/monitor-claude-code-$PORT.pid"

is_running() {
  if [[ -f "$PIDFILE" ]]; then
    local pid
    pid=$(cat "$PIDFILE")
    if kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

if is_running; then
  echo "monitor-claude-code: already running on http://127.0.0.1:$PORT"
else
  nohup python3 "$PROXY" >"$LOG" 2>&1 &
  echo $! >"$PIDFILE"
  echo "monitor-claude-code: started (pid $(cat "$PIDFILE"), log: $LOG)"
  sleep 0.3
fi

if [[ "${ANTHROPIC_BASE_URL:-}" == "http://127.0.0.1:$PORT" ]]; then
  cat <<EOF

  ✅ The session running this script IS routed through the proxy.
     Every API request will appear in the viewer.
EOF
else
  cat <<EOF

  ⚠️  This proxy does NOT capture the session running this script.
     HTTPS clients fix their endpoint at startup, so an already-running
     claude process cannot be redirected after the fact.
     • To capture: start a NEW session through monitor-claude (CLI),
       or ⌘Q + relaunch Claude Desktop to pick up the shim (agent-mode
       subprocesses only — the Desktop chat panel is never captured).
EOF
fi

cat <<EOF

  Viewer:    http://127.0.0.1:$PORT/
  Taps dir:  $SKILL_DIR/taps/

To capture a Claude Code CLI session, open a new terminal and run:

  monitor-claude            # wrapper that sets ANTHROPIC_BASE_URL for you
  # or: ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT claude

To capture Claude Desktop agent-mode sessions, install the shim once:

  bash $SKILL_DIR/scripts/install-desktop-shim.sh
  # then fully quit Claude Desktop (⌘Q) and relaunch.

Desktop shim status:
EOF
bash "$SKILL_DIR/scripts/install-desktop-shim.sh" --status 2>/dev/null || \
  echo "  (Claude Desktop not installed yet — skip)"

cat <<EOF

To stop the proxy:

  pkill -f monitor-claude-code/scripts/proxy.py

EOF

# Open viewer in default browser (best-effort, silent fail).
if command -v open >/dev/null 2>&1; then
  open "http://127.0.0.1:$PORT/" || true
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "http://127.0.0.1:$PORT/" || true
fi
