"""Tests for runtime/alert_judge.py — Claude oneshot judge with fail-open.

Covers (US-003):
  - empty candidates -> None (no LLM call)
  - valid match JSON -> JudgeResult(is_match=True, ...)
  - markdown-fenced JSON parsed
  - broken JSON / non-zero exit / timeout / matched_id not in candidates
    / empty rewritten_conclusion -> is_match=False
  - judge_runner kwargs (model, disallowed_tools, etc.)
  - truncation lengths respected
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from agent_runtime.alert_judge import JudgeResult, judge
from agent_runtime.alert_kb import AlertEntry
from agent_runtime.alert_retriever import Candidate
from agent_runtime.claude_proc import RunResult


def _entry(eid: str, alert: str = "x", conclusion: str = "y") -> AlertEntry:
    return AlertEntry(
        id=eid,
        created_at="2026-05-07T10:00:00+00:00",
        alert_text=alert,
        conclusion=conclusion,
        source_message_id="m",
        status="active",
        hit_count=0,
        last_hit_at=None,
    )


def _ok(text: str) -> RunResult:
    return RunResult(text=text, session_id=None, exit_code=0)


def _fake_runner(text: str = "") -> AsyncMock:
    """Returns a mock that stands in for claude_proc.run."""
    return AsyncMock(return_value=_ok(text))


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_candidates_returns_none():
    runner = AsyncMock()
    out = await judge(
        alert_text="anything",
        candidates=[],
        model="haiku",
        timeout=60,
        work_dir="/tmp",
        judge_runner=runner,
    )
    assert out is None
    runner.assert_not_called()


@pytest.mark.asyncio
async def test_valid_match_json_returns_match():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    payload = json.dumps({
        "is_match": True,
        "matched_id": "alert-001",
        "rewritten_conclusion": "重启 RDS 节点 (实例 X-prod-002)",
        "reason": "根因相同：RDS 主从切换中",
    })
    runner = _fake_runner(payload)

    out = await judge(
        alert_text="rds timeout new instance",
        candidates=cands,
        model="haiku",
        timeout=60,
        work_dir="/tmp",
        judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is True
    assert out.matched_id == "alert-001"
    assert out.rewritten_conclusion.startswith("重启 RDS")
    assert "根因" in out.reason


@pytest.mark.asyncio
async def test_markdown_fenced_json_parsed():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    payload = (
        "```json\n"
        + json.dumps({
            "is_match": True,
            "matched_id": "alert-001",
            "rewritten_conclusion": "ok",
            "reason": "...",
        })
        + "\n```"
    )
    runner = _fake_runner(payload)
    out = await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is True


@pytest.mark.asyncio
async def test_broken_json_returns_no_match():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    runner = _fake_runner("THIS IS NOT JSON {")
    out = await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is False


@pytest.mark.asyncio
async def test_matched_id_not_in_candidates_returns_no_match():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    runner = _fake_runner(json.dumps({
        "is_match": True,
        "matched_id": "alert-fabricated-999",
        "rewritten_conclusion": "ok",
        "reason": "...",
    }))
    out = await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is False


@pytest.mark.asyncio
async def test_empty_rewritten_conclusion_downgraded_to_no_match():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    runner = _fake_runner(json.dumps({
        "is_match": True,
        "matched_id": "alert-001",
        "rewritten_conclusion": "",
        "reason": "...",
    }))
    out = await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is False


@pytest.mark.asyncio
async def test_timeout_returns_no_match():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    runner = AsyncMock(return_value=RunResult(
        text="⚠️ 分析超时", session_id=None, exit_code=-1, timed_out=True
    ))
    out = await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is False


@pytest.mark.asyncio
async def test_non_zero_exit_returns_no_match():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    runner = AsyncMock(return_value=RunResult(
        text="error", session_id=None, exit_code=2
    ))
    out = await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    assert out is not None
    assert out.is_match is False


@pytest.mark.asyncio
async def test_runner_called_with_model_and_disallowed_tools():
    cands = [Candidate(entry=_entry("alert-001"), score=0.8)]
    runner = _fake_runner(json.dumps({
        "is_match": False,
        "matched_id": None,
        "rewritten_conclusion": None,
        "reason": "no",
    }))
    await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=runner,
    )
    runner.assert_called_once()
    call_kwargs = runner.call_args.kwargs
    assert call_kwargs["model"] == "haiku"
    assert call_kwargs["timeout"] == 60
    assert call_kwargs["session_id"] is None
    assert call_kwargs["work_dir"] == "/tmp"
    disallowed = set(call_kwargs.get("disallowed_tools") or [])
    for t in ("Bash", "Edit", "Write", "Read", "NotebookEdit", "WebFetch", "WebSearch", "Task"):
        assert t in disallowed, f"expected {t} in disallowed_tools, got {disallowed}"


@pytest.mark.asyncio
async def test_truncation_lengths_respected():
    """alert_text truncated to 2000 chars; candidate alert to 1000;
    candidate conclusion to 1500."""
    long_alert = "A" * 5000
    long_cand_alert = "B" * 5000
    long_cand_conc = "C" * 5000
    cands = [
        Candidate(
            entry=_entry("alert-001", alert=long_cand_alert, conclusion=long_cand_conc),
            score=0.5,
        )
    ]
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return _ok(json.dumps({
            "is_match": False, "matched_id": None,
            "rewritten_conclusion": None, "reason": "n",
        }))

    await judge(
        alert_text=long_alert, candidates=cands, model="haiku", timeout=60,
        work_dir="/tmp", judge_runner=capture,
    )
    prompt = captured["prompt"]
    # Query is truncated to 2000 chars
    assert prompt.count("A") == 2000
    # Candidate alert truncated to 1000
    assert prompt.count("B") == 1000
    # Candidate conclusion truncated to 1500
    assert prompt.count("C") == 1500


@pytest.mark.asyncio
async def test_judge_injects_lessons_into_prompt(tmp_path):
    """Regression: lessons.md must reach the judge prompt so the rewriter
    obeys preferences (e.g. '+8 时区') instead of preserving the old
    conclusion's stylistic choices verbatim."""
    work_dir = tmp_path
    knowledge = work_dir / "knowledge"
    knowledge.mkdir()
    (knowledge / "lessons.md").write_text(
        "# Lessons\n\n- [12:00] 对话中默认使用 +8 时区\n", encoding="utf-8"
    )
    cands = [Candidate(entry=_entry("alert-001"), score=0.9)]
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return _ok(json.dumps({"is_match": False, "matched_id": None,
                               "rewritten_conclusion": None, "reason": "n"}))

    await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir=str(work_dir), judge_runner=capture,
    )
    prompt = captured["prompt"]
    assert "用户偏好" in prompt or "lessons" in prompt.lower()
    assert "+8 时区" in prompt
    # The "must obey user preferences" rule is emphasised so the rewriter
    # doesn't just copy the old conclusion's style.
    assert "必须遵守" in prompt or "优先级" in prompt


