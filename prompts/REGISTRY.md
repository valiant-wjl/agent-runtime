# Prompts Registry — digital-agent

This file is the **single source of truth** for every LLM-facing template
in this repo. Every edit MUST update the corresponding entry below in the
SAME commit, or `templates/tests/test_prompt_registry.py` will fail in CI.

Why this exists: the sibling repo `~/lab/issue-driven-fixer` (ralph round 2,
2026-05-23) had a single prompt change unintentionally cap LLM-emitted
confidence at 0.55 across 15 events. The bug was undetectable for 2 days
because there was no registry tying prompt edits to expected impact. This
registry forces every change to leave a hypothesis row + sha256 marker,
so silent regressions get caught at commit time.

## How to change a template

1. Edit the template file (e.g. `templates/meta/SOUL.md.template`).
2. Update **this** REGISTRY:
   - Bump `Last logged sha256` (`sha256sum <path>`).
   - Bump `Last logged commit` to the SHA you're about to create (or to the
     previous SHA and rely on the test failing in CI to force a real update).
   - Add a new row to **Change history** describing what + why + (later)
     observed impact.
3. `git add` both files and commit them together.
4. After deploy, observe N >= 5 real bot interactions and fill the
   `Impact observed` cell with what changed.

## Rollback

`git revert <sha>` reverts both prompt + REGISTRY together (single commit),
so the test passes again.

---

## templates/meta/SOUL.md.template

- **Purpose**: Bot personality + behavioral rules baseline. Compiled into every project's `meta/SOUL.md` on workspace bootstrap.
- **Owner**: maintainer
- **Last logged commit**: `130c0df8ab75b08b19055c0303807d51135ce793`
- **Last logged sha256**: `d53ae62b24472c8556130a912fa23a50aa3ea6489287662f3ad93188e0ff69c0`

### Change history

| Commit | Date | Summary | Impact observed |
|---|---|---|---|
| `130c0df` | 2026-04-24 | M3-T01 baseline (initial) | Baseline |

---

## templates/meta/USER.md.template

- **Purpose**: User persona baseline. Compiled into every project's `meta/USER.md`. Sets tone, no-go zones, decision style.
- **Owner**: maintainer
- **Last logged commit**: `130c0df8ab75b08b19055c0303807d51135ce793`
- **Last logged sha256**: `96eddc24e71b9d5224a5da6baeba71e1f1903bcb392aa6b276faadde66f9bb51`

### Change history

| Commit | Date | Summary | Impact observed |
|---|---|---|---|
| `130c0df` | 2026-04-24 | M3-T01 baseline (initial) | Baseline |

---

## templates/meta/EVERGREEN.md.template

- **Purpose**: Current OKR / active projects / no-go zones — refreshed periodically. The "what is now" reference the agent loads at every conversation.
- **Owner**: maintainer
- **Last logged commit**: `3e9fb051d3b64f35057ef878c813a463aa92d2e2`
- **Last logged sha256**: `975985010b327b6796ded0348b5e63a2e1dfcfb526e082eef114c914d3fc5adf`

### Change history

| Commit | Date | Summary | Impact observed |
|---|---|---|---|
| `130c0df` | 2026-04-24 | M3-T01 baseline (initial) | Baseline |
| `3e9fb05` | 2026-05-05 | Describe approval mechanism without literal token | Avoids accidental triggers of approval flow during agent self-description |

---

## templates/meta/CLAUDE.md.template

- **Purpose**: meta-`CLAUDE.md` baseline that every meta workspace inherits — the "agent startup instructions" loaded by Claude Code itself.
- **Owner**: maintainer
- **Last logged commit**: `pending`
- **Last logged sha256**: `3dc49cb1014209bd8bd0766b2772a0a5e30d4f20a3c8221dd590fbdd8e28733e`

### Change history

| Commit | Date | Summary | Impact observed |
|---|---|---|---|
| `130c0df` | 2026-04-24 | M3-T01 baseline (initial) | Baseline |
| `96b8c45` | 2026-05-19 | (most recent update) | (not measured) |
| `302bf2e` | 2026-05-25 | 审批块加「环境」字段 + 分级说明（BOE 发起人可批 / 线上仅管理员），操作字段必须非空 | (not measured) |
| `92ed2c5` | 2026-05-25 | 讲清审批通过后由写阶段(Claude 自己,完整权限)执行命令，禁止把命令甩给用户手动跑 | (not measured) |
| `pending` | 2026-05-26 | 写操作分两类：平台写(TCC/RDS/TCE)直接执行不走审批，仅无下游兜底的写(git push 等)走审批块 | (not measured) |

---

## templates/meta/AGENTS.md.template

- **Purpose**: Subagent registry baseline. Lists available specialized agents and when to invoke them.
- **Owner**: maintainer
- **Last logged commit**: `3e9fb051d3b64f35057ef878c813a463aa92d2e2`
- **Last logged sha256**: `8b30e16e7c61a7183ed15685b9cf9c2f40c9218d63397d26482ca8f552f0a7d7`

### Change history

| Commit | Date | Summary | Impact observed |
|---|---|---|---|
| `130c0df` | 2026-04-24 | M3-T01 baseline (initial) | Baseline |
| `3e9fb05` | 2026-05-05 | Describe approval mechanism without literal token | (orthogonal to subagent semantics) |

---

## templates/meta/MEMORY.md.template

- **Purpose**: Memory index baseline. Schema for the agent's persistent memory system.
- **Owner**: maintainer
- **Last logged commit**: `130c0df8ab75b08b19055c0303807d51135ce793`
- **Last logged sha256**: `2bf3734a19aae507392a0f17cab87da8ae54d09ba7313ebed7fe78ef07b51ad4`

### Change history

| Commit | Date | Summary | Impact observed |
|---|---|---|---|
| `130c0df` | 2026-04-24 | M3-T01 baseline (initial) | Baseline |

---

## Inline prompts (not file-tracked)

These are LLM prompts constructed in Python code, not files. They are
NOT tracked by `test_prompt_registry.py` (which is sha256-based) but
should be reviewed with the same discipline. If they grow in complexity
or get tuned for behavior, consider moving them to dedicated `.md`
files and adding them above.

| Location | What | Last touched |
|---|---|---|
| `runtime/claude_proc.py:122-130` | `system_prompt` construction (`--append-system-prompt` to claude CLI) | check `git log -L 122,130:runtime/claude_proc.py` |
| `runtime/alert_judge.py:165` | `_build_prompt(...)` for alert-judging | `_build_prompt` function above |
| `runtime/scheduler.py:797,805` | Text + image + topic-history read prompt builder | `_build_read_prompt` function |
| `runtime/scheduler.py:1460` | Inline prompt for (scheduler-internal usage) | (review separately) |

---

## Adding a new tracked template

If you add a new `.template` (or any other LLM-facing file the agent ingests):

1. Add a new top-level section here matching the schema above.
2. Add the path to `TRACKED_PROMPTS` in `templates/tests/test_prompt_registry.py`.
3. Commit all three together.

## Cross-repo note

The sibling repo `issue-driven-fixer` has its own `prompts/REGISTRY.md`
tracking `fixer/diagnoser_prompt.md`. The two registries are intentionally
separate — different repos, different release cycles, different owners
when those diverge. Don't try to cross-track.
