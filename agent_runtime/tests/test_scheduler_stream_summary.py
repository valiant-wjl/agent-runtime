"""US-002 (observability): _run_read_stream emits one structured
stream_summary log line per stream — covers normal, timeout, and
mid-stream StreamCardDegraded paths.

Why this matters: the production symptom "stuck on 🔄 分析中..." can have
two very different root causes (Claude never yields an event vs. Feishu
card-patch lark-cli subprocess hanging) and they're indistinguishable
without first-event-latency + degrade timing in the logs.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu.adapter import StreamCardDegraded
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
def project_cfg(tmp_path: Path):
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
    for ev in events:
        yield ev


def _find_stream_summary(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    """Parse the unique 'stream_summary' log line into a key/value dict."""
    matches = [
        r for r in caplog.records
        if r.levelno >= logging.INFO and "stream_summary" in r.getMessage()
    ]
    assert len(matches) == 1, (
        f"expected exactly one stream_summary line, got {len(matches)}:\n"
        + "\n".join(r.getMessage() for r in matches)
    )
    fields: dict[str, str] = {}
    for tok in matches[0].getMessage().split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields


@pytest.mark.asyncio
async def test_stream_summary_normal_path(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """Happy stream -> stream_summary with sane counts and final_event_seen=true."""
    fake_events = [
        {"type": "stream_event", "event": {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "Hi "},
        }},
        {"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "text_delta", "text": "there"},
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
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )
    f = _find_stream_summary(caplog)
    assert f.get("final_event_seen") == "true", f
    assert int(f.get("text_delta_count", "-1")) == 2
    assert int(f.get("tool_use_count", "-1")) == 1
    assert int(f.get("first_event_ms", "-2")) >= 0
    assert int(f.get("first_text_delta_ms", "-2")) >= 0
    assert int(f.get("last_event_ms", "-2")) >= 0
    assert f.get("card_degraded_mid_stream") == "false"
    assert f.get("timed_out") == "false"
    assert f.get("exit_code") == "0"


@pytest.mark.asyncio
async def test_stream_summary_mid_stream_degrade(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """update_card raises StreamCardDegraded mid-stream -> summary records it."""
    call_count = 0

    async def update_side(card_msg_id, card):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise StreamCardDegraded("3 fails in a row")
        return True

    fake_channel.update_card.side_effect = update_side

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

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )
    f = _find_stream_summary(caplog)
    assert f.get("card_degraded_mid_stream") == "true", f
    # The streak that triggered degrade => at least 1 successful + 1 failed update_card
    assert int(f.get("update_failures", "-1")) >= 1


@pytest.mark.asyncio
async def test_stream_summary_timeout(
    fake_channel, parsed, project_cfg, runtime_cfg, features_cfg, monkeypatch, caplog,
):
    """asyncio.TimeoutError out of run_stream -> stream_summary with
    timed_out=true and final_event_seen=false."""

    async def _timeout_gen(events=None):
        # yield nothing then time out — never produces a result event
        if False:
            yield {}
        raise asyncio.TimeoutError("simulated reply_timeout")

    monkeypatch.setattr(
        "agent_runtime.claude_proc.run_stream",
        lambda **kw: _timeout_gen(),
    )
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    caplog.set_level(logging.INFO, logger="agent_runtime.scheduler")
    await scheduler._handle_message_inner(
        fake_channel, parsed, "billing", project_cfg, runtime_cfg, features_cfg,
    )
    f = _find_stream_summary(caplog)
    assert f.get("timed_out") == "true", f
    assert f.get("final_event_seen") == "false", f
    assert int(f.get("first_event_ms", "-2")) == -1