@pytest.mark.asyncio
async def test_judge_no_lessons_file_skips_preferences_section(tmp_path):
    """Backward compat: projects without lessons.md still produce a valid
    judge prompt (just no preferences section)."""
    cands = [Candidate(entry=_entry("alert-001"), score=0.9)]
    captured = {}

    async def capture(**kwargs):
        captured.update(kwargs)
        return _ok(json.dumps({"is_match": False, "matched_id": None,
                               "rewritten_conclusion": None, "reason": "n"}))

    await judge(
        alert_text="x", candidates=cands, model="haiku", timeout=60,
        work_dir=str(tmp_path), judge_runner=capture,
    )
    prompt = captured["prompt"]
    # Preferences section absent → judge still works; the rule line
    # mentioning "用户偏好" is unconditional (so prompts work the same
    # whether or not lessons exist), but the dedicated SECTION with
    # delimiters and "（必须遵守，优先级最高）" tag is only added when
    # lessons are non-empty.
    assert "alert-001" in prompt
    assert "（必须遵守，优先级最高）" not in prompt


@pytest.mark.asyncio
async def test_judge_result_dataclass_shape():
    """Smoke: JudgeResult fields are stable."""
    r = JudgeResult(
        is_match=False, matched_id=None, rewritten_conclusion=None, reason="x"
    )
    assert r.is_match is False
    assert r.matched_id is None
    assert r.rewritten_conclusion is None
    assert r.reason == "x"
