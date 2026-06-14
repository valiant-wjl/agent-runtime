#!/bin/bash
# Uninstall agent-runtime macOS launchd service.
set -euo pipefail

PLIST_DST="$HOME/Library/LaunchAgents/com.agent-runtime.plist"

if [ -f "$PLIST_DST" ]; then
    launchctl unload "$PLIST_DST" 2>/dev/null || true
    rm -f "$PLIST_DST"
    echo "uninstalled: $PLIST_DST"
else
    echo "not installed: $PLIST_DST"
fi
