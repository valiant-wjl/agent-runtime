"""Tests for scheduler stream card integration (M6-T04).

Four paths:
  1. Happy path: send_card → throttled progress updates → final card.
  2. send_card outright failure → degrade to text reply for the whole turn,
     counter bumped.
  3. update_card raises StreamCardDegraded mid-stream → switch to text
     reply for the rest of the turn, counter bumped.
  4. Pure unit-test of ``_extract_stream_event`` for the four event kinds
     (tool_use, text_delta, result, irrelevant).
"""

from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu.adapter import StreamCardDegraded
from agent_runtime import scheduler, session, stream_card_metrics


@pytest.fixture(autouse=True)
def _configure_session(tmp_path):
    """Each test gets a fresh on-disk session store."""
    session.configure(tmp_path / "sess.json")
    yield


@pytest.fixture
def fake_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.send_card = AsyncMock(return_value="om_card1")
    ch.update_card = AsyncMock(return_value=True)
    ch.reply = AsyncMock(return_value=None)
    return ch


@pytest.fixture
def parsed():
    return ParsedMsg(
        channel="feishu",
        message_id="om_q",
        thread_root_id="om_q",
        chat_id="oc_chat",
        sender_id="ou_user",
        sender_name="u",
        text="?",
        mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


@pytest.fixture
def project_cfg():
    return {
        "work_dir": "/tmp/proj",
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
def runtime_cfg():
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
def features_cfg():
    return {"verifier": {"enabled": False}}


async def _fake_stream(events):
    """Async generator yielding one event per element of ``events``."""
    for ev in events:
        yield ev


@pytest.mark.asyncio
async def test_streaming_path_sends_initial_progress_final(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch, tmp_path,
):
    """Happy path: send_card → throttled progress updates → final card."""
    monkeypatch.chdir(tmp_path)  # isolate stream_card_metrics state file

    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {
                "type": "tool_use", "name": "Glob", "input": {"pattern": "*.py"},
            },
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {
                "type": "tool_use", "name": "Read", "input": {"file_path": "x.py"},
            },
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 2,
            "delta": {"type": "text_delta", "text": "Final answer is 42."},
        }},
        {"type": "result", "subtype": "success", "duration_ms": 1500,
         "total_cost_usd": 0.01, "session_id": "sess-new"},
    ]

    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _fake_stream(fake_events),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )

    fake_channel.send_card.assert_called_once()  # initial card
    # At least the final update_card; possibly one or more progress updates.
    assert fake_channel.update_card.call_count >= 1
    fake_channel.reply.assert_not_called()  # stayed in card mode throughout


@pytest.mark.asyncio
async def test_send_card_failure_degrades_to_text(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch, tmp_path,
):
    """If initial send_card raises StreamCardDegraded, rest goes via text reply."""
    monkeypatch.chdir(tmp_path)
    fake_channel.send_card.side_effect = StreamCardDegraded("network down")

    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "answer"},
        }},
        {"type": "result", "subtype": "success", "session_id": "sess-x"},
    ]
    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _fake_stream(fake_events),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )

    fake_channel.reply.assert_called()  # fallback path used
    # update_card should not be tried after send_card failed
    fake_channel.update_card.assert_not_called()
    assert stream_card_metrics.get_throttled() >= 1


@pytest.mark.asyncio
async def test_update_card_degraded_mid_stream_falls_back(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch, tmp_path,
):
    """If update_card raises StreamCardDegraded mid-stream, switch to text mode."""
    monkeypatch.chdir(tmp_path)

    call_count = 0

    async def update_side_effect(card_msg_id, card):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise StreamCardDegraded("3 fails in a row")
        return True

    fake_channel.update_card.side_effect = update_side_effect

    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": i,
            "content_block": {"type": "tool_use", "name": f"T{i}", "input": {}},
        }}
        for i in range(5)
    ] + [
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 5,
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

    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )

    fake_channel.reply.assert_called()  # final fell back to text
    assert stream_card_metrics.get_throttled() >= 1


def test_extract_stream_event_helpers():
    """Unit-test _extract_stream_event for tool_use/text_delta/result/skip."""
    from agent_runtime.scheduler import _extract_stream_event
    from agent_runtime.channels.feishu.stream_card import ToolUse

    # tool_use
    ev = {"type": "stream_event", "event": {
        "type": "content_block_start", "index": 0,
        "content_block": {"type": "tool_use", "name": "Glob", "input": {"pattern": "x"}},
    }}
    kind, payload = _extract_stream_event(ev)
    assert kind == "tool_use"
    assert isinstance(payload, ToolUse)
    assert payload.name == "Glob"
    assert "x" in payload.input_summary

    # text_delta
    ev = {"type": "stream_event", "event": {
        "type": "content_block_delta",
        "delta": {"type": "text_delta", "text": "hi"},
    }}
    kind, payload = _extract_stream_event(ev)
    assert kind == "text_delta"
    assert payload == "hi"

    # result
    ev = {"type": "result", "subtype": "success", "session_id": "s"}
    kind, payload = _extract_stream_event(ev)
    assert kind == "final"
    assert payload["session_id"] == "s"
    assert payload["is_error"] is False

    # result error
    kind, payload = _extract_stream_event({"type": "result", "subtype": "error_max_turns"})
    assert kind == "final"
    assert payload["is_error"] is True

    # irrelevant: hook event
    assert _extract_stream_event({"type": "system", "subtype": "hook_started"}) is None
    # irrelevant: non-text content_block_delta
    assert _extract_stream_event({
        "type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "input_json_delta", "partial_json": "{"},
        },
    }) is None
