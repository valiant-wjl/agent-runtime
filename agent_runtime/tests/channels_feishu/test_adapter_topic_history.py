"""Tests for LarkChannel.fetch_topic_history — lark-cli thread messages list wrapper."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels.feishu.adapter import Channel


def _make_channel():
    return Channel({"lark_cli": "lark-cli"})


def _fake_proc(returncode=0, stdout=b"", stderr=b""):
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = lambda: None
    proc.kill = lambda: None
    return proc


# Realistic lark-cli wrapped response shape: {ok, data: {items, has_more}}
def _wrap(items, has_more=False):
    return json.dumps({
        "ok": True,
        "data": {"items": items, "has_more": has_more},
    }).encode()


@pytest.mark.asyncio
async def test_fetch_topic_history_parses_text_and_post():
    ch = _make_channel()
    items = [
        {
            "message_id": "om_1",
            "msg_type": "text",
            "sender": {"id": "ou_alice"},
            "body": {"content": json.dumps({"text": "hello"})},
        },
        {
            "message_id": "om_2",
            "msg_type": "post",
            "sender": {"id": "ou_bob"},
            "body": {"content": json.dumps({
                "title": "Plan",
                "content": [[{"tag": "text", "text": "step one"}]],
            })},
        },
    ]
    fake = _fake_proc(0, stdout=_wrap(items))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        history = await ch.fetch_topic_history("omt_xyz", limit=20)
    assert history == [
        "ou_alice: hello",
        "ou_bob: Plan step one",
    ]


@pytest.mark.asyncio
async def test_fetch_topic_history_passes_correct_args():
    ch = _make_channel()
    captured = {}

    async def capture(*args, **kwargs):
        captured["args"] = args
        return _fake_proc(0, stdout=_wrap([]))

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=capture)):
        await ch.fetch_topic_history("omt_thread1", limit=15)

    args = captured["args"]
    assert args[0] == "lark-cli"
    assert "im" in args
    assert "+threads-messages-list" in args
    assert "--thread" in args and args[args.index("--thread") + 1] == "omt_thread1"
    assert "--sort" in args and args[args.index("--sort") + 1] == "asc"
    assert "--page-size" in args and args[args.index("--page-size") + 1] == "15"
    # Run as user: threads-messages-list requires im:message.group_msg:get_as_user +
    # im:message.p2p_msg:get_as_user, which lark-cli's user OAuth token carries via
    # device flow. --as bot returns 230027 unless the bot app has equivalent
    # admin-console scopes + a published version (heavyweight). See adapter for detail.
    assert "--as" in args and args[args.index("--as") + 1] == "user"


@pytest.mark.asyncio
async def test_fetch_topic_history_empty_thread_returns_empty():
    ch = _make_channel()
    fake = _fake_proc(0, stdout=_wrap([]))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        history = await ch.fetch_topic_history("omt_empty")
    assert history == []


@pytest.mark.asyncio
async def test_fetch_topic_history_subprocess_error_returns_empty():
    ch = _make_channel()
    fake = _fake_proc(1, stderr=b"network down")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        history = await ch.fetch_topic_history("omt_xyz")
    assert history == []


@pytest.mark.asyncio
async def test_fetch_topic_history_bad_json_returns_empty():
    ch = _make_channel()
    fake = _fake_proc(0, stdout=b"not json garbage <<<")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        history = await ch.fetch_topic_history("omt_xyz")
    assert history == []


@pytest.mark.asyncio
async def test_fetch_topic_history_timeout_returns_empty():
    ch = _make_channel()

    async def hang_spawn(*args, **kwargs):
        proc = AsyncMock()
        proc.returncode = None

        async def never(*a, **k):
            await asyncio.sleep(10)
            return (b"", b"")

        proc.communicate = AsyncMock(side_effect=never)
        proc.wait = AsyncMock(return_value=0)
        proc.terminate = lambda: None
        proc.kill = lambda: None
        return proc

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=hang_spawn)):
        # tighten timeout via low value through monkeypatch on adapter constant
        from agent_runtime.channels.feishu import adapter as mod
        original = mod._TOPIC_HISTORY_TIMEOUT
        mod._TOPIC_HISTORY_TIMEOUT = 0.05
        try:
            history = await ch.fetch_topic_history("omt_slow")
        finally:
            mod._TOPIC_HISTORY_TIMEOUT = original
    assert history == []


@pytest.mark.asyncio
async def test_fetch_topic_history_non_text_msg_renders_placeholder():
    """Image / file / sticker messages render `<sender>: [<msg_type>]`."""
    ch = _make_channel()
    items = [
        {
            "message_id": "om_3",
            "msg_type": "image",
            "sender": {"id": "ou_carol"},
            "body": {"content": json.dumps({"image_key": "img_xx"})},
        },
    ]
    fake = _fake_proc(0, stdout=_wrap(items))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        history = await ch.fetch_topic_history("omt_xyz")
    assert history == ["ou_carol: [image]"]


@pytest.mark.asyncio
async def test_fetch_topic_history_parses_interactive_card_legacy_schema():
    """Alarm-style cards use {title, elements: [[{tag,text}]]} — same shape
    as `post` but under `elements`. Verifies the alarm card body is
    extracted instead of returning empty (which would render as
    `[interactive]` and starve Claude of context)."""
    ch = _make_channel()
    card_content = {
        "title": "Aily商业化报警通知",
        "elements": [
            [
                {"tag": "text", "text": "问题场景："},
                {"tag": "text", "text": "\n    权益用量变更"},
            ],
            [{"tag": "text", "text": "错误描述：向AI计费中台上报权益用量失败"}],
        ],
    }
    items = [{
        "message_id": "om_alarm",
        "msg_type": "interactive",
        "sender": {"id": "cli_alarm_bot"},
        "body": {"content": json.dumps(card_content)},
    }]
    fake = _fake_proc(0, stdout=_wrap(items))
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        history = await ch.fetch_topic_history("omt_xyz")
    assert len(history) == 1
    line = history[0]
    assert "Aily商业化报警通知" in line
    assert "权益用量变更" in line
    assert "错误描述" in line


@pytest.mark.asyncio
async def test_fetch_message_text_happy_path_renders_card():
    """fetch_message_text resolves a single message into a history-row
    string and decodes interactive (card) content. Used to prepend the
    thread root anchor that threads-messages-list omits."""
    ch = _make_channel()
    payload = json.dumps({
        "code": 0,
        "data": {"items": [{
            "message_id": "om_root",
            "msg_type": "interactive",
            "sender": {"id": "cli_alarm_bot"},
            "body": {"content": json.dumps({
                "title": "Alarm",
                "elements": [[{"tag": "text", "text": "boom"}]],
            })},
        }]},
    }).encode()
    fake = _fake_proc(0, stdout=payload)

    captured: dict = {}

    async def capture(*args, **kwargs):
        captured["args"] = args
        return fake

    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=capture)):
        line = await ch.fetch_message_text("om_root")

    assert line == "cli_alarm_bot: Alarm boom"
    args = captured["args"]
    assert "GET" in args
    assert "/open-apis/im/v1/messages/om_root" in args
    # threads-messages-list bug history (commit d584d75 vs 1def8ea) means
    # `--as user` is the right identity — bot scope rarely covers history reads.
    assert "--as" in args and args[args.index("--as") + 1] == "user"


@pytest.mark.asyncio
async def test_fetch_message_text_failure_returns_none():
    """Non-zero exit / spawn errors must yield None so the caller can keep
    going without blowing up the read flow."""
    ch = _make_channel()
    fake = _fake_proc(1, stderr=b"boom")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake)):
        assert await ch.fetch_message_text("om_root") is None
    fake_empty = _fake_proc(0, stdout=b'{"code":0,"data":{"items":[]}}')
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_empty)):
        assert await ch.fetch_message_text("om_root") is None
