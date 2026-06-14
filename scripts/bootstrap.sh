#!/bin/bash
# scripts/bootstrap.sh — agent-runtime first-time setup
#
# Spec: docs/specs/2026-04-23-agent-runtime-design.md §5.8
# Plan: docs/plans/2026-04-23-mvp-implementation-plan.md M3-T05
#
# Idempotent, resumable, supports --dry-run / --resume / --reset.
# Renders meta/ and project/ templates via runtime/template_render.py and
# updates config.yaml. State persisted to <meta_work_dir>/.bootstrap_state.

set -euo pipefail

#======================================================================
# Constants & globals
#======================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Overridable so tests can sandbox $REPO_ROOT/config.yaml writes; default = real repo.
REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"

DRY_RUN=0
RESUME=0
RESET=0
NON_INTERACTIVE="${BOOTSTRAP_NON_INTERACTIVE:-0}"

# Populated after meta_work_dir prompt.
STATE_FILE=""
STATE_INITIALIZED=0
# Tracks the last path that was actually loaded by state_load so resume
# logic can detect when STATE_FILE shifts (e.g. after prompt_3 changes
# meta_work_dir) and reload the correct state without mixing two files.
LOADED_STATE_FILE=""
# In-memory answer store. We avoid bash 4 associative arrays (macOS ships
# bash 3.2) and instead use dynamically-named variables ANS_<KEY> via
# `printf -v` / `eval`. ANSWER_KEYS tracks defined keys for state_save.
ANSWER_KEYS=""
COMPLETED_STEPS=""   # space-separated list of integers

# Hard-coded available project list per plan.
AVAILABLE_PROJECTS=(project-a project-b)

#======================================================================
# Proxy clearing — per CLAUDE.md, all CLI invocations bypass local proxy.
#======================================================================
clear_proxy() {
    export http_proxy='' https_proxy='' HTTP_PROXY='' HTTPS_PROXY=''
    export NO_PROXY='*' no_proxy='*'
}
clear_proxy

#======================================================================
# Logging helpers
#======================================================================
log()  { printf '[bootstrap] %s\n' "$*"; }
warn() { printf '[bootstrap][warn] %s\n' "$*" >&2; }
err()  { printf '[bootstrap][error] %s\n' "$*" >&2; }
abort() { err "$*"; exit 1; }

#======================================================================
# Answer store — bash 3.2 compatible (no associative arrays).
# Keys must match [A-Za-z_][A-Za-z0-9_]+. Stored as variables ANS_<KEY>.
#======================================================================
ans_set() {
    local key="$1"; shift
    local val="$*"
    # Track key (idempotent).
    case " $ANSWER_KEYS " in
        *" $key "*) : ;;
        *) ANSWER_KEYS="$ANSWER_KEYS $key" ;;
    esac
    # Use printf -v for safe assignment to dynamic variable.
    printf -v "ANS_$key" '%s' "$val"
}

ans_get() {
    local key="$1"
    local default="${2:-}"
    local var="ANS_$key"
    if [ -n "${!var+x}" ]; then
        printf '%s' "${!var}"
    else
        printf '%s' "$default"
    fi
}

ans_has() {
    local key="$1"
    local var="ANS_$key"
    [ -n "${!var+x}" ]
}

ans_clear_all() {
    local k
    for k in $ANSWER_KEYS; do
        unset "ANS_$k"
    done
    ANSWER_KEYS=""
}

#======================================================================
# Arg parsing
#======================================================================
usage() {
    cat <<EOF
Usage: bash scripts/bootstrap.sh [--dry-run|--resume|--reset|-h|--help]

  --dry-run   Print files that would be created and config.yaml diff; no disk writes.
  --resume    Re-read .bootstrap_state and skip already-completed steps.
  --reset     Erase .bootstrap_state and re-prompt all steps (asks confirmation).
  -h, --help  Show this message.

Env:
  BOOTSTRAP_NON_INTERACTIVE=1    Read answers from stdin without TTY prompts (smoke test).
  BOOTSTRAP_STATE_FILE=PATH      Override state file location for --resume (otherwise
                                 \$HOME/work/agent-repos/meta/.bootstrap_state is tried).
EOF
}

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run)  DRY_RUN=1 ;;
            --resume)   RESUME=1 ;;
            --reset)    RESET=1 ;;
            -h|--help)  usage; exit 0 ;;
            *) err "unknown arg: $1"; usage; exit 2 ;;
        esac
        shift
    done
    if [ "$RESUME" = "1" ] && [ "$RESET" = "1" ]; then
        abort "--resume and --reset are mutually exclusive"
    fi
}

