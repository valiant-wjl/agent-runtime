"""LarkChannel adapter smoke tests.

Full integration via real lark-cli is L3 manual (see docs/self-test-guide.md).
These tests only verify:
1. Protocol conformance (satisfies ChannelAdapter structurally)
2. Parse delegates to channels.feishu.parser
3. fetch_thread_history M2 stub returns []
4. subscribe() lifecycle: spawn failure, happy path, bad JSON, double-subscribe guard
5. reply() cleans text before sending

Stream-card behavior (send_card / update_card real lark-cli calls,
StreamCardDegraded on failure streak) is covered in test_adapter_card.py.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ChannelAdapter, ParsedMsg
from agent_runtime.channels.feishu.adapter import Channel


def test_channel_has_required_methods():
    """Protocol conformance: all 8 methods + name attribute present."""
    ch = Channel({"lark_cli": "lark-cli"})
    assert ch.name == "feishu"
    for method in ("subscribe", "parse", "reply", "send_card",
                   "update_card", "fetch_thread_history", "close"):
        assert hasattr(ch, method), f"missing method {method!r}"
    # Protocol is not @runtime_checkable (per M2-T01-fix), so no isinstance check


@pytest.mark.asyncio
async def test_parse_delegates_to_parser():
    """Channel.parse returns a ParsedMsg from real fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "sample_event.json"
    event = json.loads(fixture_path.read_text())
    ch = Channel({"lark_cli": "lark-cli"})
    parsed = await ch.parse(event)
    assert parsed is not None
    assert isinstance(parsed, ParsedMsg)
    assert parsed.channel == "feishu"
    assert parsed.message_id


@pytest.mark.asyncio
async def test_fetch_thread_history_returns_empty_list_in_m2():
    """M2 stub behavior: returns [] (v1.x will integrate lark-cli)."""
    ch = Channel({"lark_cli": "lark-cli"})
    history = await ch.fetch_thread_history("om_root_x")
    assert history == []


# ---------------------------------------------------------------------------
# subscribe() lifecycle tests
# ---------------------------------------------------------------------------

class _AsyncLineIter:
    """Mock `proc.stdout` — async iterator over bytes lines."""
    def __init__(self, lines):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


@pytest.mark.asyncio
async def test_subscribe_spawn_failure_yields_nothing():
    """FileNotFoundError on spawn → generator ends with 0 events, no raise."""
    ch = Channel({"lark_cli": "nonexistent-binary"})
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no lark-cli")):
        events = [e async for e in ch.subscribe()]
    assert events == []


@pytest.mark.asyncio
async def test_subscribe_yields_parsed_events():
    """Happy path: 2 NDJSON lines → 2 dict events."""
    lines = [
        b'{"header": {"event_type": "im.message.receive_v1"}, "event": {}}\n',
        b'{"header": {"event_type": "url_verification"}}\n',
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _AsyncLineIter(lines)
    mock_proc.returncode = 0  # 假定已结束
    mock_proc.terminate = lambda: None
    mock_proc.wait = AsyncMock(return_value=0)

    ch = Channel({"lark_cli": "lark-cli"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        events = [e async for e in ch.subscribe()]
    assert len(events) == 2
    assert events[0]["header"]["event_type"] == "im.message.receive_v1"
    assert events[1]["header"]["event_type"] == "url_verification"


@pytest.mark.asyncio
async def test_subscribe_bad_json_line_is_skipped():
    """Malformed JSON line logged+skipped, valid lines still yielded."""
    lines = [
        b'NOT JSON!\n',
        b'{"header": {"event_type": "ok"}}\n',
    ]
    mock_proc = AsyncMock()
    mock_proc.stdout = _AsyncLineIter(lines)
    mock_proc.returncode = 0
    mock_proc.terminate = lambda: None
    mock_proc.wait = AsyncMock(return_value=0)

    ch = Channel({"lark_cli": "lark-cli"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        events = [e async for e in ch.subscribe()]
    assert len(events) == 1
    assert events[0]["header"]["event_type"] == "ok"


@pytest.mark.asyncio
async def test_double_subscribe_raises():
    """Second subscribe() while first still active → RuntimeError."""
    mock_proc = AsyncMock()
    # proc 保持未退出状态 (returncode=None)
    mock_proc.returncode = None
    mock_proc.stdout = _AsyncLineIter([])
    mock_proc.terminate = lambda: None
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.kill = lambda: None

    ch = Channel({"lark_cli": "lark-cli"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        gen1 = ch.subscribe()
        # 启动第一个（消费一下触发 spawn）
        try:
            await gen1.__anext__()
        except StopAsyncIteration:
            pass
        # 手动把 returncode 恢复 None 模拟"仍在运行"
        mock_proc.returncode = None
        ch._proc = mock_proc

        # 第二次 subscribe 应 raise
        gen2 = ch.subscribe()
        with pytest.raises(RuntimeError, match="already subscribed"):
            await gen2.__anext__()

    # cleanup
    await ch.close()


@pytest.mark.asyncio
async def test_close_on_never_subscribed_is_noop():
    """close() before subscribe() — no exception, no subprocess calls."""
    ch = Channel({"lark_cli": "lark-cli"})
    await ch.close()  # no-op, no exception


@pytest.mark.asyncio
async def test_reply_calls_clean_then_send():
    """reply() cleans first, then sends."""
    import agent_runtime.channels.feishu.adapter as adapter_mod

    parsed = ParsedMsg(
        channel="feishu", message_id="m1", thread_root_id="t1",
        chat_id="c1", sender_id="s1", sender_name="s1",
        text="query", mentions=[],
    )

    clean_calls = []
    send_calls = []

    def fake_clean(text):
        clean_calls.append(text)
        return text + "_cleaned"

    async def fake_send(*, lark_cli, message_id, text, **kwargs):
        send_calls.append(text)

    with patch.object(adapter_mod.clean_mod, "clean_for_feishu", side_effect=fake_clean):
        with patch.object(adapter_mod.reply_mod, "send", side_effect=fake_send):
            ch = Channel({"lark_cli": "lark-cli"})
            await ch.reply(parsed, "dirty<br>text")

    assert clean_calls == ["dirty<br>text"]
    assert send_calls == ["dirty<br>text_cleaned"]
