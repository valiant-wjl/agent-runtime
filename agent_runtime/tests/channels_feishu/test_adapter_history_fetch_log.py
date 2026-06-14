"""US-002 (observability): fetch_topic_history emits one structured
history_fetch INFO line per call so production logs show whether the
per-turn lark-cli subprocess took 50ms (warm) or 12s (cold) on each turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels.feishu.adapter import Channel


def _make_channel() -> Channel:
    return Channel({"lark_cli": "lark-cli"})


def _fake_proc(returncode=0, stdout=b"", stderr=b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = lambda: None
    proc.kill = lambda: None
    return proc


def _wrap_messages(items):
    return json.dumps({"ok": True, "data": {"messages": items}}).encode()


def _find_history_fetch(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    matches = [
        r for r in caplog.records
        if r.levelno >= logging.INFO and "history_fetch" in r.getMessage()
    ]
    assert len(matches) == 1, (
        f"expected one history_fetch line, got {len(matches)}:\n"
        + "\n".join(r.getMessage() for r in matches)
    )
    fields: dict[str, str] = {}
    for tok in matches[0].getMessage().split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields


@pytest.mark.asyncio
async def test_history_fetch_log_ok(caplog):
    ch = _make_channel()
    items = [
        {
            "msg_type": "text",
            "sender": {"id": "ou_a"},
            "body": {"content": json.dumps({"text": "hi"})},
        }
    ]
    fake = _fake_proc(0, stdout=_wrap_messages(items))
    caplog.set_level(logging.INFO, logger="agent_runtime.channels.feishu.adapter")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        await ch.fetch_topic_history("omt_x")
    f = _find_history_fetch(caplog)
    assert f.get("outcome") == "ok", f
    assert int(f.get("message_count", "-1")) == 1
    assert int(f.get("elapsed_ms", "-2")) >= 0
    assert f.get("topic_id") == "omt_x"


@pytest.mark.asyncio
async def test_history_fetch_log_empty(caplog):
    ch = _make_channel()
    fake = _fake_proc(0, stdout=_wrap_messages([]))
    caplog.set_level(logging.INFO, logger="agent_runtime.channels.feishu.adapter")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        await ch.fetch_topic_history("omt_empty")
    f = _find_history_fetch(caplog)
    # outcome=empty distinguishes "API returned 0 messages" from "fetch errored"
    assert f.get("outcome") in {"ok", "empty"}, f
    assert int(f.get("message_count", "-1")) == 0


@pytest.mark.asyncio
async def test_history_fetch_log_timeout(caplog):
    ch = _make_channel()

    async def hang_spawn(*a, **kw):
        proc = AsyncMock()
        proc.returncode = None

        async def never(*aa, **kk):
            await asyncio.sleep(10)
            return (b"", b"")

        proc.communicate = AsyncMock(side_effect=never)
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = lambda: None
        proc.kill = lambda: None
        return proc

    caplog.set_level(logging.INFO, logger="agent_runtime.channels.feishu.adapter")
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=hang_spawn)):
        from agent_runtime.channels.feishu import adapter as mod
        orig = mod._TOPIC_HISTORY_TIMEOUT
        mod._TOPIC_HISTORY_TIMEOUT = 0.05
        try:
            await ch.fetch_topic_history("omt_slow")
        finally:
            mod._TOPIC_HISTORY_TIMEOUT = orig
    f = _find_history_fetch(caplog)
    assert f.get("outcome") == "timeout", f
    assert int(f.get("message_count", "-1")) == 0


@pytest.mark.asyncio
async def test_history_fetch_log_exit_nonzero(caplog):
    ch = _make_channel()
    fake = _fake_proc(1, stderr=b"permission denied")
    caplog.set_level(logging.INFO, logger="agent_runtime.channels.feishu.adapter")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        await ch.fetch_topic_history("omt_x")
    f = _find_history_fetch(caplog)
    assert f.get("outcome") == "exit_nonzero", f