#======================================================================
# Prereq checks — qmd=warn-only, others abort on missing.
#======================================================================
check_prereqs() {
    log "checking prerequisites"
    local missing=()

    # python 3.10+
    if ! command -v python3 >/dev/null 2>&1; then
        missing+=("python3 (3.10+)")
    else
        local pyver
        pyver=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        local major minor
        major=${pyver%.*}; minor=${pyver#*.}
        if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
            missing+=("python3 >=3.10 (found $pyver)")
        fi
    fi

    # claude CLI
    command -v claude >/dev/null 2>&1 || missing+=("claude (Claude Max CLI)")
    # git
    command -v git >/dev/null 2>&1 || missing+=("git")
    # lark-cli (for feishu channel + ingest.feishu_docs — required for MVP)
    command -v lark-cli >/dev/null 2>&1 || missing+=("lark-cli")
    # qmd — warn only per plan
    if ! command -v qmd >/dev/null 2>&1; then
        warn "qmd not found (warn-only — needed for some quote/markdown previews)"
    fi

    if [ "${#missing[@]}" -gt 0 ]; then
        err "missing required prerequisites:"
        for m in "${missing[@]}"; do err "  - $m"; done
        abort "install missing tools and re-run"
    fi

    # NO_PROXY connectivity sanity check — non-fatal.
    if command -v curl >/dev/null 2>&1; then
        if ! curl -sS -o /dev/null -m 5 https://example.com 2>/dev/null; then
            warn "https://example.com unreachable in 5s — check network / proxy clearing"
        fi
    fi

    log "prereqs OK"
}

#======================================================================
# Interactive prompt helpers (TTY-friendly)
#======================================================================
ask() {
    # ask "question" "default"  -> echoes the answer
    local prompt="$1"
    local default="${2:-}"
    local reply
    local got_input=1
    if [ "$NON_INTERACTIVE" = "1" ]; then
        # No TTY prompt; just consume one line from stdin.
        if ! IFS= read -r reply; then
            got_input=0
            reply=""
        fi
    else
        if [ -n "$default" ]; then
            printf '%s [%s]: ' "$prompt" "$default" >&2
        else
            printf '%s: ' "$prompt" >&2
        fi
        if ! IFS= read -r reply; then
            got_input=0
            reply=""
        fi
    fi
    if [ -z "$reply" ] && [ -n "$default" ]; then
        reply="$default"
    fi
    if [ "$NON_INTERACTIVE" = "1" ] && [ "$got_input" = "0" ] && [ -z "$default" ]; then
        abort "non-interactive: missing input for $prompt"
    fi
    printf '%s' "$reply"
}

confirm() {
    # confirm "question" "default y|n"  -> exit 0 if yes
    local prompt="$1"
    local default="${2:-n}"
    local hint="(y/N)"; [ "$default" = "y" ] && hint="(Y/n)"
    local reply
    if [ "$NON_INTERACTIVE" = "1" ]; then
        if ! IFS= read -r reply; then reply=""; fi
    else
        printf '%s %s ' "$prompt" "$hint" >&2
        if ! IFS= read -r reply; then reply=""; fi
    fi
    [ -z "$reply" ] && reply="$default"
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

#======================================================================
# State (.bootstrap_state JSON) — atomic write, in-memory mirror.
#======================================================================
state_path_set() {
    local meta_dir="$1"
    STATE_FILE="$meta_dir/.bootstrap_state"
}

state_load() {
    if [ -z "$STATE_FILE" ] || [ ! -f "$STATE_FILE" ]; then
        STATE_INITIALIZED=1
        return 0
    fi
    # Parse via python (already a hard dep).
    local dump
    dump=$(PYTHONPATH="$REPO_ROOT" python3 - "$STATE_FILE" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    data = json.load(f)
print("__STEPS__", " ".join(str(s) for s in data.get("completed_steps", [])))
for k, v in (data.get("answers") or {}).items():
    if isinstance(v, (list, dict)):
        v = json.dumps(v, ensure_ascii=False)
    # encode newlines/tabs so the bash side can recover them
    v = str(v).replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t")
    print(f"__ANS__\t{k}\t{v}")
PY
)
    local line rest key val
    while IFS= read -r line; do
        case "$line" in
            "__STEPS__ "*) COMPLETED_STEPS="${line#__STEPS__ }" ;;
            "__ANS__	"*)
                rest="${line#__ANS__	}"
                key="${rest%%	*}"
                val="${rest#*	}"
                # Decode escapes (order matters: do \\ last via temp marker).
                val="${val//\\\\/$'\1'}"
                val="${val//\\n/$'\n'}"
                val="${val//\\t/$'\t'}"
                val="${val//$'\1'/\\}"
                ans_set "$key" "$val"
                ;;
        esac
    done <<< "$dump"
    STATE_INITIALIZED=1
}

state_save() {
    [ "$STATE_INITIALIZED" = "1" ] || return 0
    [ -z "$STATE_FILE" ] && return 0
    [ "$DRY_RUN" = "1" ] && return 0

    local meta_dir
    meta_dir="$(dirname "$STATE_FILE")"
    mkdir -p "$meta_dir"

    # Serialize answers as KEY\tVALUE lines via env. Newlines/tabs/backslashes
    # in values are escaped so each answer fits on one stdin line; python decodes.
    local payload="" k v
    for k in $ANSWER_KEYS; do
        v="$(ans_get "$k")"
        v="${v//\\/\\\\}"
        v="${v//$'\n'/\\n}"
        v="${v//$'\t'/\\t}"
        payload+="$k	$v"$'\n'
    done

    PYTHONPATH="$REPO_ROOT" ANS_PAYLOAD="$payload" \
        python3 - "$STATE_FILE" "$COMPLETED_STEPS" <<'PY'
import json, os, sys
state_path = sys.argv[1]
steps_str = sys.argv[2].strip()
steps = sorted({int(s) for s in steps_str.split() if s})
answers = {}
for line in os.environ.get("ANS_PAYLOAD", "").splitlines():
    if not line:
        continue
    k, _, v = line.partition("\t")
    # decode escapes (order: \\ first via marker)
    v = v.replace("\\\\", "\x01").replace("\\n", "\n").replace("\\t", "\t").replace("\x01", "\\")
    answers[k] = v
out = {"version": 1, "completed_steps": steps, "answers": answers}
tmp = state_path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False, sort_keys=True)
os.replace(tmp, state_path)
PY
}

