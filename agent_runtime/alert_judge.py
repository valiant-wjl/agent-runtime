"""Alert judge — single Claude oneshot to decide same-class.

Contract:
  - judge() returns None when there are no candidates (caller treats
    as "miss" without burning a Claude call).
  - On any error (timeout, non-zero exit, invalid JSON, schema
    mismatch, fabricated matched_id, empty rewritten_conclusion) the
    judge returns ``JudgeResult(is_match=False, ...)``. Errors are
    logged at warning level — never raised. This is the fail-open
    discipline: the judge must not block the alert path.

The prompt is plain Chinese + JSON schema; the runner has all tools
disallowed because the judge needs no filesystem / web / code execution.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agent_runtime import claude_proc
from agent_runtime.alert_retriever import Candidate

log = logging.getLogger(__name__)

# Truncation budget. Pre-truncated by Python len() (chars), not bytes —
# token spend is approximate, but plenty headroom for haiku context.
_MAX_QUERY_CHARS = 2000
_MAX_CAND_ALERT_CHARS = 1000
_MAX_CAND_CONC_CHARS = 1500

# Judge runs without any tool access — purely a reasoning + JSON-output call.
_JUDGE_DISALLOWED_TOOLS = [
    "Bash", "Edit", "Write", "Read", "NotebookEdit", "WebFetch", "WebSearch", "Task",
]

JudgeRunner = Callable[..., Awaitable[claude_proc.RunResult]]


@dataclass
class JudgeResult:
    is_match: bool
    matched_id: str | None
    rewritten_conclusion: str | None
    reason: str


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n]


# Cap on lessons.md content baked into the judge prompt. lessons grow over
# time; we keep the cap modest so token spend stays predictable.
_MAX_LESSONS_CHARS = 1500


def _read_user_preferences(work_dir: str | None) -> str:
    """Read ``<work_dir>/knowledge/lessons.md`` if present, return its
    body capped at _MAX_LESSONS_CHARS. Errors and missing files yield ""
    so the judge prompt simply omits the preferences section.

    Why bake lessons into the judge prompt:
      The hit-and-rewrite path NEVER goes through the deep-investigation
      step where the project's CLAUDE.md instructs the agent to read
      lessons.md. Without injection, the rewriter preserves the old
      conclusion's stylistic choices (timezone, format, register) even
      after the user adds a `/lesson` correcting them. Injecting
      lessons.md here makes user preferences first-class for the
      rewriter without burning a deep-investigation Claude call.
    """
    if not work_dir:
        return ""
    try:
        from pathlib import Path
        p = Path(work_dir) / "knowledge" / "lessons.md"
        if not p.is_file():
            return ""
        body = p.read_text(encoding="utf-8")
        return _truncate(body, _MAX_LESSONS_CHARS)
    except Exception as e:
        log.warning("alert_judge: read lessons.md failed: %s", e)
        return ""


def _build_prompt(
    alert_text: str,
    candidates: list[Candidate],
    *,
    user_preferences: str = "",
) -> str:
    parts: list[str] = [
        "你是告警值班助手。下面是一条新告警，以及历史最相似的若干条告警 + 当时给出的结论。",
        "",
        "判断：新告警是否本质同一类问题（根因相同、触发条件相同）？",
        "",
        "规则：",
        "- 仅当根因 / 触发条件相同才算「同类」；表面相似但根因不同的不算。",
        "- 如果同类，请基于该历史结论改写为适配当前告警参数的版本（替换时间戳 / 服务实例 / 数值等）。",
        "- **改写时必须遵守下方「用户偏好」中的所有指令**（时区、措辞、格式等）— 老结论里如果违反偏好，需要改正而不是照搬。",
        "- 输出**严格 JSON**，不要包裹任何 markdown 代码块、不要前后加解释文字。",
        "",
        "JSON schema:",
        '{"is_match": bool, "matched_id": "alert-..." | null, "rewritten_conclusion": "..." | null, "reason": "<不超过 60 字>"}',
    ]
    if user_preferences.strip():
        parts.extend([
            "",
            "用户偏好（必须遵守，优先级最高）:",
            "---",
            user_preferences.strip(),
            "---",
        ])
    parts.extend([
        "",
        "新告警:",
        "---",
        _truncate(alert_text, _MAX_QUERY_CHARS),
        "---",
        "",
        "历史候选:",
    ])
    for i, c in enumerate(candidates, start=1):
        e = c.entry
        parts.extend([
            f"[{i}] id={e.id} created_at={e.created_at} hit_count={e.hit_count} score={c.score:.3f}",
            "alert: " + _truncate(e.alert_text, _MAX_CAND_ALERT_CHARS),
            "conclusion: " + _truncate(e.conclusion, _MAX_CAND_CONC_CHARS),
            "",
        ])
    return "\n".join(parts)


def _strip_json_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        # drop opening fence (with optional language tag like ```json)
        first_nl = s.find("\n")
        if first_nl != -1:
            s = s[first_nl + 1 :]
    if s.endswith("```"):
        s = s[: -3].rstrip()
    return s


async def judge(
    *,
    alert_text: str,
    candidates: list[Candidate],
    model: str | None,
    timeout: int,
    work_dir: str,
    judge_runner: JudgeRunner = claude_proc.run,
) -> JudgeResult | None:
    """Return None iff candidates is empty; otherwise a JudgeResult.

    Never raises. On any error path returns ``is_match=False`` so the
    caller falls through to deep investigation.
    """
    if not candidates:
        return None

    user_prefs = _read_user_preferences(work_dir)
    prompt = _build_prompt(
        alert_text=alert_text,
        candidates=candidates,
        user_preferences=user_prefs,
    )
    valid_ids = {c.entry.id for c in candidates}

    try:
        result = await judge_runner(
            work_dir=work_dir,
            prompt=prompt,
            timeout=timeout,
            session_id=None,
            disallowed_tools=list(_JUDGE_DISALLOWED_TOOLS),
            model=model,
            stream=False,
        )
    except Exception as e:
        log.warning("alert_judge: runner crashed: %s", e)
        return JudgeResult(False, None, None, f"runner crash: {e!r}")

    if getattr(result, "timed_out", False):
        log.warning("alert_judge: claude timed out")
        return JudgeResult(False, None, None, "timed out")
    if result.exit_code != 0:
        log.warning("alert_judge: claude exit=%s", result.exit_code)
        return JudgeResult(False, None, None, f"exit={result.exit_code}")

    raw = _strip_json_fence(result.text or "")
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("alert_judge: not JSON (%s); raw=%r", e, raw[:200])
        return JudgeResult(False, None, None, "json parse error")

    if not isinstance(data, dict):
        log.warning("alert_judge: top-level not object: %r", type(data))
        return JudgeResult(False, None, None, "not object")

    is_match = bool(data.get("is_match"))
    matched_id = data.get("matched_id")
    rewritten = data.get("rewritten_conclusion")
    reason = str(data.get("reason") or "")[:200]

    if is_match:
        if matched_id not in valid_ids:
            log.warning(
                "alert_judge: matched_id %r not in candidate set %s",
                matched_id, sorted(valid_ids),
            )
            return JudgeResult(False, None, None, "fabricated matched_id")
        if not isinstance(rewritten, str) or not rewritten.strip():
            log.warning("alert_judge: empty rewritten_conclusion despite is_match=True")
            return JudgeResult(False, None, None, "empty rewritten_conclusion")
        return JudgeResult(True, matched_id, rewritten, reason)

    return JudgeResult(False, None, None, reason or "no match")
