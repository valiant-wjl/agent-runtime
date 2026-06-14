"""Tests for scheduler verifier integration (M7-T03).

Five paths:
  1. should_trigger=False  → reply uses draft as-is, send_card not called.
  2. should_trigger=True + verify=PASS  → reply uses draft text.
  3. should_trigger=True + verify=REVISE persistent → reply text contains
     "verifier 仍有疑虑" + concerns.
  4. should_trigger=True + verify=CRASHED (verified=None) → reply text
     contains "verifier 未跑通" + draft.
  5. should_trigger=True + can_trigger=False (rate-limited) → reply text
     contains "verifier 限额已用尽" + draft.

Tests target the ``_maybe_verify`` helper directly (not the full
``_handle_message_inner``) to keep mocking surface small and to avoid
re-exercising the existing M2 scheduler paths.
"""

from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import scheduler
from agent_runtime import verifier as verifier_mod


@pytest.fixture
def fake_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.send_card = AsyncMock(return_value="card-1")
    ch.reply = AsyncMock(return_value=None)
    ch.update_card = AsyncMock(return_value=None)
    return ch


@pytest.fixture
def parsed():
    return ParsedMsg(
        channel="feishu",
        message_id="m-1",
        thread_root_id="t-verify-1",
        chat_id="c-1",
        sender_id="ou-sender",
        sender_name="u",
        text="limit 多少",
        mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


@pytest.fixture
def project_cfg():
    return {"work_dir": "/tmp/proj", "model": "sonnet"}


@pytest.fixture
def runtime_cfg(tmp_path):
    return {
        "paths": {"meta_work_dir": str(tmp_path / "meta")},
    }


@pytest.fixture
def features_cfg(tmp_path):
    # state_file_path scoped to tmp_path so the daemon's persistent budget
    # file (.state/verifier-counters.json) doesn't bleed into pytest runs.
    return {
        "verifier": {
            "enabled": True,
            "max_revise_rounds": 2,
            "cost_cap": {
                "daily_trigger_limit": 200,
                "per_chat_trigger_limit": 30,
                "state_file_path": str(tmp_path / "verifier-counters.json"),
            },
        }
    }


@pytest.fixture(autouse=True)
def _reset_cost_tracker():
    """Reset module-level singleton between tests."""
    scheduler._cost_tracker = None
    yield
    scheduler._cost_tracker = None


@pytest.mark.asyncio
async def test_maybe_verify_skip_when_trigger_false(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch
):
    """should_trigger=False → return draft as-is, no send_card."""
    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(False, "test_skip"),
    )

    async def _fail_verify(**kw):
        raise AssertionError("verifier.verify should not be called when trigger=False")

    monkeypatch.setattr(verifier_mod, "verify", _fail_verify)

    out = await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="limit 多少",
        draft="raw draft",
    )
    assert out == "raw draft"
    fake_channel.send_card.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_verify_pass_path(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch
):
    """should_trigger=True + verify PASS → return draft, send_card called."""
    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    async def fake_verify(**kw):
        return verifier_mod.VerifyResult(verified=True, rounds_used=1)

    monkeypatch.setattr(verifier_mod, "verify", fake_verify)

    out = await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="limit 多少",
        draft="answer with 100 qps",
    )
    assert out == "answer with 100 qps"
    # Best-effort "验证中" card was sent.
    fake_channel.send_card.assert_called_once()


@pytest.mark.asyncio
async def test_maybe_verify_revise_persistent(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch
):
    """verify returns verified=False → reply text contains warning + concerns."""
    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    async def fake_verify(**kw):
        return verifier_mod.VerifyResult(
            verified=False,
            concerns=["数字错: 应该是 200 不是 100", "PSM 名错"],
            rounds_used=2,
        )

    monkeypatch.setattr(verifier_mod, "verify", fake_verify)

    out = await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="limit 多少",
        draft="answer with 100 qps",
    )
    assert "answer with 100 qps" in out
    assert "verifier 仍有疑虑" in out
    assert "2轮后" in out
    assert "数字错: 应该是 200 不是 100" in out
    assert "PSM 名错" in out


@pytest.mark.asyncio
async def test_maybe_verify_crashed_path(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch
):
    """verify returns verified=None (verifier itself crashed) → draft + 未跑通 hint."""
    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    async def fake_verify(**kw):
        return verifier_mod.VerifyResult(
            verified=None,
            error_msg="claude_proc fork failed",
            rounds_used=1,
        )

    monkeypatch.setattr(verifier_mod, "verify", fake_verify)

    out = await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="limit 多少",
        draft="raw draft",
    )
    assert "raw draft" in out
    assert "verifier 未跑通" in out