state_completed() {
    local step="$1"
    case " $COMPLETED_STEPS " in
        *" $step "*) return 0 ;;
        *) return 1 ;;
    esac
}

state_mark_done() {
    local step="$1"
    state_completed "$step" && return 0
    COMPLETED_STEPS="$COMPLETED_STEPS $step"
    state_save
}

state_reset() {
    if [ -f "$STATE_FILE" ]; then
        if ! confirm "This will erase ${STATE_FILE} and re-prompt all questions. Continue?" "n"; then
            abort "aborted by user"
        fi
        rm -f "$STATE_FILE"
        log "state file removed: $STATE_FILE"
    fi
    COMPLETED_STEPS=""
    ans_clear_all
}

#======================================================================
# Step skip helper. Each prompt_N function calls `step_skip N || return 0`.
#======================================================================
step_skip() {
    local step="$1"
    if [ "$RESUME" = "1" ] && state_completed "$step"; then
        log "step $step already done — skipping (resume)"
        return 1   # NOT-skipped → 1 means "do NOT execute"
    fi
    return 0
}

#======================================================================
# 11 prompts
#======================================================================

# 1. USER_NAME
prompt_1_name() {
    step_skip 1 || return 0
    local v
    v=$(ask "1) Your display name (USER_NAME)" "$(ans_get USER_NAME)")
    [ -z "$v" ] && abort "USER_NAME is required"
    ans_set USER_NAME "$v"
    state_mark_done 1
}

# 2. Feishu open_id
prompt_2_open_id() {
    step_skip 2 || return 0
    local v
    v=$(ask "2) Your Feishu open_id (ou_...)" "$(ans_get OPEN_ID "")")
    [ -z "$v" ] && abort "OPEN_ID is required"
    case "$v" in
        ou_*) : ;;
        *) warn "open_id should start with 'ou_' (got: $v) — proceeding anyway" ;;
    esac
    ans_set OPEN_ID "$v"
    state_mark_done 2
}

