#!/bin/bash
# scripts/ingest_feishu.sh — M9-T01
#
# Pulls Feishu wiki/docx documents listed in meta/config.yaml under
#   ingest.feishu_docs.source_spaces (list of space ids)
#   ingest.feishu_docs.include_patterns (list of globs, optional)
#   ingest.feishu_docs.exclude_patterns (list of globs, optional)
#
# Behavior:
#   1. Auth precheck via `lark-cli auth status`. Non-zero -> errors.log + exit 2.
#   2. Read meta/.state/ingest_feishu.last (default 1970-01-01T00:00:00Z).
#   3. For each source space, list docs via `lark-cli wiki nodes list` with a
#      `--jq` reshape that emits [{doc_id, title, obj_type, updated_at}, ...],
#      filtering to obj_type in {doc, docx} and converting the unix-second
#      `obj_edit_time` to ISO-8601 UTC. Then skip docs with updated_at <=
#      last_ingest_at, apply include/exclude patterns against title, and
#      export each remaining doc to meta/raw/feishu-docs/<doc_id>.md via
#      `lark-cli drive +export --doc-type ... --file-extension markdown`.
#   4. Append "<iso>,feishu-doc,<doc_id>,<title>" to meta/raw/INDEX.md per doc.
#   5. On any single-doc failure: append to errors.log and continue.
#   6. Bump last_ingest_at only if at least one doc succeeded — and bump it to
#      max(updated_at) of successful docs (NOT now()), so failed docs whose
#      updated_at < now still get retried on the next run.
#
# Exit codes:
#   0 = full success (all spaces listed, all matching docs exported)
#   1 = partial failure (at least one space list failed but some docs succeeded;
#       watchdog signal — investigate errors.log)
#   2 = auth failure
#   3 = config.yaml parse failure / META_DIR missing
#
# Bash 3.2 compatible (no associative arrays). NO `set -e` so partial-success
# path works — explicit failure checks throughout.

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

# Resolve a Python that actually has PyYAML. cron/launchd run with a minimal
# PATH, and which interpreter carries PyYAML differs by host: on the macOS box
# deps live in <repo>/.venv; on the Linux box they live in system python3 (its
# .venv is a thin symlink without site-packages). So probe candidates and pick
# the first whose `import yaml` succeeds rather than assuming the venv —
# otherwise config parsing fails ("No module named 'yaml'").
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PYBIN=""
for _cand in "$_SCRIPT_DIR/../.venv/bin/python3" python3; do
  if command -v "$_cand" >/dev/null 2>&1 && "$_cand" -c 'import yaml' >/dev/null 2>&1; then
    PYBIN="$_cand"; break
  fi
done
PYBIN="${PYBIN:-python3}"

if [[ ! -d "$META_DIR" ]]; then
  echo "[ingest_feishu] META_DIR not found: $META_DIR" >&2
  exit 3
fi

CONFIG_FILE="$META_DIR/config.yaml"
STATE_DIR="$META_DIR/.state"
RAW_DIR="$META_DIR/raw/feishu-docs"
INDEX_FILE="$META_DIR/raw/INDEX.md"
LAST_FILE="$STATE_DIR/ingest_feishu.last"
ERR_LOG="$STATE_DIR/ingest_feishu.errors.log"

mkdir -p "$STATE_DIR" "$RAW_DIR" "$(dirname "$INDEX_FILE")" 2>/dev/null

now_iso() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

log_err() {
  printf '%s %s\n' "$(now_iso)" "$*" >> "$ERR_LOG"
}

#======================================================================
# Auth precheck
#======================================================================
if ! lark-cli auth status >/dev/null 2>&1; then
  log_err "auth failed"
  echo "[ingest_feishu] auth failed — see $ERR_LOG" >&2
  exit 2
fi

#======================================================================
# Parse config.yaml — emit lines:
#   SPACE\t<space_id>
#   INCLUDE\t<pattern>
#   EXCLUDE\t<pattern>
#======================================================================
if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "[ingest_feishu] config not found: $CONFIG_FILE" >&2
  exit 3
fi

