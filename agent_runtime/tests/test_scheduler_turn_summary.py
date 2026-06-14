"""US-001 (observability): scheduler emits one structured turn_summary log
line per turn, regardless of which branch handled the message.

Branches covered:
  - deep:            normal stream path through claude_proc.run_stream
  - alert_hit:       alert_resolver short-circuits before claude_proc
  - unsupported:     msg type not in supported list, early return
  - deep (exception): stream raises -> summary still emitted via finally
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import scheduler, session


@pytest.fixture(autouse=True)
def _configure_session(tmp_path):
    session.configure(tmp_path / "sess.json")
    yield


@pytest.fixture
def fake_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.send_card = AsyncMock(return_value="om_card1")
    ch.update_card = AsyncMock(return_value=True)
    ch.reply = AsyncMock(return_value=None)
    ch.fetch_topic_history = AsyncMock(return_value=[])
    ch.fetch_message_text = AsyncMock(return_value=None)
    return ch


def _parsed(*, text="hi", msg_type="text", topic_id=None) -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id="om_q1",
        thread_root_id="om_root",
        chat_id="oc_chat",
        sender_id="ou_user",
        sender_name="alice",
        text=text,
        mentions=[],
        topic_id=topic_id,
        raw_event={"event": {"message": {"message_type": msg_type}}},
    )


@pytest.fixture
def project_cfg(tmp_path: Path) -> dict:
    return {
        "work_dir": str(tmp_path / "work"),
        "model": "sonnet",
        "read_phase": {
            "disallowed_tools": ["Edit", "Write"],
            "disallowed_bash_patterns": [],
        },
        "supported_msg_types": ["text"],
        "approval_timeout": 1800,
        "admin_users": [],
    }


@pytest.fixture
def runtime_cfg() -> dict:
    return {
        "paths": {"meta_work_dir": "/tmp/meta"},
        "reply_timeout": 30,
        "channels": {
            "feishu": {
                "stream_card": {
                    "enabled": True,
                    "throttle_ms": 100,
                    "throttle_tool_calls": 2,
                }
            }
        },
    }


@pytest.fixture
def features_cfg() -> dict:
    return {"verifier": {"enabled": False}}


async def _fake_stream(events):
    for ev in events:
        yield ev


def _find_summary(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    """Locate the unique 'turn_summary' log line and parse its key=value
    fields into a dict. Raises AssertionError if 0 or >1 lines found."""
    matches = [
        r for r in caplog.records
        if r.levelno >= logging.INFO and "turn_summary" in r.getMessage()
    ]
    assert len(matches) == 1, (
        f"expected exactly one turn_summary line, got {len(matches)}:\n"
        + "\n".join(r.getMessage() for r in matches)
    )
    msg = matches[0].getMessage()
    fields: dict[str, str] = {}
    for tok in msg.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields


@pytest.mark.asyncio
async def test_turn_summary_deep_path(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """Normal stream path -> turn_summary with branch=deep."""
    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "hello"},
        }},
        {"type": "result", "subtype": "success", "session_id": "s1"},
    ]
    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _fake_stream(fake_events),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, _parsed(), "billing", project_cfg, runtime_cfg, features_cfg,
    )
    f = _find_summary(caplog)
    assert f.get("branch") == "deep", f
    assert f.get("exit_code") == "0"
    assert f.get("timed_out") == "false"
    assert f.get("is_alert") == "false"
    assert int(f.get("total_ms", "-2")) >= 0
    assert int(f.get("read_ms", "-2")) >= 0
    assert int(f.get("text_len", "-2")) >= 1
    assert f.get("card_msg_id_set") == "true"


@pytest.mark.asyncio
async def test_turn_summary_unsupported_msg(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """Unsupported msg type returns early; summary emitted with branch=unsupported."""
    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    parsed = _parsed(msg_type="audio")  # not in ["text"] supported list
    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )
    f = _find_summary(caplog)
    assert f.get("branch") == "unsupported", f
    assert f.get("is_alert") == "false"
    assert int(f.get("total_ms", "-2")) >= 0
    # Stages that didn't run emit -1 sentinel.
    assert f.get("read_ms") == "-1"
    assert f.get("history_ms") == "-1"


@pytest.mark.asyncio
async def test_turn_summary_alert_hit(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, tmp_path, caplog,
):
    """alert_resolver short-circuit hit -> branch=alert_hit, no read phase."""
    parsed = ParsedMsg(
        channel="feishu",
        message_id="om_alert_q",
        thread_root_id="om_root",
        chat_id="oc_alert",
        sender_id="ou_bot",
        sender_name="bot",
        text="alert payload here",
        mentions=[],
        sender_type="app",
        raw_event={"event": {"message": {"message_type": "text"}}},
    )
    alert_cfg = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "top_k": 3,
        "judge_timeout": 60,
        "judge_model": "haiku",
        "alert_chats": [{"chat_id": "oc_alert", "project": "billing"}],
        "sweep": {"enabled": False},
    }
    # Force is_alert_message → True and try_handle_alert_hit → True.
    monkeypatch.setattr(
        "agent_runtime.alert_resolver.is_alert_message",
        lambda parsed, cfg: (True, cfg["alert_chats"][0]),
    )
    monkeypatch.setattr(
        "agent_runtime.alert_resolver.try_handle_alert_hit",
        AsyncMock(return_value=True),
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
        alert_cfg=alert_cfg,
    )
    f = _find_summary(caplog)
    assert f.get("branch") == "alert_hit", f
    assert f.get("is_alert") == "true"
    assert f.get("read_ms") == "-1"  # never reached read phase


@pytest.mark.asyncio
async def test_final_update_card_false_falls_back_to_reply(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """US-003 root-cause fix: when the final card-flip update_card returns
    False (single transient failure, not 3-streak), the previous code
    silently left the card stuck at progress state — almost certainly the
    'stuck on 🔄 分析中...' symptom. After fix: channel.reply must be
    called with final_text, and turn_summary.final_card_update_failed=true.
    """
    # In-flight progress updates succeed (return True); FINAL update_card
    # returns False (single transient lark-cli failure, not streak → no raise).
    call_count = 0

    async def update_side(card_msg_id, card):
        nonlocal call_count
        call_count += 1
        # The final-card update is the call carrying a green/red 'template'
        # in stats — i.e. build_final_card output. Earlier progress cards
        # have no header.template == green/red distinction; simpler proxy:
        # let the LAST update return False, all prior return True.
        # We rely on the test stream having only one progress emit before
        # the final, so call_count == 2 = final.
        if call_count >= 2:
            return False
        return True

    fake_channel.update_card.side_effect = update_side

    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "answer text"},
        }},
        {"type": "result", "subtype": "success", "session_id": "s"},
    ]
    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _fake_stream(fake_events),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, _parsed(), "billing", project_cfg, runtime_cfg, features_cfg,
    )

    # Behaviour: text fallback fired
    fake_channel.reply.assert_called()
    # Observability: summary records final_card_update_failed=true
    f = _find_summary(caplog)
    assert f.get("final_card_update_failed") == "true", f
    assert f.get("card_degraded") == "true", f


@pytest.mark.asyncio
async def test_final_update_card_true_no_fallback(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """When the final update_card returns True, no text fallback fires and
    turn_summary.final_card_update_failed=false."""
    fake_channel.update_card.return_value = True

    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "ok"},
        }},
        {"type": "result", "subtype": "success", "session_id": "s"},
    ]
    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _fake_stream(fake_events),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, _parsed(), "billing", project_cfg, runtime_cfg, features_cfg,
    )

    fake_channel.reply.assert_not_called()
    f = _find_summary(caplog)
    assert f.get("final_card_update_failed") == "false", f


@pytest.mark.asyncio
async def test_turn_summary_cancelled_mid_flight(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """When the daemon is shut down mid-turn, asyncio.CancelledError escapes
    _handle_message_inner_impl. Today that leaves branch='unknown' in the
    turn_summary; with US-002 the wrapper tags branch='cancelled' AND
    re-raises so the cancellation still propagates to the asyncio task.

    Reproduce the production case: cancellation inside try_handle_alert_hit
    (the long-running alert_judge claude call) → impl never tagged a branch
    → wrapper must rescue it as 'cancelled'.
    """
    import asyncio

    parsed = ParsedMsg(
        channel="feishu", message_id="om_alert_q", thread_root_id="t_x",
        chat_id="oc_alert", sender_id="ou_bot", sender_name="bot",
        text="alert payload", mentions=[], sender_type="app",
        raw_event={"event": {"message": {"message_type": "text"}}},
    )
    alert_cfg = {
        "enabled": True, "ttl_days": 14, "retriever": "keyword", "top_k": 3,
        "judge_timeout": 60, "judge_model": "haiku",
        "alert_chats": [{"chat_id": "oc_alert", "project": "billing"}],
        "sweep": {"enabled": False},
    }
    monkeypatch.setattr(
        "agent_runtime.alert_resolver.is_alert_message",
        lambda parsed, cfg: (True, cfg["alert_chats"][0]),
    )

    async def _cancel(*_a, **_kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "agent_runtime.alert_resolver.try_handle_alert_hit", _cancel,
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    with pytest.raises(asyncio.CancelledError):
        await scheduler._handle_message_inner(
            fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
            alert_cfg=alert_cfg,
        )
    f = _find_summary(caplog)
    assert f.get("branch") == "cancelled", f
    assert f.get("is_alert") == "true", f


def test_emit_turn_summary_includes_token_usage_columns(caplog):
    """Pre-2026-06-15 Agent SDK billing split: token usage must show up in
    the grep-friendly turn_summary line so we can aggregate per-day spend
    from the daemon log without parsing OTel spans.
    """
    summary = scheduler._TurnSummary(
        msg_id="m1",
        chat_id="c1",
        branch="deep",
        usage_input_tokens=1234,
        usage_output_tokens=567,
        model="claude-sonnet-4-6",
    )
    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    scheduler._emit_turn_summary(summary, total_ms=42)
    matches = [r for r in caplog.records if "turn_summary" in r.getMessage()]
    assert len(matches) == 1
    msg = matches[0].getMessage()
    fields: dict[str, str] = {}
    for tok in msg.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    assert fields.get("tokens_in") == "1234", msg
    assert fields.get("tokens_out") == "567", msg
    assert fields.get("model") == "claude-sonnet-4-6", msg


def test_emit_turn_summary_emits_zero_tokens_when_unavailable(caplog):
    """Branches that never reached claude (unsupported / alert_hit) should
    still emit token columns with 0 / '-' so log-shape stays uniform."""
    summary = scheduler._TurnSummary(msg_id="m2", chat_id="c2", branch="unsupported")
    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    scheduler._emit_turn_summary(summary, total_ms=1)
    msg = [r.getMessage() for r in caplog.records if "turn_summary" in r.getMessage()][0]
    fields = {k: v for tok in msg.split() if "=" in tok for k, v in [tok.split("=", 1)]}
    assert fields.get("tokens_in") == "0"
    assert fields.get("tokens_out") == "0"
    assert fields.get("model") == "-"


@pytest.mark.asyncio
async def test_turn_summary_exception_in_stream(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """If the stream layer raises, the summary line still emits with timed_out
    or non-zero exit code so the stall is still observable."""

    async def _boom(events):
        # Raise *inside* the generator so _run_read_stream's try/except
        # for "Exception" branch is exercised.
        for ev in events:
            yield ev
        raise RuntimeError("simulated stream blowup")

    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _boom([{"type": "result", "subtype": "success", "session_id": "s"}]),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, _parsed(), "billing", project_cfg, runtime_cfg, features_cfg,
    )
    f = _find_summary(caplog)
    assert f.get("branch") == "deep", f
    # Stream exception path sets exit_code=1; check we still got a row.
    assert f.get("exit_code") in {"1", "-1"}
