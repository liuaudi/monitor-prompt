#!/usr/bin/env bash
# Install a wrapper around the system Claude Code CLI so that when the
# monitor-claude-code proxy is running, every CLI session is captured. When
# the proxy is down, the wrapper is a pass-through and the CLI behaves
# identically to an uninstalled state.
#
# This is the analog of install-desktop-shim.sh, for the system CLI
# (the one `which claude` finds — typically ~/.local/bin/claude →
# ~/.local/share/claude/versions/<ver>).
#
# IMPORTANT BACKUP LOCATION — DO NOT STORE .real INSIDE versions/.
#
# The Claude Code CLI runs a self-cleanup pass on `~/.local/share/claude/
# versions/` whenever any `claude` invocation starts. That cleanup
# deletes anything in `versions/` that doesn't match a recognised
# version-string pattern. An earlier version of this script stored the
# backup at `versions/<ver>.real`, which Claude promptly deleted on its
# next invocation — destroying the user's real CLI binaries.
#
# Backups now live at $BACKUP_DIR below, outside any path Claude
# manages. The shim hardcodes the absolute backup path so it doesn't
# depend on PATH or working directory at exec time.
#
# Idempotent. Re-run after a CLI auto-update to wrap new versions.
#
# Usage:
#   bash install-cli-shim.sh           # install/refresh
#   bash install-cli-shim.sh --status  # report installed versions
#   bash install-cli-shim.sh --uninstall
set -euo pipefail

PORT="${MONITOR_PROMPT_PORT:-9999}"
VERSIONS_DIR="$HOME/.local/share/claude/versions"
BACKUP_DIR="$HOME/.local/share/monitor-claude-code-shim/cli-backups"

# Minimum file size (bytes) we'll accept as a real CLI binary. The
# bun-compiled Claude Code binary is ~200MB; we use 1MB as a generous
# floor that still rules out shims (~500B), launcher scripts (~few KB),
# and accidentally-truncated files. Without this check, re-running the
# script on an already-shimmed-but-corrupted state could promote a 500B
# shim to .real, masking the broken state.
MIN_REAL_BINARY_BYTES=1048576

if [[ ! -d "$VERSIONS_DIR" ]]; then
  echo "monitor-claude-code: no Claude Code CLI versions dir at:" >&2
  echo "  $VERSIONS_DIR" >&2
  echo "Install Claude Code CLI (https://claude.ai/install.sh) first, then re-run." >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

mode="${1:-install}"

# The shim hardcodes the absolute path to its own backup binary.
# Self-resolution via $0 is unreliable when invoked via symlink under
# BSD readlink, so we just bake the path in at install time. Each
# version gets a self-contained shim.
shim_body() {
  local real_path="$1"
  cat <<EOF
#!/bin/sh
# monitor-claude-code shim — auto-installed.
# Real binary: $real_path
# Restore: rm "\$0" && mv "$real_path" "\$0"
PORT="\${MONITOR_PROMPT_PORT:-$PORT}"
REAL="$real_path"
if [ ! -x "\$REAL" ]; then
  echo "monitor-claude-code shim: backup binary missing at \$REAL" >&2
  echo "  Reinstall Claude Code (curl -fsSL https://claude.ai/install.sh | bash)" >&2
  echo "  or remove this shim: rm \"\$0\"" >&2
  exit 127
fi
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
skipped_small=0

backup_path_for() {
  echo "$BACKUP_DIR/$1.real"
}

for bin in "$VERSIONS_DIR"/*; do
  [[ -f "$bin" ]] || continue
  name="$(basename "$bin")"
  found=$((found + 1))
  real="$(backup_path_for "$name")"

  case "$mode" in
    --status)
      if head -3 "$bin" 2>/dev/null | grep -qF "$shim_marker"; then
        if [[ -x "$real" ]]; then
          echo "  $name  shim INSTALLED (backup: $real)"
        else
          echo "  $name  shim INSTALLED but backup MISSING at $real — claude will fail. Reinstall Claude Code."
        fi
      else
        echo "  $name  shim not installed"
      fi
      ;;
    --uninstall)
      if head -3 "$bin" 2>/dev/null | grep -qF "$shim_marker"; then
        if [[ -x "$real" ]]; then
          rm -f "$bin"
          mv "$real" "$bin"
          echo "  $name  shim removed, real binary restored"
          restored=$((restored + 1))
        else
          echo "  $name  shim present but backup MISSING at $real — refusing to delete shim (would leave no claude). Reinstall Claude Code." >&2
        fi
      else
        echo "  $name  no shim to remove"
      fi
      ;;
    install)
      if head -3 "$bin" 2>/dev/null | grep -qF "$shim_marker"; then
        echo "  $name  already shimmed (skip)"
        already=$((already + 1))
        continue
      fi
      # Guard against wrapping something that isn't a real binary.
      # Stat in BSD/macOS: stat -f %z gives size in bytes.
      size=$(/usr/bin/stat -f %z "$bin" 2>/dev/null || echo 0)
      if [[ "$size" -lt "$MIN_REAL_BINARY_BYTES" ]]; then
        echo "  $name  WARNING: file is only ${size}B (<${MIN_REAL_BINARY_BYTES}B threshold), refusing to wrap." >&2
        echo "    This is probably already a shim or a corrupted install. Reinstall Claude Code if needed." >&2
        skipped_small=$((skipped_small + 1))
        continue
      fi
      if [[ -e "$real" ]]; then
        # Backup slot already occupied. Probably a prior failed install.
        # Don't clobber — leave it as evidence and skip.
        echo "  $name  WARNING: backup already exists at $real and current binary is unshimmed — refusing to overwrite." >&2
        echo "    Inspect both files; if the backup is the real binary, delete the live file and rerun. If unsure, reinstall Claude Code." >&2
        continue
      fi
      mv "$bin" "$real"
      shim_body "$real" > "$bin"
      chmod +x "$bin"
      # Sanity check: confirm the move succeeded and backup is non-empty
      # before declaring victory. Catches edge cases like a filesystem
      # that silently failed mid-mv.
      backup_size=$(/usr/bin/stat -f %z "$real" 2>/dev/null || echo 0)
      if [[ "$backup_size" -lt "$MIN_REAL_BINARY_BYTES" ]]; then
        echo "  $name  ERROR: backup ended up only ${backup_size}B — rolling back." >&2
        rm -f "$bin"
        mv "$real" "$bin"
        exit 3
      fi
      echo "  $name  shim installed (backup: $real)"
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
  echo "monitor-claude-code: no CLI version binaries found under $VERSIONS_DIR" >&2
  exit 1
fi

case "$mode" in
  install)
    echo
    echo "Installed: $installed   Already shimmed: $already   Skipped (too small): $skipped_small   Versions found: $found"
    if [[ $installed -gt 0 ]]; then
      echo
      echo "==> Backups live OUTSIDE versions/ to survive Claude's self-cleanup:"
      echo "      $BACKUP_DIR/"
      echo "==> New 'claude' invocations will route through the proxy when it's up."
      echo "    Currently-running CLI sessions keep using their old (open) binary"
      echo "    inode — restart them to pick up the shim."
      echo "    Make sure monitor-claude-code is running:  bash scripts/start.sh"
    fi
    ;;
  --uninstall)
    echo
    echo "Removed: $restored shim(s) of $found version(s)."
    ;;
esac
