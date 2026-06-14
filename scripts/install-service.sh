#!/bin/bash
# Install agent-runtime as macOS launchd service (user-level).
#
# Copies scripts/com.agent-runtime.plist to ~/Library/LaunchAgents/ and loads it.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
PLIST_SRC="$REPO_ROOT/scripts/com.agent-runtime.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.agent-runtime.plist"

# Render: replace {{REPO_ROOT}} and {{HOME}} placeholders
mkdir -p "$HOME/Library/LaunchAgents"
# Ensure launchd StandardOutPath / StandardErrorPath parent dirs exist
mkdir -p "$REPO_ROOT/log" "$REPO_ROOT/.state"
sed -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" -e "s|{{HOME}}|$HOME|g" "$PLIST_SRC" > "$PLIST_DST"
echo "installed: $PLIST_DST"

# Ensure `claude` is reachable from the launchd PATH WITHOUT hardcoding an
# nvm version segment. claude-code installs under the active node version's
# global bin (e.g. ~/.nvm/versions/node/vX.Y.Z/bin/claude), which is NOT on
# the plist PATH and changes whenever node is upgraded. Resolve it to its real
# binary and expose it via a stable, version-agnostic symlink in ~/.local/bin
# (already on the plist PATH). Idempotent: refreshes the link to the current
# binary on every install.
LINK="$HOME/.local/bin/claude"
CLAUDE_BIN="$(command -v claude || true)"
if [ -n "$CLAUDE_BIN" ]; then
    # Canonicalize to the REAL binary. Critical: on a re-install ~/.local/bin is
    # already on PATH, so `command -v claude` resolves to $LINK itself — linking
    # that to itself would create a self-referential symlink (ELOOP) and break
    # claude entirely. realpath/readlink -f follow through to the upstream binary.
    CLAUDE_REAL="$(realpath "$CLAUDE_BIN" 2>/dev/null || readlink -f "$CLAUDE_BIN" 2>/dev/null || echo "$CLAUDE_BIN")"
    # Only manage the link if absent or already a symlink (never clobber a real
    # binary a user may have placed there), and never point it at itself.
    if [ "$CLAUDE_REAL" != "$LINK" ] && { [ ! -e "$LINK" ] || [ -L "$LINK" ]; }; then
        mkdir -p "$HOME/.local/bin"
        ln -sfn "$CLAUDE_REAL" "$LINK"
        echo "linked: $LINK -> $CLAUDE_REAL"
    fi
else
    echo "WARNING: 'claude' not found on PATH at install time; the service will" >&2
    echo "         fail to spawn it. Install claude-code and re-run this script," >&2
    echo "         or create ~/.local/bin/claude manually." >&2
fi

# Load (idempotent: unload first)
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"
echo "loaded: com.agent-runtime"
sleep 2
if launchctl list | grep -q com.agent-runtime; then
    echo "service up: com.agent-runtime"
else
    echo "service not in launchctl list after 2s; check $REPO_ROOT/log/launchd.err.log"
    exit 1
fi
