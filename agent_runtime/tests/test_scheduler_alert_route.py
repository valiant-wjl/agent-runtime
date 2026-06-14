"""US-007: scheduler routes alert messages through alert_resolver.

Cases:
  1. alert_cfg=None / disabled  → normal path (claude_proc.run called)
  2. non-alert chat              → normal path
  3. alert chat + sender=user    → normal path
  4. alert chat + sender=app + try_handle_alert_hit returns True
                                 → short-circuit; reply via resolver,
                                   claude_proc NOT called, sink NOT called
  5. alert chat + sender=app + try_handle_alert_hit returns False
                                 → claude_proc called, sink called once
                                   with (parsed, final_text)
  6. alert chat + sender=app + miss + APPROVAL_REQUIRED in draft
                                 → claude_proc called, sink NOT called
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import concurrency, scheduler, session
from agent_runtime.claude_proc import RunResult


_ALERT_CFG = {
    "enabled": True,
    "ttl_days": 14,
    "retriever": "keyword",
    "top_k": 3,
    "judge_timeout": 60,
    "judge_model": "haiku",
    "alert_chats": [{"chat_id": "oc_alert", "project": "p"}],
    "sweep": {"enabled": False},
}

_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


def _parsed(*, chat_id="oc_alert", sender_type="app", text="boom rds timeout") -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id=f"om_{chat_id}_{sender_type}",
        thread_root_id="t_x",
        chat_id=chat_id,
        sender_id="ou_bot" if sender_type == "app" else "ou_alice",
        sender_name="bot" if sender_type == "app" else "alice",
        text=text,
        mentions=[],
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
        "display_name": "p",
        "model": "opus",
        "admin_users": ["ou_admin"],
        "approval_timeout": 1800,
        "read_phase": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "disallowed_bash_patterns": [],
        },
        "write_phase": {"timeout": 600},
        "supported_msg_types": ["text", "post"],
        "unsupported_msg_reply": "暂不支持",
    }


@pytest.fixture(autouse=True)
def _scheduler_init():
    concurrency.init_global(10)
    yield


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_cfg_none_falls_through_to_normal_path(tmp_path):
    """No alert_cfg → resolver branch skipped; normal claude path runs."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    fake = RunResult(text="answer", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit", AsyncMock()) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep", AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
        )

    try_hit.assert_not_called()
    run_mock.assert_called_once()
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_alert_cfg_disabled_falls_through(tmp_path):
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    disabled_cfg = {**_ALERT_CFG, "enabled": False}
    fake = RunResult(text="answer", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit", AsyncMock()) as try_hit:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=disabled_cfg,
        )
    try_hit.assert_not_called()
    run_mock.assert_called_once()


@pytest.mark.asyncio
async def test_non_alert_chat_falls_through(tmp_path):
    """chat_id not in alert_chats → resolver short-circuits at is_alert_message."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed(chat_id="oc_random")
    fake = RunResult(text="answer", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit", AsyncMock()) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep", AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_not_called()
    run_mock.assert_called_once()
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_alert_chat_human_sender_falls_through(tmp_path):
    """Human in alert chat → not an alert; normal path."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed(sender_type="user")
    fake = RunResult(text="answer", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit", AsyncMock()) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep", AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_not_called()
    run_mock.assert_called_once()
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_alert_chat_bot_sender_hit_short_circuits(tmp_path):
    """try_handle_alert_hit=True ⇒ no claude, no sink, no session put."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()  # alert chat, app sender

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit",
               AsyncMock(return_value=True)) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep", AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_called_once()
    run_mock.assert_not_called()
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_alert_chat_bot_sender_miss_runs_claude_and_sinks(tmp_path):
    """try_handle_alert_hit=False ⇒ deep path runs, sink called with final_text."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    fake = RunResult(text="深度排查结论：重启 X-prod-002", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run",
               AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit",
               AsyncMock(return_value=False)) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep",
               AsyncMock(return_value=None)) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_called_once()
    # claude_proc.run may be invoked once (read) plus extra verifier passes
    # depending on features_cfg defaults; the contract is "deep path ran",
    # not an exact call count.
    assert run_mock.called
    sink.assert_called_once()
    # sink called with parsed + final_text
    sink_args, sink_kwargs = sink.call_args
    assert sink_args[0] is parsed
    assert "重启 X-prod-002" in sink_args[1]


