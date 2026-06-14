"""US-cmd-002: /alert <text> command in scheduler.

Tests the test-entry behaviour:
  - retriever runs against the FIRST alert_chats target_chat_id (not
    parsed.chat_id, which may be a DM)
  - hit → reply with debug prefix + rewritten conclusion; NO mark_hit
  - miss / no candidates / judge error → reply with debug; never trigger
    deep investigation
  - never sinks to kb (test entry, no production-data pollution)
  - resolver disabled / no alert_chats → friendly error message
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import alert_resolver, concurrency, scheduler, session
from agent_runtime.alert_judge import JudgeResult


_ALERT_CFG = {
    "enabled": True,
    "ttl_days": 14,
    "retriever": "keyword",
    "top_k": 3,
    "judge_timeout": 60,
    "judge_model": "haiku",
    "alert_chats": [{"chat_id": "oc_alert_target", "project": "p"}],
}

_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


def _parsed(text: str, *, chat_id: str = "oc_dm_user", sender_type: str = "user") -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id="om_cmd",
        thread_root_id="om_cmd",
        chat_id=chat_id,
        sender_id="ou_alice",
        sender_name="alice",
        text=text,
        mentions=[],
        chat_type="p2p",
        sender_type=sender_type,
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


def _channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.reply = AsyncMock(return_value=None)
    ch.send_card = AsyncMock(return_value="card-1")
    ch.update_card = AsyncMock(return_value=None)
    return ch


def _project_cfg(work_dir: Path) -> dict:
    return {
        "work_dir": str(work_dir),
        "model": "opus",
        "admin_users": ["ou_admin"],
        "approval_timeout": 1800,
        "read_phase": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "disallowed_bash_patterns": [],
        },
        "write_phase": {"timeout": 600},
        "supported_msg_types": ["text", "post"],
    }


@pytest.fixture(autouse=True)
def _scheduler_init():
    concurrency.init_global(10)
    yield


# ---------------------------------------------------------------------------
# Empty / config-error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_cmd_usage_when_body_empty(tmp_path):
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed("/alert")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    run_mock.assert_not_called()
    ch.reply.assert_called_once()
    assert "用法" in ch.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_alert_cmd_disabled_alert_resolver(tmp_path):
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed("/alert RDS 超时")
    disabled_cfg = {**_ALERT_CFG, "enabled": False}
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=disabled_cfg,
        )
    run_mock.assert_not_called()
    ch.reply.assert_called_once()
    assert "alert_resolver" in ch.reply.call_args[0][1]


@pytest.mark.asyncio
async def test_alert_cmd_no_alert_chats(tmp_path):
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed("/alert RDS 超时")
    cfg_no_chats = {**_ALERT_CFG, "alert_chats": []}
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=cfg_no_chats,
        )
    run_mock.assert_not_called()
    ch.reply.assert_called_once()
    assert "alert_chats" in ch.reply.call_args[0][1]


# ---------------------------------------------------------------------------
# Retriever + judge flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_cmd_no_candidates_replies_debug(tmp_path):
    """KB empty for the target chat_id → debug reply, no judge call."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed("/alert 全新告警")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock, \
         patch("agent_runtime.scheduler.alert_judge.judge", AsyncMock()) as judge_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    run_mock.assert_not_called()
    judge_mock.assert_not_called()
    ch.reply.assert_called_once()
    text = ch.reply.call_args[0][1]
    assert "🧪" in text
    assert "未找到候选" in text or "未命中" in text


@pytest.mark.asyncio
async def test_alert_cmd_miss_replies_with_candidates(tmp_path):
    """Candidates exist but judge says no match → debug reply with the
    candidate ids so dev can see what was considered."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    # Seed kb with one entry under the TARGET chat_id (not the DM).
    kb = alert_resolver.make_kb(str(work_dir))
    kb.add(
        chat_id="oc_alert_target", alert_text="RDS timeout host=x-prod",
        conclusion="重启 X 节点恢复正常，建议监控连接池。",
        source_message_id="om_seed",
    )
    ch = _channel()
    parsed = _parsed("/alert 完全无关的告警")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock, \
         patch("agent_runtime.scheduler.alert_judge.judge",
               AsyncMock(return_value=JudgeResult(False, None, None, "不同根因"))) as judge_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    run_mock.assert_not_called()
    judge_mock.assert_called_once()
    ch.reply.assert_called_once()
    text = ch.reply.call_args[0][1]
    assert "🧪" in text
    assert "未命中" in text
    # Candidate id surfaced for visibility
    assert "alert-" in text


@pytest.mark.asyncio
async def test_alert_cmd_hit_replies_with_debug_prefix_and_no_mark_hit(tmp_path):
    """Judge hit → reply with debug prefix + rewritten conclusion; the
    underlying entry's hit_count must NOT be incremented (test entry)."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    # Seed kb under target chat_id
    kb = alert_resolver.make_kb(str(work_dir))
    seeded = kb.add(
        chat_id="oc_alert_target",
        alert_text="RDS timeout host=x-prod",
        conclusion="重启 X 节点恢复正常",
        source_message_id="om_seed",
    )
    ch = _channel()
    parsed = _parsed("/alert RDS 超时 host=x-prod")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock, \
         patch("agent_runtime.scheduler.alert_judge.judge",
               AsyncMock(return_value=JudgeResult(
                   True, seeded.id, "重启 Y-prod 节点（按当前实例改写）", "根因相同"
               ))) as judge_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    run_mock.assert_not_called()
    judge_mock.assert_called_once()
    ch.reply.assert_called_once()
    text = ch.reply.call_args[0][1]
    assert "🧪" in text
    assert seeded.id in text
    assert "重启 Y-prod 节点" in text
    assert "dry-run" in text or "不计数" in text or "不写" in text

    # hit_count must remain 0 — command is a test entry, no mutation
    kb_file = work_dir / "knowledge" / "alerts" / "oc_alert_target.jsonl"
    rows = [json.loads(line) for line in kb_file.read_text().splitlines() if line]
    assert rows[0]["hit_count"] == 0
    assert rows[0]["last_hit_at"] is None


@pytest.mark.asyncio
async def test_alert_cmd_does_not_sink_to_kb(tmp_path):
    """Even on retriever-miss / judge-miss path, the command must not
    write a new entry to kb."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed("/alert 全新告警一条")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    run_mock.assert_not_called()
    # No kb file should have been created
    kb_dir = work_dir / "knowledge" / "alerts"
    assert not kb_dir.exists() or not any(kb_dir.glob("*.jsonl"))


@pytest.mark.asyncio
async def test_alert_cmd_judge_exception_replies_debug_not_crash(tmp_path):
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    kb = alert_resolver.make_kb(str(work_dir))
    kb.add(
        chat_id="oc_alert_target", alert_text="something",
        conclusion="resolution conclusion long enough to pass filter",
        source_message_id="om_seed",
    )
    ch = _channel()
    parsed = _parsed("/alert 任意告警")
    with patch("agent_runtime.scheduler.alert_judge.judge",
               AsyncMock(side_effect=RuntimeError("boom"))):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    ch.reply.assert_called_once()
    text = ch.reply.call_args[0][1]
    assert "🧪" in text
    assert "judge" in text.lower() or "异常" in text
