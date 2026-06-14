#!/bin/bash
# Compile-wiki precheck.
#
# Verifies that meta/wiki/ has no uncommitted manual edits before letting
# /compile-wiki overwrite generated entries. If the wiki/ subtree is dirty,
# exit 1 and instruct the user to either commit those edits or pass
# --override-git-check (forwarded via $ARGUMENTS by compile-wiki.md.template).
#
# Also performs a sanity check that .state/ is writable so the compile step
# can later record `last_compile_at`.
#
# Usage:
#   scripts/compile-wiki-precheck.sh [META_DIR] [--override-git-check]
#
# Exit codes:
#   0 = OK to proceed
#   1 = wiki/ has uncommitted manual edits (blocked)
#   2 = META_DIR invalid / cannot cd
#   3 = cannot mkdir .state
#   4 = .state not writable
#   5 = META_DIR is not a git repository

set -uo pipefail

META_DIR="${1:-$(pwd)}"
shift || true

OVERRIDE_GIT=0
for arg in "$@"; do
  case "$arg" in
    --override-git-check) OVERRIDE_GIT=1 ;;
  esac
done

cd "$META_DIR" 2>/dev/null || { echo "BLOCKED: cannot cd to $META_DIR" >&2; exit 2; }

if [[ $OVERRIDE_GIT -eq 0 ]]; then
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    echo "BLOCKED: $META_DIR is not a git repository (no version control = no recovery if compile corrupts wiki). Pass --override-git-check if you really want to proceed." >&2
    exit 5
  fi
  if [[ -n "$(git status --porcelain wiki/ 2>/dev/null)" ]]; then
    echo "BLOCKED: meta/wiki/ has uncommitted manual edits. Please 'git commit -am \"manual edits\"' or pass --override-git-check" >&2
    exit 1
  fi
fi

# Sanity: .state/ must be writable for last_compile_at.
mkdir -p .state 2>/dev/null || { echo "BLOCKED: cannot mkdir .state in $META_DIR" >&2; exit 3; }
[[ -w .state ]] || { echo "BLOCKED: .state not writable in $META_DIR" >&2; exit 4; }

exit 0