@pytest.mark.asyncio
async def test_maybe_verify_rate_limited_path(
    fake_channel, parsed, project_cfg, runtime_cfg, monkeypatch
):
    """can_trigger=False (limits exhausted) → draft + 限额已用尽 hint, verify NOT called."""
    # Saturate by setting both limits to 0
    saturated_features = {
        "verifier": {
            "enabled": True,
            "cost_cap": {
                "daily_trigger_limit": 0,
                "per_chat_trigger_limit": 0,
            },
        }
    }
    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    async def _fail_verify(**kw):
        raise AssertionError("verifier.verify must not run when rate-limited")

    monkeypatch.setattr(verifier_mod, "verify", _fail_verify)

    out = await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=saturated_features,
        question="limit 多少",
        draft="raw draft",
    )
    assert "raw draft" in out
    assert "verifier 限额已用尽" in out
    fake_channel.send_card.assert_not_called()


# ---------------------------------------------------------------------------
# US-003: lessons.md injection (Tier 2 verifier loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lessons_injected_into_verifier_question(
    fake_channel, parsed, runtime_cfg, features_cfg, monkeypatch, tmp_path
):
    """When project's knowledge/lessons.md exists, its contents must be
    prepended to the question handed to verifier.verify(). The original
    user question must still appear after the lessons block."""
    work_dir = tmp_path / "proj_with_lessons"
    (work_dir / "knowledge").mkdir(parents=True)
    (work_dir / "knowledge" / "lessons.md").write_text(
        "# Lessons\n\n## 2026-04-30\n- [12:00] 自我介绍 ≤ 3 句\n",
        encoding="utf-8",
    )
    project_cfg = {"work_dir": str(work_dir), "model": "sonnet"}

    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    captured = {}

    async def fake_verify(**kw):
        captured["question"] = kw["question"]
        return verifier_mod.VerifyResult(verified=True, rounds_used=1)

    monkeypatch.setattr(verifier_mod, "verify", fake_verify)

    await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="bot 介绍下你",
        draft="我是 lbp-growth-agent...",
    )

    q = captured["question"]
    assert "PRIOR LESSONS" in q, f"expected PRIOR LESSONS marker; got: {q!r}"
    assert "自我介绍 ≤ 3 句" in q, "lesson body must be folded in"
    assert "USER QUESTION" in q, "user-question delimiter must appear"
    assert "bot 介绍下你" in q, "original question must remain"
    # The lessons block should appear BEFORE the user question.
    assert q.find("PRIOR LESSONS") < q.find("bot 介绍下你")


@pytest.mark.asyncio
async def test_no_lessons_file_question_unchanged(
    fake_channel, parsed, runtime_cfg, features_cfg, monkeypatch, tmp_path
):
    """When knowledge/lessons.md doesn't exist, verifier must receive the
    original question unmodified (backward compatibility)."""
    work_dir = tmp_path / "proj_no_lessons"
    work_dir.mkdir(parents=True)  # no knowledge/ subdir at all
    project_cfg = {"work_dir": str(work_dir), "model": "sonnet"}

    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    captured = {}

    async def fake_verify(**kw):
        captured["question"] = kw["question"]
        return verifier_mod.VerifyResult(verified=True, rounds_used=1)

    monkeypatch.setattr(verifier_mod, "verify", fake_verify)

    await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="原始问题",
        draft="raw",
    )

    assert captured["question"] == "原始问题"
    assert "PRIOR LESSONS" not in captured["question"]


@pytest.mark.asyncio
async def test_empty_lessons_file_question_unchanged(
    fake_channel, parsed, runtime_cfg, features_cfg, monkeypatch, tmp_path
):
    """A whitespace-only lessons.md must be treated as 'no lessons' so the
    injection prefix doesn't bloat verifier prompts with empty headers."""
    work_dir = tmp_path / "proj_empty_lessons"
    (work_dir / "knowledge").mkdir(parents=True)
    (work_dir / "knowledge" / "lessons.md").write_text("\n   \n", encoding="utf-8")
    project_cfg = {"work_dir": str(work_dir), "model": "sonnet"}

    monkeypatch.setattr(
        verifier_mod,
        "should_trigger",
        lambda q, d, user_hint=None: verifier_mod.TriggerDecision(True, "test_trig"),
    )

    captured = {}

    async def fake_verify(**kw):
        captured["question"] = kw["question"]
        return verifier_mod.VerifyResult(verified=True, rounds_used=1)

    monkeypatch.setattr(verifier_mod, "verify", fake_verify)

    await scheduler._maybe_verify(
        channel=fake_channel,
        parsed=parsed,
        project_cfg=project_cfg,
        runtime_cfg=runtime_cfg,
        features_cfg=features_cfg,
        question="原始问题",
        draft="raw",
    )

    assert captured["question"] == "原始问题"
    assert "PRIOR LESSONS" not in captured["question"]
