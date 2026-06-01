#!/usr/bin/env bash
# Install a launchd LaunchAgent that auto-reinstalls the Desktop shim
# whenever Claude Desktop downloads a new embedded CLI version.
#
# Mechanism: WatchPaths on ~/Library/Application Support/Claude/claude-code/
# fires whenever that directory's contents change (new version subfolder
# appears). The agent runs install-desktop-shim.sh, which is idempotent —
# it only touches versions that aren't already shimmed.
#
# Usage:
#   bash install-launchagent.sh             # install + load
#   bash install-launchagent.sh --status    # show load state
#   bash install-launchagent.sh --uninstall # unload + remove
set -euo pipefail

LABEL="com.monitor-claude-code.shim-watch"
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_SCRIPT="$SKILL_DIR/scripts/install-desktop-shim.sh"
WATCH_DIR="$HOME/Library/Application Support/Claude/claude-code"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="/tmp/monitor-claude-code-shim-watch.log"

mode="${1:-install}"

case "$mode" in
  --status)
    if launchctl list "$LABEL" >/dev/null 2>&1; then
      echo "loaded:    yes"
      launchctl list "$LABEL" 2>/dev/null | grep -E '"(PID|LastExitStatus)"' | sed 's/^/  /' || true
    else
      echo "loaded:    no"
    fi
    if [[ -f "$PLIST" ]]; then
      echo "plist:     $PLIST  (exists)"
    else
      echo "plist:     $PLIST  (missing)"
    fi
    echo "log:       $LOG"
    [[ -f "$LOG" ]] && echo "  (last 5 lines)" && tail -5 "$LOG" | sed 's/^/    /'
    exit 0
    ;;
  --uninstall)
    if launchctl list "$LABEL" >/dev/null 2>&1; then
      launchctl unload "$PLIST" 2>/dev/null || true
      echo "unloaded:  $LABEL"
    fi
    if [[ -f "$PLIST" ]]; then
      rm -f "$PLIST"
      echo "removed:   $PLIST"
    fi
    exit 0
    ;;
  install)
    : # fall through
    ;;
  *)
    echo "Usage: $0 [install|--status|--uninstall]" >&2
    exit 2
    ;;
esac

if [[ ! -f "$INSTALL_SCRIPT" ]]; then
  echo "Cannot find install-desktop-shim.sh at $INSTALL_SCRIPT" >&2
  exit 1
fi

mkdir -p "$(dirname "$PLIST")"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$INSTALL_SCRIPT</string>
    <string>install</string>
  </array>
  <key>WatchPaths</key>
  <array>
    <string>$WATCH_DIR</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG</string>
  <key>StandardErrorPath</key>
  <string>$LOG</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin</string>
  </dict>
</dict>
</plist>
EOF

# Reload (unload first if already loaded) so plist edits take effect.
if launchctl list "$LABEL" >/dev/null 2>&1; then
  launchctl unload "$PLIST" 2>/dev/null || true
fi
launchctl load "$PLIST"

echo "Installed LaunchAgent: $LABEL"
echo "  plist:    $PLIST"
echo "  watches:  $WATCH_DIR"
echo "  runs:     $INSTALL_SCRIPT install"
echo "  log:      $LOG"
echo
echo "It just fired once (RunAtLoad). Check: bash $0 --status"
