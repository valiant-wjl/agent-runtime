#!/bin/bash
# Claude Code PreToolUse hook for Write|Edit.
#
# Contract: Claude Code passes the tool invocation as a JSON payload on STDIN.
# Exit 0 = allow the operation. Exit 1 = block it (stderr is shown to user).
#
# Purpose: protect user-authored wiki entries marked with
#   `manual_edits_locked: true` in YAML frontmatter from being overwritten by
#   the LLM during /compile-wiki or any other Write/Edit call.
#
# Notes:
#   - Depends on `python3` only (guaranteed on dev/Mac/CI). On any parse
#     error or missing python, we FAIL CLOSED (block) rather than silently
#     allowing every write.
#   - The lock is detected ONLY inside the YAML frontmatter region (between
#     the first two `---` lines), so a wiki page that documents the lock
#     mechanism in its body does not accidentally lock itself.
#   - If the target file does not exist on disk yet (a CREATE), the awk scan
#     silently returns non-zero and we allow the write — only existing
#     locked files are protected.
#   - Intentionally no `set -e`: a non-zero from awk on an unrelated file
#     must NOT abort the script with an implicit error exit.
#
# Best-effort guard, NOT atomic: a prior Edit in the same agent turn that strips
# the lock line from frontmatter will let a subsequent Write through unblocked.
# Threat model: lazy LLM overwrite, not a malicious actor.

command -v python3 >/dev/null || { echo "BLOCKED: python3 missing" >&2; exit 1; }

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | python3 -c '''import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(2)
print((d.get("tool_input") or {}).get("file_path") or "")
''' 2>/dev/null) || {
  echo "BLOCKED: invalid hook payload (python parse failed)" >&2; exit 1; }

# No file_path in payload -> nothing to guard.
[[ -z "$FILE" ]] && exit 0

# Only guard paths under a meta/wiki/ directory.
[[ "$FILE" != */meta/wiki/* ]] && exit 0

# Only guard files that already exist AND have the lock inside YAML frontmatter.
# `... || exit 0` covers both file-missing and "not locked" cases, preserving
# CREATE-safe behavior.
awk '
  BEGIN { in_fm = 0; seen = 0 }
  /^---[[:space:]]*$/ { seen++; if (seen == 1) { in_fm = 1; next } else { exit } }
  in_fm && /^manual_edits_locked:[[:space:]]*true[[:space:]]*$/ { found = 1; exit }
  END { exit !found }
' "$FILE" 2>/dev/null || exit 0

echo "BLOCKED: $FILE has manual_edits_locked=true (user-authored content)" >&2
exit 1