# 3. meta_work_dir
prompt_3_meta_dir() {
    step_skip 3 || return 0
    local default="$(ans_get META_WORK_DIR "$HOME/work/agent-repos/meta")"
    local v
    v=$(ask "3) meta_work_dir path" "$default")
    [ -z "$v" ] && abort "META_WORK_DIR is required"
    # Expand ~ and relative.
    v="${v/#\~/$HOME}"
    case "$v" in
        /*) : ;;
        *) v="$(pwd)/$v" ;;
    esac
    ans_set META_WORK_DIR "$v"
    state_path_set "$v"   # state file lives inside meta_work_dir
    state_mark_done 3
}

# 4. enabled projects (multi-select, default billing)
prompt_4_projects() {
    step_skip 4 || return 0
    log "4) Available projects: ${AVAILABLE_PROJECTS[*]}"
    local default="$(ans_get PROJECTS "billing")"
    local v
    v=$(ask "   Comma/space-separated list of projects to enable" "$default")
    [ -z "$v" ] && v="billing"
    # Normalize: split on commas/spaces, validate against AVAILABLE_PROJECTS.
    local sel=()
    local tok
    for tok in ${v//,/ }; do
        local found=0
        for ap in "${AVAILABLE_PROJECTS[@]}"; do
            if [ "$tok" = "$ap" ]; then found=1; break; fi
        done
        if [ "$found" = "1" ]; then
            sel+=("$tok")
        else
            warn "unknown project '$tok' — ignored (valid: ${AVAILABLE_PROJECTS[*]})"
        fi
    done
    [ "${#sel[@]}" -eq 0 ] && abort "no valid projects selected"
    ans_set PROJECTS "${sel[*]}"
    state_mark_done 4
}

# 5. work_dir for each enabled project
prompt_5_project_dirs() {
    step_skip 5 || return 0
    local p
    # shellcheck disable=SC2206
    local enabled=($(ans_get PROJECTS))
    for p in "${enabled[@]}"; do
        local key="PROJECT_DIR_${p}"
        local default="$(ans_get "$key" "$HOME/work/agent-repos/$p")"
        local v
        v=$(ask "5) work_dir for project '$p'" "$default")
        v="${v/#\~/$HOME}"
        case "$v" in
            /*) : ;;
            *) v="$(pwd)/$v" ;;
        esac
        ans_set "$key" "$v"
    done
    state_mark_done 5
}

# 6. bot_mention_key
prompt_6_mention_key() {
    step_skip 6 || return 0
    local default
    default="$(ans_get BOT_MENTION_KEY "$(ans_get OPEN_ID)")"
    local v
    v=$(ask "6) Bot mention key (open_id of the bot account)" "$default")
    [ -z "$v" ] && abort "BOT_MENTION_KEY is required"
    ans_set BOT_MENTION_KEY "$v"
    state_mark_done 6
}

# 7. backup remote URL — private validation
prompt_7_backup_remote() {
    step_skip 7 || return 0
    local v
    v=$(ask "7) Backup git remote URL for meta/ (private only; blank = skip)" "$(ans_get BACKUP_REMOTE "")")
    if [ -z "$v" ]; then
        ans_set BACKUP_REMOTE ""
        state_mark_done 7
        return 0
    fi
    # Regex check for "looks private".
    local looks_private=0
    case "$v" in
        *.private.*|*.internal.*) looks_private=1 ;;
        *github.com*)
            # github.com/<user>/repo — only treat as private if NOT in a public-org list.
            # Heuristic: if URL contains 'public' or 'oss' or 'opensource', NOT private.
            case "$v" in
                *public*|*opensource*|*oss/*) looks_private=0 ;;
                *) looks_private=1 ;;
            esac
            ;;
    esac
    if [ "$looks_private" -ne 1 ]; then
        if ! confirm "URL '$v' does not match known private patterns. Confirm it is PRIVATE?" "n"; then
            abort "backup remote rejected as not private"
        fi
    fi
    # Connectivity check: git ls-remote (network call, proxy already cleared).
    if ! git ls-remote "$v" >/dev/null 2>&1; then
        warn "git ls-remote failed for $v (network/auth?)"
        if ! confirm "Continue anyway and store URL?" "n"; then
            abort "backup remote unreachable"
        fi
    fi
    ans_set BACKUP_REMOTE "$v"
    state_mark_done 7
}

# 8. ingest feishu_docs + source_spaces
prompt_8_ingest_feishu() {
    step_skip 8 || return 0
    if confirm "8) Enable feishu_docs ingest?" "n"; then
        ans_set INGEST_FEISHU_ENABLED "true"
        local v
        v=$(ask "   Source spaces (comma-separated wiki space IDs; blank = skip for now)" \
                "$(ans_get INGEST_FEISHU_SPACES "")")
        ans_set INGEST_FEISHU_SPACES "$v"
    else
        ans_set INGEST_FEISHU_ENABLED "false"
        ans_set INGEST_FEISHU_SPACES ""
    fi
    state_mark_done 8
}

# 9. ingest mr_notes
prompt_9_ingest_mr() {
    step_skip 9 || return 0
    if confirm "9) Enable MR notes ingest?" "n"; then
        ans_set MR_INGEST_ENABLED "true"
        local v
        v=$(ask "   GitLab user for MR fetch" "$(ans_get MR_GITLAB_USER "")")
        ans_set MR_GITLAB_USER "$v"
    else
        ans_set MR_INGEST_ENABLED "false"
        ans_set MR_GITLAB_USER ""
    fi
    state_mark_done 9
}

# 10. verifier
prompt_10_verifier() {
    step_skip 10 || return 0
    if confirm "10) Enable verifier subagent?" "y"; then
        ans_set VERIFIER_ENABLED "true"
        local v
        v=$(ask "   daily_trigger_limit" "$(ans_get VERIFIER_DAILY_LIMIT "200")")
        case "$v" in
            ''|*[!0-9]*) abort "daily_trigger_limit must be integer" ;;
        esac
        ans_set VERIFIER_DAILY_LIMIT "$v"
    else
        ans_set VERIFIER_ENABLED "false"
        ans_set VERIFIER_DAILY_LIMIT "0"
    fi
    state_mark_done 10
}

# 11. SOUL/USER/EVERGREEN guided Q&A — 10-15 follow-up free-text questions.
prompt_11_persona_qa() {
    step_skip 11 || return 0
    log "11) Persona Q&A — answers will be appended to SOUL.md / USER.md / EVERGREEN.md"
    log "    (press Enter on blank to skip a question)"
    local -a questions=(
        "Q1  你的工作场景一句话描述（公司/团队/方向）"
        "Q2  你说话的风格（务实/幽默/学术/简洁/先结论后论据）"
        "Q3  你 2-3 条核心价值观或做事原则"
        "Q4  你与同事/上级/下属合作时最看重什么"
        "Q5  本季 OKR 简述（O1/O2 关键词）"
        "Q6  当前活跃项目 3-5 个（项目名 + 一句话角色）"
        "Q7  禁区关键词（薪资/HC/未公开规划之外，你还想加什么）"
        "Q8  常用的口头禅或拒绝模式（如 \"我没把握，建议直接找本人\"）"
        "Q9  沟通偏好：中/英、正式/随意、详尽/简短"
        "Q10 喜欢的回复格式（结论先行？markdown 列表？带代码块？）"
        "Q11 你最不希望数字人替你做的事"
        "Q12 你期望数字人能帮你做的最有价值的一件事"
        "Q13 当前最关注的技术主题（用于 wiki ingest 优先级）"
        "Q14 历史踩过的最大坑（让数字人提醒未来的你）"
        "Q15 任何想留给数字人的备注"
    )
    local q answer
    local qa_text=""
    for q in "${questions[@]}"; do
        answer=$(ask "$q" "")
        if [ -n "$answer" ]; then
            qa_text+="**${q}**"$'\n'"${answer}"$'\n\n'
        fi
    done
    ans_set PERSONA_QA "$qa_text"
    state_mark_done 11
}

#======================================================================
# Render: meta/ templates and project/ templates
#======================================================================

# render_one TEMPLATE OUTPUT KEY1=VAL1 KEY2=VAL2 ...
# Reads template, renders via runtime.template_render, writes to OUTPUT.
# In dry-run mode, prints the path that would be created and the missing-vars
# check (still runs the renderer but discards the output).
render_one() {
    local template="$1"
    local output="$2"
    shift 2
    # Encode each value as base64 to safely transport multi-line content
    # (e.g. PROJECTS_LIST with N projects becomes N newline-separated items;
    # naive newline-joined KV strings split on those internal newlines and
    # corrupt the parser). Format on the wire: "KEY=<base64-of-value>\n".
    local kv_lines=""
    local kv k v v_b64
    for kv in "$@"; do
        k="${kv%%=*}"
        v="${kv#*=}"
        v_b64="$(printf '%s' "$v" | base64 | tr -d '\n')"
        kv_lines+="$k=$v_b64"$'\n'
    done
    if [ "$DRY_RUN" = "1" ]; then
        log "  would render: $template -> $output"
    fi
    PYTHONPATH="$REPO_ROOT" KV_INPUT="$kv_lines" python3 - "$template" "$output" "$DRY_RUN" <<'PY'
import base64, os, sys, pathlib
from agent_runtime.template_render import render_template, TemplateError

template, output, dry = sys.argv[1], sys.argv[2], sys.argv[3]
ctx = {}
for line in os.environ.get("KV_INPUT", "").splitlines():
    if not line:
        continue
    k, _, v_b64 = line.partition("=")
    ctx[k] = base64.b64decode(v_b64).decode("utf-8") if v_b64 else ""
try:
    rendered = render_template(template, ctx)
except TemplateError as e:
    print(f"TemplateError: missing {e.missing_vars} in {e.path}", file=sys.stderr)
    sys.exit(3)

if dry == "1":
    sys.exit(0)

out_path = pathlib.Path(output)
out_path.parent.mkdir(parents=True, exist_ok=True)
# Preserve existing user-edited content for SOUL/USER/EVERGREEN/MEMORY.
keep = out_path.name in {"SOUL.md", "USER.md", "EVERGREEN.md", "MEMORY.md"}
if keep and out_path.exists():
    existing = out_path.read_text(encoding="utf-8")
    # Heuristic: if file size > template size + 100 bytes OR contains "##" past
    # the template, treat as user-edited and skip.
    if len(existing) > len(rendered) + 100 or "<!-- USER_EDITED -->" in existing:
        print(f"PRESERVE: {out_path} (user-edited; not overwriting)")
        sys.exit(0)
out_path.write_text(rendered, encoding="utf-8")
print(f"WROTE: {out_path}")
PY
}

build_projects_list() {
    # Emit a markdown bullet list for {{PROJECTS_LIST}} substitution.
    # shellcheck disable=SC2206
    local enabled=($(ans_get PROJECTS))
    local out=""
    local p
    for p in "${enabled[@]}"; do
        local d="$(ans_get "PROJECT_DIR_$p" "")"
        out+="- $p (work_dir: $d)"$'\n'
    done
    printf '%s' "$out"
}

render_meta() {
    local meta_dir="$(ans_get META_WORK_DIR)"
    local user_name="$(ans_get USER_NAME)"
    local projects_list
    projects_list="$(build_projects_list)"

    log "rendering meta/ templates -> $meta_dir"

    # Files that take only USER_NAME.
    render_one "$REPO_ROOT/templates/meta/CLAUDE.md.template"  "$meta_dir/CLAUDE.md"  "USER_NAME=$user_name"
    render_one "$REPO_ROOT/templates/meta/AGENTS.md.template"  "$meta_dir/AGENTS.md"  "USER_NAME=$user_name"
    render_one "$REPO_ROOT/templates/meta/SOUL.md.template"    "$meta_dir/SOUL.md"    "USER_NAME=$user_name"
    render_one "$REPO_ROOT/templates/meta/USER.md.template"    "$meta_dir/USER.md"    "USER_NAME=$user_name"
    render_one "$REPO_ROOT/templates/meta/MEMORY.md.template"  "$meta_dir/MEMORY.md"
    # EVERGREEN takes USER_NAME + PROJECTS_LIST
    render_one "$REPO_ROOT/templates/meta/EVERGREEN.md.template" "$meta_dir/EVERGREEN.md" \
        "USER_NAME=$user_name" "PROJECTS_LIST=$projects_list"

    # .claude/settings.json — FRAMEWORK_DIR substitution
    render_one "$REPO_ROOT/templates/meta/.claude/settings.json.template" \
        "$meta_dir/.claude/settings.json" "FRAMEWORK_DIR=$REPO_ROOT"

    # .claude/mcp.json — META_DIR substitution (qmd MCP server)
    render_one "$REPO_ROOT/templates/meta/.claude/mcp.json.template" \
        "$meta_dir/.claude/mcp.json" "META_DIR=$meta_dir"

    # Copy verifier agent (no template vars; just copy if present).
    if [ -f "$REPO_ROOT/templates/meta/.claude/agents/verifier.md" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            log "  would copy: .claude/agents/verifier.md"
        else
            mkdir -p "$meta_dir/.claude/agents"
            cp "$REPO_ROOT/templates/meta/.claude/agents/verifier.md" \
               "$meta_dir/.claude/agents/verifier.md"
            log "  copied: $meta_dir/.claude/agents/verifier.md"
        fi
    fi

    # Slash commands: compile-wiki.md needs FRAMEWORK_DIR + META_DIR substitution;
    # save.md / evergreen-refresh.md / autoresearch.md are static (just copy).
    local cmd_src="$REPO_ROOT/templates/meta/.claude/commands"
    local cmd_dst="$meta_dir/.claude/commands"
    if [ -f "$cmd_src/compile-wiki.md.template" ]; then
        render_one "$cmd_src/compile-wiki.md.template" \
            "$cmd_dst/compile-wiki.md" \
            "FRAMEWORK_DIR=$REPO_ROOT" "META_DIR=$meta_dir"
    fi
    local cmd_file
    for cmd_file in save.md evergreen-refresh.md autoresearch.md; do
        if [ -f "$cmd_src/$cmd_file" ]; then
            if [ "$DRY_RUN" = "1" ]; then
                log "  would copy: .claude/commands/$cmd_file"
            else
                mkdir -p "$cmd_dst"
                cp "$cmd_src/$cmd_file" "$cmd_dst/$cmd_file"
                log "  copied: $cmd_dst/$cmd_file"
            fi
        fi
    done

    # Append persona Q&A under marker if any.
    local qa="$(ans_get PERSONA_QA "")"
    if [ -n "$qa" ]; then
        if [ "$DRY_RUN" = "1" ]; then
            log "  would append PERSONA_QA -> $meta_dir/USER.md and $meta_dir/SOUL.md"
        else
            local marker="## Bootstrap Q&A (raw)"
            for f in USER.md SOUL.md; do
                local tgt="$meta_dir/$f"
                if [ -f "$tgt" ] && ! grep -qF "$marker" "$tgt"; then
                    {
                        printf '\n\n%s\n\n' "$marker"
                        printf '_Captured during bootstrap %s; curate or move into the right section._\n\n' \
                               "$(date '+%Y-%m-%d')"
                        printf '%s' "$qa"
                    } >> "$tgt"
                    log "  appended Q&A to $tgt"
                fi
            done
        fi
    fi
}

render_project() {
    # shellcheck disable=SC2206
    local enabled=($(ans_get PROJECTS))
    local user_name="$(ans_get USER_NAME)"
    local p
    for p in "${enabled[@]}"; do
        local pdir="$(ans_get "PROJECT_DIR_$p")"
        # PSM placeholder (user fills later) — bootstrap default = "TBD"
        local psm="TBD-${p}"
        log "rendering project '$p' -> $pdir"
        render_one "$REPO_ROOT/templates/project/CLAUDE.md.template"    "$pdir/CLAUDE.md" \
            "PROJECT_NAME=$p" "PSM=$psm" "USER_NAME=$user_name"
        # EVERGREEN template's literal `{{ }}` comment uses whitespace inside
        # braces, which the strict renderer ignores per its docstring — so no
        # phony VAR={{VAR}} workaround needed.
        render_one "$REPO_ROOT/templates/project/EVERGREEN.md.template" "$pdir/EVERGREEN.md" \
            "PROJECT_NAME=$p" "PSM=$psm" "USER_NAME=$user_name"
        # knowledge/README.md is not a template — copy verbatim.
        if [ "$DRY_RUN" = "1" ]; then
            log "  would copy: knowledge/README.md -> $pdir/knowledge/README.md"
        else
            mkdir -p "$pdir/knowledge"
            cp "$REPO_ROOT/templates/project/knowledge/README.md" "$pdir/knowledge/README.md"
            log "  copied: $pdir/knowledge/README.md"
        fi
    done
}

#======================================================================
# config.yaml: merge user answers into existing config (or create from example).
#======================================================================
update_config_yaml() {
    local target="$REPO_ROOT/config.yaml"
    local source="$target"
    [ ! -f "$source" ] && source="$REPO_ROOT/config.example.yaml"
    log "updating config.yaml (source: $source)"

    # Build env for python.
    local enabled_csv="$(ans_get PROJECTS)"
    enabled_csv="${enabled_csv// /,}"

    local kv_env=""
    kv_env+="META_WORK_DIR=$(ans_get META_WORK_DIR)"$'\n'
    kv_env+="OPEN_ID=$(ans_get OPEN_ID)"$'\n'
    kv_env+="BOT_MENTION_KEY=$(ans_get BOT_MENTION_KEY)"$'\n'
    kv_env+="BACKUP_REMOTE=$(ans_get BACKUP_REMOTE "")"$'\n'
    kv_env+="INGEST_FEISHU_ENABLED=$(ans_get INGEST_FEISHU_ENABLED "false")"$'\n'
    kv_env+="INGEST_FEISHU_SPACES=$(ans_get INGEST_FEISHU_SPACES "")"$'\n'
    kv_env+="MR_INGEST_ENABLED=$(ans_get MR_INGEST_ENABLED "false")"$'\n'
    kv_env+="MR_GITLAB_USER=$(ans_get MR_GITLAB_USER "")"$'\n'
    kv_env+="VERIFIER_ENABLED=$(ans_get VERIFIER_ENABLED "true")"$'\n'
    kv_env+="VERIFIER_DAILY_LIMIT=$(ans_get VERIFIER_DAILY_LIMIT "200")"$'\n'
    kv_env+="PROJECTS=$enabled_csv"$'\n'
    # shellcheck disable=SC2206
    local enabled=($(ans_get PROJECTS))
    local p
    for p in "${enabled[@]}"; do
        kv_env+="PROJECT_DIR_${p}=$(ans_get "PROJECT_DIR_$p")"$'\n'
    done

    PYTHONPATH="$REPO_ROOT" KV_INPUT="$kv_env" \
        python3 - "$source" "$target" "$DRY_RUN" <<'PY'
import os, sys, pathlib, difflib

source, target, dry = sys.argv[1], sys.argv[2], sys.argv[3]

ctx = {}
for line in os.environ["KV_INPUT"].splitlines():
    if not line:
        continue
    k, _, v = line.partition("=")
    ctx[k] = v

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed (pip install pyyaml)", file=sys.stderr)
    sys.exit(4)

with open(source, encoding="utf-8") as f:
    original = f.read()
data = yaml.safe_load(original) or {}

# Apply answers.
data.setdefault("paths", {})["meta_work_dir"] = ctx["META_WORK_DIR"]
data.setdefault("channels", {}).setdefault("feishu", {})["bot_mention_key"] = ctx["BOT_MENTION_KEY"]

# Projects: keep only enabled ones; preserve template defaults for kept ones,
# update work_dir + admin_users.
project_names = [p for p in ctx["PROJECTS"].split(",") if p]
existing_projects = data.get("projects", {}) or {}
new_projects = {}
template_proj = next(iter(existing_projects.values()), {}) if existing_projects else {}
import copy
for name in project_names:
    if name in existing_projects:
        base = copy.deepcopy(existing_projects[name])
    else:
        base = copy.deepcopy(template_proj) if template_proj else {}
        # Reset name-derived fields when copying from a template project.
        base["display_name"] = f"{name.capitalize()}Bot"
        base["routing_keywords"] = [name]
    base["work_dir"] = ctx[f"PROJECT_DIR_{name}"]
    base["admin_users"] = [ctx["OPEN_ID"]]
    base.setdefault("display_name", f"{name.capitalize()}Bot")
    new_projects[name] = base
data["projects"] = new_projects

# Features.
data.setdefault("features", {}).setdefault("verifier", {})
data["features"]["verifier"]["enabled"] = (ctx["VERIFIER_ENABLED"] == "true")
data["features"]["verifier"].setdefault("cost_cap", {})["daily_trigger_limit"] = int(ctx["VERIFIER_DAILY_LIMIT"])

# Ingest.
data.setdefault("ingest", {}).setdefault("feishu_docs", {})
data["ingest"]["feishu_docs"]["enabled"] = (ctx["INGEST_FEISHU_ENABLED"] == "true")
spaces = ctx.get("INGEST_FEISHU_SPACES", "")
data["ingest"]["feishu_docs"]["source_spaces"] = [s for s in spaces.replace(",", " ").split() if s]
data["ingest"].setdefault("mr_notes", {})
data["ingest"]["mr_notes"]["enabled"] = (ctx["MR_INGEST_ENABLED"] == "true")
data["ingest"]["mr_notes"]["gitlab_user"] = ctx.get("MR_GITLAB_USER", "")

# Backup.
data.setdefault("backup", {})["meta_remote"] = ctx.get("BACKUP_REMOTE", "")

new_yaml = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)

if dry == "1":
    print("=== DRY RUN: config.yaml diff ===")
    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        new_yaml.splitlines(keepends=True),
        fromfile=source, tofile=target,
    )
    sys.stdout.writelines(diff)
    sys.exit(0)

tmp = target + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write(new_yaml)
os.replace(tmp, target)
print(f"WROTE: {target}")
PY
}

#======================================================================
# Dry-run summary
#======================================================================
print_dry_summary() {
    local meta_dir="$(ans_get META_WORK_DIR)"
    cat <<EOF

=== DRY RUN: files that WOULD be created ===
  $meta_dir/CLAUDE.md
  $meta_dir/AGENTS.md
  $meta_dir/SOUL.md
  $meta_dir/USER.md
  $meta_dir/MEMORY.md
  $meta_dir/EVERGREEN.md
  $meta_dir/.claude/settings.json
  $meta_dir/.claude/agents/verifier.md
EOF
    # shellcheck disable=SC2206
    local enabled=($(ans_get PROJECTS))
    local p
    for p in "${enabled[@]}"; do
        local pdir="$(ans_get "PROJECT_DIR_$p")"
        echo "  $pdir/CLAUDE.md"
        echo "  $pdir/EVERGREEN.md"
        echo "  $pdir/knowledge/README.md"
    done
    echo ""
}

#======================================================================
# Backup-pipeline closure (post-incident 2026-05-07).
# Three idempotent steps that complete what BACKUP_REMOTE setup needs:
#   - init_meta_repo:        git init + .gitignore + remote add
#   - update_meta_config_yaml: write backup.meta_remote into meta/config.yaml
#                              (read by scripts/backup-wiki.sh)
#   - install_cron:          invoke scripts/install-cron.sh
# Skipped or no-op'd in DRY_RUN; safe to re-run on --resume.
#======================================================================

init_meta_repo() {
    local meta_dir backup_remote
    meta_dir="$(ans_get META_WORK_DIR)"
    backup_remote="$(ans_get BACKUP_REMOTE "")"

    if [ -z "$backup_remote" ]; then
        log "skip git init: BACKUP_REMOTE blank (user opted out of backup)"
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        log "  would: git init -b main + .gitignore + remote add origin $backup_remote (in $meta_dir)"
        return 0
    fi

    if [ -d "$meta_dir/.git" ]; then
        log "  $meta_dir already a git repo — skip init"
    else
        ( cd "$meta_dir" && git init -b main >/dev/null )
        log "  git init -b main -> $meta_dir"
    fi

    local gi="$meta_dir/.gitignore"
    if [ ! -f "$gi" ]; then
        cat > "$gi" <<'EOF'
# Bootstrap / runtime state — local-only, never tracked.
.bootstrap_state
.state/

# Logs
*.log

# Editor noise
.DS_Store
*.swp
EOF
        log "  wrote $gi"
    fi

    if ! ( cd "$meta_dir" && git remote get-url origin >/dev/null 2>&1 ); then
        ( cd "$meta_dir" && git remote add origin "$backup_remote" )
        log "  remote add origin $backup_remote (in $meta_dir)"
    fi
}

update_meta_config_yaml() {
    local meta_dir backup_remote
    meta_dir="$(ans_get META_WORK_DIR)"
    backup_remote="$(ans_get BACKUP_REMOTE "")"

    if [ -z "$backup_remote" ]; then
        log "skip meta config backup section: BACKUP_REMOTE blank"
        return 0
    fi

    if [ "$DRY_RUN" = "1" ]; then
        log "  would write backup.meta_remote=$backup_remote -> $meta_dir/config.yaml"
        return 0
    fi

    local mcfg="$meta_dir/config.yaml"
    python3 - "$mcfg" "$backup_remote" <<'PY'
import os, sys
try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed", file=sys.stderr); sys.exit(1)
mcfg, remote = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(mcfg):
    with open(mcfg, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
data.setdefault("backup", {})["meta_remote"] = remote
header = (
    "# meta/config.yaml — per-agent ingest + backup configuration.\n"
    "# Read by scripts/ingest_feishu.sh and scripts/backup-wiki.sh.\n"
    "# Framework runtime config lives in lab/agent-runtime/config.yaml.\n\n"
)
tmp = mcfg + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    fh.write(header)
    yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True,
                   default_flow_style=False)
os.replace(tmp, mcfg)
print(f"WROTE: {mcfg}")
PY
    log "  wrote backup.meta_remote -> $mcfg"
}

install_cron() {
    if [ "$DRY_RUN" = "1" ]; then
        log "  would invoke: bash scripts/install-cron.sh"
        return 0
    fi
    log "installing cron entries (managed block)..."
    bash "$REPO_ROOT/scripts/install-cron.sh"
}

#======================================================================
# SIGINT / EXIT trap
#======================================================================
on_sigint() {
    echo ""
    warn "interrupted (SIGINT)"
    if [ -n "$STATE_FILE" ] && [ "$DRY_RUN" != "1" ]; then
        state_save && log "state flushed to $STATE_FILE"
        log "resume with: bash scripts/bootstrap.sh --resume"
    fi
    exit 130
}
trap on_sigint INT

on_err() {
    local rc=$?
    # Disarm the trap immediately to prevent recursive entry if state_save or
    # the err() helper itself trips ERR.
    trap - ERR
    err "command failed (exit $rc); flushing state for --resume"
    if [ -n "$STATE_FILE" ] && [ "$DRY_RUN" != "1" ]; then
        state_save || true
    fi
    exit $rc
}
trap on_err ERR

#======================================================================
# Main
#======================================================================
main() {
    parse_args "$@"
    log "agent-runtime bootstrap (repo: $REPO_ROOT)"
    [ "$DRY_RUN" = "1" ] && log "*** DRY RUN: no disk writes ***"

    check_prereqs

    # First three prompts run before state_load can find a state file
    # (state lives inside meta_work_dir, which prompt 3 establishes).
    # We try to load state from default location (or BOOTSTRAP_STATE_FILE
    # override) on --resume so prompts 1+2 can be skipped.
    if [ "$RESUME" = "1" ]; then
        local override="${BOOTSTRAP_STATE_FILE:-}"
        local guess="${override:-$HOME/work/agent-repos/meta/.bootstrap_state}"
        if [ -f "$guess" ]; then
            STATE_FILE="$guess"
            state_load
            LOADED_STATE_FILE="$guess"
            log "resumed state from $STATE_FILE (completed steps:$COMPLETED_STEPS)"
        fi
    fi

    prompt_1_name
    prompt_2_open_id
    prompt_3_meta_dir

    # After step 3, the canonical state path is known. If it differs from
    # what we already loaded (e.g. user supplied a non-default meta_work_dir
    # without BOOTSTRAP_STATE_FILE), clear in-memory answers and re-load
    # from the correct location to avoid bleeding two states together.
    if [ "$RESET" = "1" ]; then
        state_reset
    elif [ "$STATE_FILE" != "$LOADED_STATE_FILE" ] && [ -f "$STATE_FILE" ]; then
        ans_clear_all
        COMPLETED_STEPS=""
        state_load
        LOADED_STATE_FILE="$STATE_FILE"
    elif [ "$STATE_INITIALIZED" != "1" ]; then
        # Brand-new run, no state file exists yet — initialize empty state.
        state_load
    fi
    state_mark_done 1
    state_mark_done 2
    state_mark_done 3

    prompt_4_projects
    prompt_5_project_dirs
    prompt_6_mention_key
    prompt_7_backup_remote
    prompt_8_ingest_feishu
    prompt_9_ingest_mr
    prompt_10_verifier
    prompt_11_persona_qa

    if [ "$DRY_RUN" = "1" ]; then
        print_dry_summary
        update_config_yaml
        init_meta_repo
        update_meta_config_yaml
        install_cron
        log "dry run complete (no files written)"
        return 0
    fi

    # Render/config steps 12-17 — state-tracked per spec §5.8 so a mid-step
    # failure leaves .bootstrap_state at the last clean step and --resume
    # picks up exactly where it stopped. Steps 15-17 close the backup
    # pipeline (incident 2026-05-07): without them, the meta dir was never
    # git-init'd, meta/config.yaml lacked backup.meta_remote, and cron was
    # never installed — leaving the configured remote silently empty.
    if ! state_completed 12; then render_meta;             state_mark_done 12; fi
    if ! state_completed 13; then render_project;          state_mark_done 13; fi
    if ! state_completed 14; then update_config_yaml;      state_mark_done 14; fi
    if ! state_completed 15; then init_meta_repo;          state_mark_done 15; fi
    if ! state_completed 16; then update_meta_config_yaml; state_mark_done 16; fi
    if ! state_completed 17; then install_cron;            state_mark_done 17; fi

    log "bootstrap complete."
    log "Next: review $(ans_get META_WORK_DIR)/SOUL.md / USER.md / EVERGREEN.md"
    log "      and run: bash scripts/run.sh"
}

main "$@"