@pytest.mark.asyncio
async def test_alert_chat_miss_with_approval_required_does_not_sink(tmp_path):
    """If draft contains [APPROVAL_REQUIRED], conclusion isn't final — skip sink."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    approval_text = (
        "我建议执行重启操作。\n\n"
        "[APPROVAL_REQUIRED]\n"
        "操作: restart pod X\n"
        "原因: rds timeout\n"
        "影响: 30s downtime\n"
        "回滚: kubectl rollout undo\n"
        "[/APPROVAL_REQUIRED]\n"
    )
    fake = RunResult(text=approval_text, session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run",
               AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit",
               AsyncMock(return_value=False)) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep", AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_called_once()
    run_mock.assert_called_once()
    # APPROVAL_REQUIRED draft → no sink (conclusion not yet stable).
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_alert_chat_miss_with_timed_out_run_does_not_sink(tmp_path):
    """Timed-out claude run is not a stable conclusion — skip sink."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    fake = RunResult(text="⚠️ 分析超时", session_id="s-1", exit_code=-1, timed_out=True)

    with patch("agent_runtime.scheduler.claude_proc.run",
               AsyncMock(return_value=fake)), \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit",
               AsyncMock(return_value=False)), \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep", AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )
    sink.assert_not_called()


@pytest.mark.asyncio
async def test_alert_chat_interactive_card_miss_bypasses_supported_check(tmp_path):
    """Regression: a polled Aily card has raw msg_type=interactive (not in
    project.supported_msg_types). Miss path must NOT trigger the user-msg
    whitelist reject — alert turns are an independent route."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"; work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    # Override raw_event to simulate an interactive card from the poller
    parsed.raw_event = {"event": {"message": {"message_type": "interactive"}}}
    fake = RunResult(text="结论", session_id="s-1", exit_code=0)

    project_cfg = _project_cfg(work_dir)
    # supported_msg_types deliberately tight — does NOT include "interactive"
    project_cfg["supported_msg_types"] = ["text", "post"]

    with patch("agent_runtime.scheduler.claude_proc.run",
               AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit",
               AsyncMock(return_value=False)) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep",
               AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", project_cfg, _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_called_once()
    # The reject path must NOT have fired — channel.reply may still be
    # called for the final answer, but never with the unsupported text.
    for call in ch.reply.call_args_list:
        args = call.args if call.args else (None, call.kwargs.get("text"))
        if len(args) >= 2:
            assert "暂不支持" not in args[1]
    # Deep path actually ran
    assert run_mock.called
    sink.assert_called_once()


@pytest.mark.asyncio
async def test_alert_resolver_crash_falls_through_to_normal_path(tmp_path):
    """If resolver raises, scheduler logs + continues with normal path."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()
    parsed = _parsed()
    fake = RunResult(text="ok", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run",
               AsyncMock(return_value=fake)) as run_mock, \
         patch("agent_runtime.scheduler.alert_resolver.try_handle_alert_hit",
               AsyncMock(side_effect=RuntimeError("boom"))) as try_hit, \
         patch("agent_runtime.scheduler.alert_resolver.sink_after_deep",
               AsyncMock()) as sink:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(work_dir), _RUNTIME_CFG,
            alert_cfg=_ALERT_CFG,
        )

    try_hit.assert_called_once()
    run_mock.assert_called_once()
    # Resolver crashed but flow continued; sink should still happen since
    # the message *was* an alert turn (just resolver couldn't classify).
    sink.assert_called_once()
