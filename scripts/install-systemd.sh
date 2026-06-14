#!/bin/bash
# scripts/install-systemd.sh — Linux systemd user unit installer (launchd equivalent)
#
# Renders scripts/agent-runtime.service.template (substituting {{REPO_ROOT}} + {{HOME}}),
# installs to ~/.config/systemd/user/, runs daemon-reload + enable.
# Does NOT auto-start (user runs `systemctl --user start agent-runtime` after bootstrap).
#
# Usage:
#   bash scripts/install-systemd.sh              # render + install + enable
#   bash scripts/install-systemd.sh --dry-run    # print rendered unit, no disk write
#   bash scripts/install-systemd.sh --uninstall  # disable + remove unit
#
# Exit codes:
#   0 = success / dry-run / clean uninstall
#   1 = systemctl failure
#   2 = template missing or invalid arg
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$REPO_ROOT/scripts/agent-runtime.service"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/agent-runtime.service"

DRY_RUN=0
UNINSTALL=0
SYSTEMCTL_BIN="${SYSTEMCTL_BIN:-systemctl}"   # tests can inject a stub

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)    DRY_RUN=1 ;;
        --uninstall)  UNINSTALL=1 ;;
        --help|-h)
            grep -E '^#( |$)' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
            exit 0 ;;
        *)
            echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

log() { printf '[install-systemd] %s\n' "$*"; }

if [ "$UNINSTALL" = "1" ]; then
    log "uninstalling agent-runtime.service"
    "$SYSTEMCTL_BIN" --user stop agent-runtime 2>/dev/null || true
    "$SYSTEMCTL_BIN" --user disable agent-runtime 2>/dev/null || true
    rm -f "$UNIT_PATH"
    "$SYSTEMCTL_BIN" --user daemon-reload 2>/dev/null || true
    log "uninstall done"
    exit 0
fi

if [ ! -f "$TEMPLATE" ]; then
    echo "template missing: $TEMPLATE" >&2
    exit 2
fi

# Render the template (substitute {{REPO_ROOT}} and {{HOME}})
RENDERED=$(sed -e "s|{{REPO_ROOT}}|$REPO_ROOT|g" -e "s|{{HOME}}|$HOME|g" "$TEMPLATE")

if [ "$DRY_RUN" = "1" ]; then
    log "DRY RUN — would write: $UNIT_PATH"
    echo "--- rendered unit ---"
    echo "$RENDERED"
    echo "--- end ---"
    exit 0
fi

mkdir -p "$UNIT_DIR" "$REPO_ROOT/log" || {
    echo "mkdir failed" >&2; exit 1
}
echo "$RENDERED" > "$UNIT_PATH"
log "wrote: $UNIT_PATH"

"$SYSTEMCTL_BIN" --user daemon-reload || {
    echo "systemctl daemon-reload failed" >&2; exit 1
}
"$SYSTEMCTL_BIN" --user enable agent-runtime || {
    echo "systemctl enable failed" >&2; exit 1
}

log "installed + enabled. Start with: systemctl --user start agent-runtime"
log "To enable lingering (run on boot without login): loginctl enable-linger \$USER"
