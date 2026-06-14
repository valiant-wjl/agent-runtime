"""Tests for runtime/alert_resolver.py — orchestration layer.

Covers (US-005):
  - is_alert_message decision matrix
  - try_handle_alert_hit: hit (reply + mark_hit) / miss / empty cands /
    judge None / matched_id missing in cand list
  - sink_after_deep happy path + swallowed-failure
  - run_sweep_loop schedule (mock now_fn + sleep_fn)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import alert_resolver
from agent_runtime.alert_judge import JudgeResult
from agent_runtime.alert_kb import AlertEntry
from agent_runtime.alert_retriever import Candidate


def _parsed(*, chat_id="oc_alert", sender_type="app", text="boom rds timeout") -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id="om_x",
        thread_root_id="t_x",
        chat_id=chat_id,
        sender_id="ou_bot" if sender_type == "app" else "ou_alice",
        sender_name="bot" if sender_type == "app" else "alice",
        text=text,
        mentions=[],
        sender_type=sender_type,
    )


def _project_cfg(work_dir: Path) -> dict:
    return {"work_dir": str(work_dir), "model": "opus"}


_ALERT_CFG_BASE = {
    "enabled": True,
    "ttl_days": 14,
    "retriever": "keyword",
    "top_k": 3,
    "judge_timeout": 60,
    "judge_model": "haiku",
    "alert_chats": [{"chat_id": "oc_alert", "project": "example_project"}],
    "sweep": {"enabled": True, "hour": 4},
}


# ---------------------------------------------------------------------------
# is_alert_message
# ---------------------------------------------------------------------------


def test_is_alert_message_disabled_returns_false():
    cfg = {**_ALERT_CFG_BASE, "enabled": False}
    ok, route = alert_resolver.is_alert_message(_parsed(), cfg)
    assert ok is False and route is None


def test_is_alert_message_chat_not_in_list():
    ok, route = alert_resolver.is_alert_message(
        _parsed(chat_id="oc_random"), _ALERT_CFG_BASE
    )
    assert ok is False and route is None


def test_is_alert_message_human_sender_not_alert():
    ok, route = alert_resolver.is_alert_message(
        _parsed(sender_type="user"), _ALERT_CFG_BASE
    )
    assert ok is False and route is None


def test_is_alert_message_unknown_sender_type_not_alert():
    """sender_type=None (unset) must not be treated as alert."""
    ok, route = alert_resolver.is_alert_message(
        _parsed(sender_type=None), _ALERT_CFG_BASE
    )
    assert ok is False and route is None


def test_is_alert_message_bot_in_alert_chat_returns_route():
    ok, route = alert_resolver.is_alert_message(_parsed(), _ALERT_CFG_BASE)
    assert ok is True
    assert route == {"chat_id": "oc_alert", "project": "example_project"}


def test_is_alert_message_missing_alert_resolver_section_safe():
    ok, route = alert_resolver.is_alert_message(_parsed(), {})
    assert ok is False and route is None


# ---------------------------------------------------------------------------
# try_handle_alert_hit
# ---------------------------------------------------------------------------


def _seed_kb(work_dir: Path, chat_id: str, alert_text: str = "boom rds timeout prod") -> AlertEntry:
    kb = alert_resolver.make_kb(str(work_dir))
    return kb.add(
        chat_id=chat_id,
        alert_text=alert_text,
        conclusion="restart node X",
        source_message_id="om_old",
    )


@pytest.mark.asyncio
async def test_try_handle_alert_hit_returns_false_on_empty_kb(tmp_path):
    project = _project_cfg(tmp_path)
    kb = alert_resolver.make_kb(str(tmp_path))
    retriever = MagicMock()
    retriever.search.return_value = []
    judge_fn = AsyncMock()  # should not be called
    channel = AsyncMock()

    out = await alert_resolver.try_handle_alert_hit(
        _parsed(), project, _ALERT_CFG_BASE, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert out is False
    judge_fn.assert_not_called()
    channel.reply.assert_not_called()


@pytest.mark.asyncio
async def test_try_handle_alert_hit_returns_false_when_judge_misses(tmp_path):
    project = _project_cfg(tmp_path)
    seeded = _seed_kb(tmp_path, "oc_alert")
    kb = alert_resolver.make_kb(str(tmp_path))
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=seeded, score=0.5)]
    judge_fn = AsyncMock(return_value=JudgeResult(False, None, None, "no"))
    channel = AsyncMock()

    out = await alert_resolver.try_handle_alert_hit(
        _parsed(), project, _ALERT_CFG_BASE, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert out is False
    channel.reply.assert_not_called()


@pytest.mark.asyncio
async def test_try_handle_alert_hit_replies_and_marks(tmp_path):
    project = _project_cfg(tmp_path)
    seeded = _seed_kb(tmp_path, "oc_alert")
    kb = alert_resolver.make_kb(str(tmp_path))
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=seeded, score=0.9)]
    judge_fn = AsyncMock(return_value=JudgeResult(
        True, seeded.id, "重启 X 节点（按当前时间替换）", "根因相同"
    ))
    channel = AsyncMock()
    channel.reply = AsyncMock()

    out = await alert_resolver.try_handle_alert_hit(
        _parsed(), project, _ALERT_CFG_BASE, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert out is True

    # Reply formatted with original ts + new count + rewritten conclusion.
    channel.reply.assert_called_once()
    text = channel.reply.call_args[0][1]
    assert seeded.id in text
    assert "第 1 次复用" in text
    assert "重启 X 节点" in text
    assert seeded.created_at in text

    # mark_hit ran: entry's hit_count bumped.
    kb_file = tmp_path / "knowledge" / "alerts" / "oc_alert.jsonl"
    rows = [json.loads(line) for line in kb_file.read_text().splitlines() if line]
    assert rows[0]["hit_count"] == 1
    assert rows[0]["last_hit_at"] is not None


@pytest.mark.asyncio
async def test_try_handle_alert_hit_judge_returns_none_treated_as_miss(tmp_path):
    project = _project_cfg(tmp_path)
    seeded = _seed_kb(tmp_path, "oc_alert")
    kb = alert_resolver.make_kb(str(tmp_path))
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=seeded, score=0.9)]
    judge_fn = AsyncMock(return_value=None)  # candidates empty case shouldn't happen, but be defensive
    channel = AsyncMock()
    out = await alert_resolver.try_handle_alert_hit(
        _parsed(), project, _ALERT_CFG_BASE, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert out is False


@pytest.mark.asyncio
async def test_try_handle_alert_hit_mark_hit_failure_swallowed(tmp_path):
    """Reply was sent; if mark_hit fails (e.g., file permission), don't crash."""
    project = _project_cfg(tmp_path)
    seeded = _seed_kb(tmp_path, "oc_alert")
    kb = MagicMock()
    kb.mark_hit.side_effect = OSError("disk full")
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=seeded, score=0.9)]
    judge_fn = AsyncMock(return_value=JudgeResult(True, seeded.id, "ok", "r"))
    channel = AsyncMock()

    out = await alert_resolver.try_handle_alert_hit(
        _parsed(), project, _ALERT_CFG_BASE, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert out is True
    channel.reply.assert_called_once()


# ---------------------------------------------------------------------------
# sink_after_deep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sink_after_deep_writes_entry(tmp_path):
    kb = alert_resolver.make_kb(str(tmp_path))
    parsed = _parsed(text="boom!")
    real_conclusion = "重启 X-prod-002 节点；已确认 Y 服务恢复，告警自愈。"
    entry = await alert_resolver.sink_after_deep(parsed, real_conclusion, kb=kb)
    assert entry is not None
    rows = [json.loads(line) for line in (tmp_path / "knowledge" / "alerts" / "oc_alert.jsonl").read_text().splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["alert_text"] == "boom!"
    assert rows[0]["conclusion"] == real_conclusion
    assert rows[0]["source_message_id"] == "om_x"


@pytest.mark.asyncio
async def test_sink_after_deep_skips_timeout_sentinel(tmp_path):
    """Regression: a "⚠️ 分析超时" conclusion must NOT be sinked — retrieval
    would surface it as a candidate and judge would waste cycles on it."""
    kb = alert_resolver.make_kb(str(tmp_path))
    parsed = _parsed(text="real alert")
    out = await alert_resolver.sink_after_deep(
        parsed, "⚠️ 分析超时", kb=kb,
    )
    assert out is None
    # No file written
    assert not (tmp_path / "knowledge" / "alerts" / "oc_alert.jsonl").exists()


@pytest.mark.asyncio
async def test_sink_after_deep_skips_too_short_conclusion(tmp_path):
    """A 11-char conclusion is almost certainly a fallback string."""
    kb = alert_resolver.make_kb(str(tmp_path))
    parsed = _parsed(text="real alert")
    out = await alert_resolver.sink_after_deep(parsed, "短回复", kb=kb)
    assert out is None


@pytest.mark.asyncio
async def test_sink_after_deep_accepts_real_conclusion(tmp_path):
    kb = alert_resolver.make_kb(str(tmp_path))
    parsed = _parsed(text="real alert")
    long_conclusion = "重启 X-prod-002 节点；已确认 Y 服务恢复，告警自愈。"
    out = await alert_resolver.sink_after_deep(parsed, long_conclusion, kb=kb)
    assert out is not None
    assert out.conclusion == long_conclusion


@pytest.mark.asyncio
async def test_sink_after_deep_failure_returns_none(tmp_path):
    kb = MagicMock()
    kb.add.side_effect = OSError("disk full")
    parsed = _parsed()
    out = await alert_resolver.sink_after_deep(parsed, "x", kb=kb)
    assert out is None


# ---------------------------------------------------------------------------
# run_sweep_loop schedule
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_sweep_loop_calls_sweep_at_target_hour():
    """Mock time so we can verify scheduling math without waiting."""
    kb = MagicMock()
    kb.sweep = MagicMock(return_value=3)
    sleep_calls: list[float] = []

    # Pretend "now" is 2026-05-07 23:30:00 local.
    fixed_now = datetime(2026, 5, 7, 23, 30, 0).timestamp()

    def now_fn():
        return fixed_now

    iter_count = {"n": 0}

    async def sleep_fn(s):
        sleep_calls.append(s)
        iter_count["n"] += 1
        # First sleep returns normally so sweep gets a chance to run; the
        # second iteration's sleep cancels the loop.
        if iter_count["n"] >= 2:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await alert_resolver.run_sweep_loop(
            kbs=[kb],
            alert_cfg=_ALERT_CFG_BASE,
            now_fn=now_fn, sleep_fn=sleep_fn,
        )

    # Hour=4, now=23:30 → target = next day 04:00 → 4h30m = 16200 seconds.
    assert sleep_calls
    assert 16000 <= sleep_calls[0] <= 16400
    kb.sweep.assert_called_once()


@pytest.mark.asyncio
async def test_run_sweep_loop_continues_on_sweep_error():
    kb = MagicMock()
    kb.sweep.side_effect = [OSError("one bad day"), 0]
    iter_count = {"n": 0}

    def now_fn():
        return datetime(2026, 5, 7, 23, 30, 0).timestamp()

    async def sleep_fn(s):
        iter_count["n"] += 1
        # Allow two full sweep iterations before cancelling.
        if iter_count["n"] >= 3:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await alert_resolver.run_sweep_loop(
            kbs=[kb], alert_cfg=_ALERT_CFG_BASE,
            now_fn=now_fn, sleep_fn=sleep_fn,
        )
    assert kb.sweep.call_count == 2


# ---------------------------------------------------------------------------
# make_kb
# ---------------------------------------------------------------------------


def test_make_kb_resolves_under_work_dir(tmp_path):
    kb = alert_resolver.make_kb(str(tmp_path))
    e = kb.add(chat_id="oc_x", alert_text="t", conclusion="c", source_message_id="m")
    expected = tmp_path / "knowledge" / "alerts" / "oc_x.jsonl"
    assert expected.is_file()
    assert e.id.startswith("alert-")