# Capture stderr separately so warnings don't poison the parsed output.
PARSE_ERR=$(mktemp)
PARSED=$("$PYBIN" - "$CONFIG_FILE" 2>"$PARSE_ERR" <<'PY'
import sys
try:
    import yaml
except Exception as e:
    sys.stderr.write("yaml import failed: %s\n" % e)
    sys.exit(3)

path = sys.argv[1]
try:
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
except Exception as e:
    sys.stderr.write("yaml parse failed: %s\n" % e)
    sys.exit(3)

cfg = ((data.get("ingest") or {}).get("feishu_docs") or {})
# Legacy: list of space_ids → root-of-space ingest
for s in (cfg.get("source_spaces") or []):
    print("SPACE\t%s" % s)
# US-004: list of {space_id, root_node_token} → recursive subtree ingest
for n in (cfg.get("source_nodes") or []):
    if not isinstance(n, dict):
        continue
    sp = n.get("space_id") or ""
    rt = n.get("root_node_token") or ""
    if sp and rt:
        print("NODE\t%s\t%s" % (sp, rt))
for p in (cfg.get("include_patterns") or []):
    print("INCLUDE\t%s" % p)
for p in (cfg.get("exclude_patterns") or []):
    print("EXCLUDE\t%s" % p)
PY
)
PARSE_RC=$?
if [[ $PARSE_RC -ne 0 ]]; then
  err_msg=$(cat "$PARSE_ERR" 2>/dev/null)
  rm -f "$PARSE_ERR"
  echo "[ingest_feishu] config parse failed: $err_msg" >&2
  exit 3
fi
rm -f "$PARSE_ERR"

# Use bash arrays so values are not subject to glob/word-splitting at iteration
# (e.g. a stored pattern like "Project*" must NOT match files in cwd).
SPACES=()
SOURCE_NODES=()  # entries: "<space_id>\t<root_node_token>"
INCLUDES=()
EXCLUDES=()
while IFS=$'\t' read -r kind val val2; do
  [[ -z "${kind:-}" ]] && continue
  case "$kind" in
    SPACE)   SPACES+=("$val") ;;
    NODE)    SOURCE_NODES+=("${val}"$'\t'"${val2}") ;;
    INCLUDE) INCLUDES+=("$val") ;;
    EXCLUDE) EXCLUDES+=("$val") ;;
  esac
done <<< "$PARSED"

#======================================================================
# Read last_ingest_at
#======================================================================
LAST_INGEST_AT="1970-01-01T00:00:00Z"
if [[ -f "$LAST_FILE" ]]; then
  v=$(cat "$LAST_FILE" 2>/dev/null)
  [[ -n "$v" ]] && LAST_INGEST_AT="$v"
fi

