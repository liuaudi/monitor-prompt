#!/usr/bin/env bash
# Install a wrapper around Claude Desktop's embedded `claude` CLI so that
# when the monitor-claude-code proxy is running, every Desktop agent-mode
# session is captured. When the proxy is down, the wrapper is a pass-through
# and Desktop behaves identically to an uninstalled state.
#
# Idempotent. Run again after a Desktop update to wrap newly-downloaded
# CLI versions.
#
# Usage:
#   bash install-desktop-shim.sh           # install/refresh
#   bash install-desktop-shim.sh --status  # report installed versions
set -euo pipefail

PORT="${MONITOR_PROMPT_PORT:-9999}"
CC_ROOT="$HOME/Library/Application Support/Claude/claude-code"

if [[ ! -d "$CC_ROOT" ]]; then
  echo "monitor-claude-code: no Claude Desktop embedded CLI found at:" >&2
  echo "  $CC_ROOT" >&2
  echo "Launch Claude Desktop once so it downloads its embedded CLI, then re-run." >&2
  exit 1
fi

mode="${1:-install}"

# Retry rename to tolerate the launchd WatchPaths race: when Claude Desktop
# auto-updates, the watcher fires while the new binary is still held by the
# installer and mv returns EPERM. Total backoff ~31s, final attempt propagates.
mv_retry() {
  local src="$1" dst="$2"
  for delay in 1 2 4 8 16; do
    if mv "$src" "$dst" 2>/dev/null; then
      return 0
    fi
    sleep "$delay"
  done
  mv "$src" "$dst"
}

shim_body() {
  cat <<EOF
#!/bin/sh
# monitor-claude-code shim — auto-installed. Redirects this process to
# http://127.0.0.1:$PORT IFF a listener is up. Otherwise pass-through.
# Restore: rm "\$0" && mv "\$(dirname "\$0")/claude.real" "\$0"
PORT="\${MONITOR_PROMPT_PORT:-$PORT}"
DIR="\$(dirname "\$0")"
REAL="\$DIR/claude.real"
if /usr/bin/nc -z 127.0.0.1 "\$PORT" 2>/dev/null; then
  exec env ANTHROPIC_BASE_URL="http://127.0.0.1:\$PORT" "\$REAL" "\$@"
else
  exec "\$REAL" "\$@"
fi
EOF
}

shim_marker="# monitor-claude-code shim — auto-installed."

found=0
installed=0
already=0
restored=0

for verdir in "$CC_ROOT"/*/; do
  [[ -d "$verdir" ]] || continue
  version="$(basename "$verdir")"
  bin="$verdir/claude.app/Contents/MacOS/claude"
  real="$verdir/claude.app/Contents/MacOS/claude.real"

  if [[ ! -e "$bin" && ! -e "$real" ]]; then
    continue
  fi
  found=$((found + 1))

  case "$mode" in
    --status)
      if [[ -f "$real" ]] && head -3 "$bin" 2>/dev/null | grep -qF "$shim_marker"; then
        echo "  $version  shim INSTALLED"
      else
        echo "  $version  shim not installed"
      fi
      ;;
    --uninstall)
      if [[ -f "$real" ]] && head -3 "$bin" 2>/dev/null | grep -qF "$shim_marker"; then
        rm -f "$bin"
        mv "$real" "$bin"
        echo "  $version  shim removed"
        restored=$((restored + 1))
      else
        echo "  $version  no shim to remove"
      fi
      ;;
    install)
      if [[ -f "$real" ]] && head -3 "$bin" 2>/dev/null | grep -qF "$shim_marker"; then
        echo "  $version  already shimmed (skip)"
        already=$((already + 1))
        continue
      fi
      if [[ -f "$real" && ! -f "$bin" ]]; then
        echo "  $version  WARNING: claude.real exists but claude missing — partial state, fixing" >&2
      elif [[ -f "$real" && -f "$bin" ]]; then
        # claude.real exists from a prior partial install but claude is the
        # real binary again (e.g. user manually restored). Keep claude.real
        # as the canonical real, replace claude with shim.
        :
      else
        # Normal first-time install: move real binary aside.
        mv_retry "$bin" "$real"
      fi
      shim_body > "$bin"
      chmod +x "$bin"
      echo "  $version  shim installed"
      installed=$((installed + 1))
      ;;
    *)
      echo "Unknown mode: $mode" >&2
      echo "Usage: $0 [install|--status|--uninstall]" >&2
      exit 2
      ;;
  esac
done

if [[ $found -eq 0 ]]; then
  echo "monitor-claude-code: no Claude Desktop CLI versions found under $CC_ROOT" >&2
  exit 1
fi

case "$mode" in
  install)
    echo
    echo "Installed: $installed   Already shimmed: $already   Versions found: $found"
    if [[ $installed -gt 0 ]]; then
      echo
      echo "==> Now fully quit Claude Desktop (⌘Q) and relaunch."
      echo "    The next agent-mode session will spawn the shimmed CLI."
      echo "    Make sure monitor-claude-code is running:  bash scripts/start.sh"
    fi
    ;;
  --uninstall)
    echo
    echo "Removed: $restored shim(s) of $found version(s)."
    if [[ $restored -gt 0 ]]; then
      echo "Fully quit Claude Desktop (⌘Q) and relaunch to use the original binaries."
    fi
    ;;
esac
