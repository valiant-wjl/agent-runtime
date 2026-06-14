#!/bin/bash
# scripts/install-cron.sh — M9-T06
#
# Installs agent-runtime's scheduled cron jobs into the user's crontab,
# idempotently and non-destructively. Existing user cron entries are preserved.
#
# Usage:
#   scripts/install-cron.sh              # install/refresh the managed block
#   scripts/install-cron.sh --dry-run    # print merged crontab to stdout, do not apply
#   scripts/install-cron.sh --uninstall  # remove only the managed block
#
# Behavior:
#   - Reads existing crontab via `crontab -l` (no entries -> empty baseline).
#   - Strips any pre-existing managed block (between MARK_BEGIN/MARK_END).
#   - Appends a fresh managed block with the cron entries.
#   - Pipes merged result back via `crontab -`.
#   - Never invokes `crontab -r` or overwrites unconditionally.
#
# Stub-friendly: respects $CRONTAB_CMD env var so tests can inject a fake
# crontab binary on PATH; otherwise uses the system `crontab`.
#
# Bash 3.2 compatible. NO `set -e` — we want fail-soft on cosmetic failures
# (e.g. mkdir log/ when permission denied) but `set -uo pipefail` is fine.

set -uo pipefail

#======================================================================
# Proxy clearing — per CLAUDE.md, all CLI invocations bypass local proxy.
#======================================================================
export http_proxy='' https_proxy='' HTTP_PROXY='' HTTPS_PROXY=''
export NO_PROXY='*' no_proxy='*'

#======================================================================
# Resolve REPO_ROOT — works regardless of where the user cloned.
#======================================================================
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Match bootstrap.sh's REPO_ROOT idiom so a caller (e.g. test harness) can
# redirect cron entries into a tmp sandbox via the env var.
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

CRONTAB_CMD="${CRONTAB_CMD:-crontab}"

MARK_BEGIN="# >>> agent-runtime cron BEGIN (do not edit between markers; managed by scripts/install-cron.sh)"
MARK_END="# <<< agent-runtime cron END"

#======================================================================
# Build the managed block.
#======================================================================
build_block() {
  # PATH is set inside the block because cron runs jobs with a minimal default
  # PATH (/usr/bin:/bin) that cannot find lark-cli / jq — the ingest auth
  # precheck would fail ("auth failed") otherwise. $HOME is expanded here at
  # install time (cron does not expand it). Union covers both hosts: lark-cli is
  # under ~/.npm-global/bin on Linux and /opt/homebrew/bin on macOS.
  cat <<EOF
$MARK_BEGIN
PATH=$HOME/bin:$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
0 */1 * * * cd $REPO_ROOT && bash scripts/ingest_feishu.sh >> $REPO_ROOT/log/cron-ingest_feishu.log 2>&1
0 2 * * * cd $REPO_ROOT && bash scripts/backup-wiki.sh >> $REPO_ROOT/log/cron-backup.log 2>&1
$MARK_END
EOF
}

#======================================================================
# Read existing crontab — empty if none installed.
# `crontab -l` exits non-zero when there's no crontab; we treat that as empty.
#======================================================================
read_current() {
  "$CRONTAB_CMD" -l 2>/dev/null || true
}

#======================================================================
# Strip existing managed block (idempotent boundary).
# Uses awk for portability; deletes lines from MARK_BEGIN to MARK_END inclusive.
#======================================================================
strip_block() {
  awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
    $0 == b { skip = 1; next }
    skip && $0 == e { skip = 0; next }
    !skip { print }
  '
}

#======================================================================
# Parse args.
#======================================================================
MODE="install"
for arg in "$@"; do
  case "$arg" in
    --dry-run)   MODE="dry-run" ;;
    --uninstall) MODE="uninstall" ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "[install-cron] unknown arg: $arg" >&2
      exit 2
      ;;
  esac
done

#======================================================================
# Ensure log dir exists (best-effort; cron lines redirect into it).
#======================================================================
mkdir -p "$REPO_ROOT/log" 2>/dev/null || true

#======================================================================
# Build the merged crontab.
#======================================================================
CURRENT="$(read_current)"
STRIPPED="$(printf '%s\n' "$CURRENT" | strip_block)"
# `printf '%s\n' ""` adds a stray newline when CURRENT is empty; trim leading
# blank lines to keep output tidy.
STRIPPED="$(printf '%s' "$STRIPPED" | awk 'NF || seen { seen=1; print }')"

case "$MODE" in
  uninstall)
    MERGED="$STRIPPED"
    ;;
  install|dry-run)
    BLOCK="$(build_block)"
    if [[ -n "$STRIPPED" ]]; then
      MERGED="$STRIPPED"$'\n'"$BLOCK"
    else
      MERGED="$BLOCK"
    fi
    ;;
esac

#======================================================================
# Apply or print.
#======================================================================
if [[ "$MODE" == "dry-run" ]]; then
  printf '%s\n' "$MERGED"
  exit 0
fi

# Pipe merged result back via `crontab -`. Trailing newline ensures the last
# entry is properly terminated (crontab requires it).
printf '%s\n' "$MERGED" | "$CRONTAB_CMD" -
RC=$?
if [[ $RC -ne 0 ]]; then
  echo "[install-cron] crontab apply failed (rc=$RC)" >&2
  exit $RC
fi

case "$MODE" in
  install)   echo "[install-cron] installed managed block (2 entries) under $REPO_ROOT" ;;
  uninstall) echo "[install-cron] removed managed block; user entries preserved" ;;
esac
exit 0