#======================================================================
# Pattern match helpers
#======================================================================
title_included() {
  local title="$1"
  local pat
  # Bash 3.2 + `set -u`: an empty array's "${ARR[@]}" expansion errors. Guard
  # via `${ARR[@]+"${ARR[@]}"}` which expands to nothing when ARR is empty.
  if [[ ${#INCLUDES[@]} -eq 0 ]]; then
    :  # no includes -> all match by default
  else
    local matched=0
    for pat in "${INCLUDES[@]}"; do
      if [[ "$title" == $pat ]]; then matched=1; break; fi
    done
    [[ $matched -eq 1 ]] || return 1
  fi
  if [[ ${#EXCLUDES[@]} -gt 0 ]]; then
    for pat in "${EXCLUDES[@]}"; do
      if [[ "$title" == $pat ]]; then return 1; fi
    done
  fi
  return 0
}

#======================================================================
# Main loop
#======================================================================
SUCCESS_COUNT=0
SPACE_FAIL_COUNT=0
# Track max successful updated_at so we can set last_ingest_at to that value
# rather than now(). Why: if a doc exported successfully had updated_at older
# than wall-clock now, bumping to now() would mask any failures whose
# updated_at < now from the next retry pass.
MAX_UPDATED="$LAST_INGEST_AT"

# jq expression run by lark-cli to reshape the wiki.nodes.list response
# into rows the downstream parser expects.
#
# US-004: keep node_token + has_child so we can recurse into subtrees,
# and keep all obj_types (filtering to doc/docx happens in bash so we
# can still recurse into a folder/sheet that *contains* doc children).
WIKI_LIST_JQ='[.data.items[] | {node_token, obj_token, obj_type, title, has_child, obj_edit_time}]'

# BFS over the wiki tree. Initial frontier = legacy SPACES (root listing)
# + new SOURCE_NODES (subtree listing). Each queue entry is the tab-joined
# pair "<space_id>\t<parent_node_token>" where empty parent means "list
# the space root".
QUEUE=()
for sp in ${SPACES[@]+"${SPACES[@]}"}; do
  QUEUE+=("${sp}"$'\t'"")
done
for entry in ${SOURCE_NODES[@]+"${SOURCE_NODES[@]}"}; do
  QUEUE+=("$entry")
done

# Hard cap on processed nodes — defensive guard against pathological
# subtrees / fixture cycles. 500 is well above the typical wiki space
# size (Aily 商业化 has ~50 nodes); raise via env if needed.
MAX_NODES="${INGEST_MAX_NODES:-500}"
PROCESSED=0
# Track visited node_tokens so a fixture/data cycle can't pump the BFS
# forever even within MAX_NODES.
VISITED_TOKENS=" "

while [ ${#QUEUE[@]} -gt 0 ] && [ "$PROCESSED" -lt "$MAX_NODES" ]; do
  ENTRY="${QUEUE[0]}"
  if [ ${#QUEUE[@]} -gt 1 ]; then
    QUEUE=("${QUEUE[@]:1}")
  else
    QUEUE=()
  fi
  PROCESSED=$((PROCESSED + 1))

  IFS=$'\t' read -r SPACE_ID PARENT <<< "$ENTRY"
  [[ -z "$SPACE_ID" ]] && continue

  # Skip already-visited subtree roots (cycle protection).
  if [[ -n "$PARENT" ]]; then
    case "$VISITED_TOKENS" in
      *" $PARENT "*) continue ;;
      *) VISITED_TOKENS="$VISITED_TOKENS$PARENT " ;;
    esac
  fi

  if [[ -z "$PARENT" ]]; then
    PARAMS="{\"space_id\":\"$SPACE_ID\"}"
    LIST_DESC="space=$SPACE_ID"
  else
    PARAMS="{\"space_id\":\"$SPACE_ID\",\"parent_node_token\":\"$PARENT\"}"
    LIST_DESC="space=$SPACE_ID parent=$PARENT"
  fi

  LIST_JSON=$(lark-cli wiki nodes list \
    --params "$PARAMS" \
    --page-all \
    --jq "$WIKI_LIST_JQ" \
    2>/dev/null)
  LIST_RC=$?
  if [[ $LIST_RC -ne 0 ]]; then
    log_err "wiki list failed for $LIST_DESC"
    SPACE_FAIL_COUNT=$((SPACE_FAIL_COUNT + 1))
    continue
  fi

  # Parse list JSON -> tab-separated rows:
  #   node_token \t obj_token \t obj_type \t title \t updated_at \t has_child
  # Backward-compatible with legacy fixtures that only set {doc_id,title,updated_at}:
  #   node_token := obj_token := doc_id, obj_type := "docx", has_child := "0".
  ROWS=$(LIST_JSON_ENV="$LIST_JSON" "$PYBIN" - <<'PY' 2>/dev/null
import os, json, sys
from datetime import datetime, timezone

def to_utc(s):
    if not s:
        return ""
    try:
        if s.endswith("Z"):
            s2 = s[:-1] + "+00:00"
        else:
            s2 = s
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return s

raw = os.environ.get("LIST_JSON_ENV", "")
try:
    data = json.loads(raw) if raw.strip() else []
except Exception:
    sys.exit(0)
if isinstance(data, dict):
    data = data.get("docs") or data.get("items") or []
for d in data:
    if not isinstance(d, dict):
        continue
    obj_token = d.get("obj_token") or d.get("doc_id") or d.get("id") or ""
    node_token = d.get("node_token") or obj_token
    obj_type = d.get("obj_type") or "docx"
    title = (d.get("title") or "").replace("\t", " ").replace("\n", " ")
    # obj_edit_time is unix-seconds (str or int) on the live API; legacy
    # fixtures use ISO-8601 in `updated_at`. Try both.
    edit_time = d.get("obj_edit_time")
    if edit_time:
        try:
            edit_time = int(edit_time)
            updated_at = datetime.fromtimestamp(edit_time, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (TypeError, ValueError):
            updated_at = to_utc(str(edit_time))
    else:
        updated_at = to_utc(d.get("updated_at") or "")
    has_child_raw = d.get("has_child")
    if isinstance(has_child_raw, bool):
        has_child = "1" if has_child_raw else "0"
    elif isinstance(has_child_raw, str):
        has_child = "1" if has_child_raw.lower() == "true" else "0"
    else:
        has_child = "0"
    if not (node_token or obj_token):
        continue
    print("%s\t%s\t%s\t%s\t%s\t%s" % (
        node_token, obj_token, obj_type, title, updated_at, has_child
    ))
PY
  )

  while IFS=$'\t' read -r NODE_TOKEN OBJ_TOKEN OBJ_TYPE TITLE UPDATED_AT HAS_CHILD; do
    [[ -z "${NODE_TOKEN:-}" && -z "${OBJ_TOKEN:-}" ]] && continue

    # Enqueue children for recursion (any obj_type can have children —
    # folders/sheets/mindnotes that *contain* docs are valid intermediate
    # nodes even though we never export them ourselves).
    if [[ "$HAS_CHILD" == "1" && -n "$NODE_TOKEN" ]]; then
      QUEUE+=("${SPACE_ID}"$'\t'"${NODE_TOKEN}")
    fi

    # Only export doc/docx — drive +export to markdown is unreliable for
    # other obj_types (sheet/bitable/mindnote/file/slides).
    if [[ "$OBJ_TYPE" != "doc" && "$OBJ_TYPE" != "docx" ]]; then
      continue
    fi

    DOC_ID="$OBJ_TOKEN"
    [[ -z "$DOC_ID" ]] && continue

    # Sanitize doc_id — reject anything outside [A-Za-z0-9_-] to prevent
    # path traversal or whitespace-injected file targets.
    if [[ ! "$DOC_ID" =~ ^[A-Za-z0-9_-]+$ ]]; then
      log_err "rejected doc_id with unsafe chars: $DOC_ID"
      continue
    fi

    # Dedup by last_ingest_at — both sides are canonical UTC ISO-8601.
    if [[ -n "$UPDATED_AT" && ! "$UPDATED_AT" > "$LAST_INGEST_AT" ]]; then
      continue
    fi

    if ! title_included "$TITLE"; then
      continue
    fi

    OUT_FILE="$RAW_DIR/${DOC_ID}.md"
    TMP_OUT=$(mktemp -d 2>/dev/null) || { log_err "mktemp failed"; continue; }
    # lark-cli drive +export rejects absolute --output-dir for path-traversal
    # safety; cd into TMP_OUT and pass "." so the relative-path check passes
    # while still isolating each doc's export to its own scratch directory.
    if ! ( cd "$TMP_OUT" && lark-cli drive +export \
           --doc-type "${OBJ_TYPE:-docx}" \
           --file-extension markdown \
           --token "$DOC_ID" \
           --output-dir "." \
           --overwrite ) >/dev/null 2>&1; then
      log_err "export failed doc_id=$DOC_ID title=$TITLE"
      rm -rf "$TMP_OUT" 2>/dev/null
      continue
    fi
    EXPORTED=$(find "$TMP_OUT" -maxdepth 1 -type f -name '*.md' 2>/dev/null | head -n 1)
    if [[ -z "$EXPORTED" || ! -s "$EXPORTED" ]]; then
      log_err "export empty doc_id=$DOC_ID title=$TITLE"
      rm -rf "$TMP_OUT" 2>/dev/null
      continue
    fi
    mv "$EXPORTED" "$OUT_FILE" 2>/dev/null
    rm -rf "$TMP_OUT" 2>/dev/null

    printf '%s,feishu-doc,%s,%s\n' "$(now_iso)" "$DOC_ID" "$TITLE" >> "$INDEX_FILE"
    SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    if [[ -n "$UPDATED_AT" && "$UPDATED_AT" > "$MAX_UPDATED" ]]; then
      MAX_UPDATED="$UPDATED_AT"
    fi
  done <<< "$ROWS"
done

if [ "$PROCESSED" -ge "$MAX_NODES" ] && [ ${#QUEUE[@]} -gt 0 ]; then
  log_err "ingest_feishu hit MAX_NODES=$MAX_NODES with ${#QUEUE[@]} unvisited; raise INGEST_MAX_NODES if intended"
fi

#======================================================================
# Bump last_ingest_at only on success — to max(updated_at) of successful docs.
#======================================================================
if [[ $SUCCESS_COUNT -gt 0 ]]; then
  printf '%s\n' "$MAX_UPDATED" > "$LAST_FILE"
fi

# Exit code semantics: see header.
if [[ $SPACE_FAIL_COUNT -gt 0 && $SUCCESS_COUNT -gt 0 ]]; then
  exit 1
fi
exit 0
