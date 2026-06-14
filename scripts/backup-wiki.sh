#!/bin/bash
# scripts/backup-wiki.sh — M9-T03
#
# Daily/cron-driven backup of the meta repo:
#   1. cd into META_DIR (must be a directory + git repo, else exit 2).
#   2. Auto-commit any dirty state with "auto: daily backup YYYY-MM-DD".
#   3. Push to `origin main` if `meta/config.yaml` has backup.meta_remote set.
#      Push failure (network/auth/non-fast-forward) is logged to
#      .state/backup.log but does NOT abort — exit stays 0 so cron does not
#      bombard the user with failure mail. Watchdog (M9-T05) detects stale
#      .state/backup.log mtime > 3d.
#   4. On the 1st of each month, tag HEAD as backup-YYYY-MM (idempotent) and
#      push tags. Tag-push failure is also logged but non-fatal.
#
# Exit codes:
#   0 = ran to completion (push may have failed but is recoverable)
#   2 = META_DIR missing / not a git repo (BLOCKED, manual fix required)
#
# Bash 3.2 compatible. NO `set -e` — fail-soft for push errors is required.

set -uo pipefail

#======================================================================
# Proxy clearing — per CLAUDE.md, all CLI invocations bypass local proxy.
#======================================================================
export http_proxy='' https_proxy='' HTTP_PROXY='' HTTPS_PROXY=''
export NO_PROXY='*' no_proxy='*'

#======================================================================
# Resolve META_DIR
#======================================================================
META_DIR="${META_DIR:-${1:-$HOME/work/agent-repos/meta}}"

# Resolve a Python that actually has PyYAML. Resolved BEFORE the cd into
# META_DIR below so a relative BASH_SOURCE still points at the repo. cron/launchd
# run with a minimal PATH, and which interpreter carries PyYAML differs by host:
# on the macOS box deps live in <repo>/.venv; on the Linux box they live in
# system python3 (its .venv is a thin symlink without site-packages). So probe
# candidates and pick the first whose `import yaml` succeeds — otherwise
# meta_remote reads empty and the backup push is silently skipped.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PYBIN=""
for _cand in "$_SCRIPT_DIR/../.venv/bin/python3" python3; do
  if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c 'import yaml' >/dev/null 2>&1; then
    PYBIN="$_cand"; break
  fi
done
PYBIN="${PYBIN:-python3}"

if [[ ! -d "$META_DIR" ]]; then
  echo "[backup-wiki] BLOCKED: META_DIR not a directory: $META_DIR" >&2
  exit 2
fi

cd "$META_DIR" || {
  echo "[backup-wiki] BLOCKED: cannot cd into META_DIR: $META_DIR" >&2
  exit 2
}

if [[ ! -d .git ]] && ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "[backup-wiki] BLOCKED: META_DIR is not a git repo: $META_DIR" >&2
  exit 2
fi

STATE_DIR="$META_DIR/.state"
LOG_FILE="$STATE_DIR/backup.log"
CONFIG_FILE="$META_DIR/config.yaml"

mkdir -p "$STATE_DIR" 2>/dev/null

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log_line() {
  printf '%s %s\n' "$(now_iso)" "$*" >> "$LOG_FILE"
}

#======================================================================
# Read backup.meta_remote (may be missing — that's OK; we'll skip push).
#======================================================================
META_REMOTE=""
if [[ -f "$CONFIG_FILE" ]]; then
  META_REMOTE=$("$PYBIN" - "$CONFIG_FILE" <<'PY' 2>/dev/null
import sys
try:
    import yaml
except Exception:
    sys.exit(0)
try:
    with open(sys.argv[1]) as fh:
        data = yaml.safe_load(fh) or {}
except Exception:
    sys.exit(0)
val = ((data.get("backup") or {}).get("meta_remote") or "")
if isinstance(val, str):
    print(val.strip())
PY
  )
fi

#======================================================================
# Step 1: Auto-commit dirty state.
#======================================================================
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  git add -A 2>/dev/null
  if ! git commit -m "auto: daily backup $(date +%Y-%m-%d)" >/dev/null 2>&1; then
    log_line "commit failed (possibly nothing to commit after add)"
  fi
fi

#======================================================================
# Step 2: Push to origin main (only if meta_remote configured & non-empty).
#======================================================================
# Strip whitespace defensively — `[[ -n "  " ]]` is true, but we want false.
META_REMOTE_TRIM=$(printf '%s' "$META_REMOTE" | tr -d '[:space:]')
if [[ -z "$META_REMOTE_TRIM" ]]; then
  log_line "skip push: no meta_remote configured"
else
  if ! git push origin main >>"$LOG_FILE" 2>&1; then
    log_line "push failed origin main"
  fi
fi

#======================================================================
# Step 3: Monthly tag (1st of month). Idempotent — skip if tag exists.
#======================================================================
DAY=$(date +%d)
if [[ "$DAY" == "01" ]]; then
  TAG="backup-$(date +%Y-%m)"
  if git rev-parse "refs/tags/$TAG" >/dev/null 2>&1; then
    log_line "skip tag: $TAG already exists"
  else
    if git tag "$TAG" HEAD 2>/dev/null; then
      if [[ -n "$META_REMOTE_TRIM" ]]; then
        if ! git push --tags >>"$LOG_FILE" 2>&1; then
          log_line "tag push failed for $TAG"
        fi
      fi
    else
      log_line "tag create failed for $TAG"
    fi
  fi
fi

# Always exit 0 unless META_DIR/git invalid (already exited 2 above).
exit 0
