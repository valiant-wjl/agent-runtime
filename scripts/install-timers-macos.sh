#!/bin/bash
# Install agent-runtime's scheduled ingest/backup jobs as macOS launchd timers.
#
# Why launchd and not cron: macOS cron is deprecated and silently requires Full
# Disk Access for /usr/sbin/cron, so cron jobs often never run. launchd user
# agents run reliably in the user context (same mechanism as the main service).
#
# This installs ONLY the jobs that currently work on this host:
#   - com.agent-runtime.ingest-feishu  (hourly)
#   - com.agent-runtime.backup         (daily 02:00)
# Only the generic ingest (feishu) and backup jobs are scheduled here.
# Platform-specific data-source ingestion belongs in a deployment-private overlay.
#
# Usage:
#   scripts/install-timers-macos.sh              # install/refresh + load
#   scripts/install-timers-macos.sh --uninstall  # unload + remove the plists
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"
AGENTS_DIR="${AGENTS_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-launchctl}"   # tests can inject a stub

LABELS=(com.agent-runtime.ingest-feishu com.agent-runtime.backup)

uninstall() {
    for label in "${LABELS[@]}"; do
        dst="$AGENTS_DIR/$label.plist"
        "$LAUNCHCTL_BIN" unload "$dst" 2>/dev/null || true
        rm -f "$dst"
        echo "removed: $label"
    done
}

if [[ "${1:-}" == "--uninstall" ]]; then
    uninstall
    exit 0
fi

mkdir -p "$AGENTS_DIR" "$REPO_ROOT/log"

for label in "${LABELS[@]}"; do
    src="$REPO_ROOT/scripts/$label.plist"
    dst="$AGENTS_DIR/$label.plist"
    if [[ ! -f "$src" ]]; then
        echo "ERROR: template not found: $src" >&2
        exit 1
    fi
    # Render {{REPO_ROOT}} / {{HOME}} placeholders.
    sed -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" -e "s|{{HOME}}|$HOME|g" "$src" > "$dst"
    # Reload (idempotent: unload first).
    "$LAUNCHCTL_BIN" unload "$dst" 2>/dev/null || true
    "$LAUNCHCTL_BIN" load "$dst"
    echo "installed + loaded: $label"
done

echo "done. verify: launchctl list | grep com.agent-runtime"
