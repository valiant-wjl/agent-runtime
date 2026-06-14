"""Tests for stream-card-related Channel methods (M6-T03).

Covers send_card / update_card real lark-cli invocations and the
per-card consecutive-failure streak that triggers StreamCardDegraded.
Mocks asyncio.create_subprocess_exec so we never actually shell out.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu.adapter import Channel, StreamCardDegraded


def _make_channel():
    return Channel({"lark_cli": "lark-cli", "bot_mention_key": "ou_test"})


def _make_parsed():
    return ParsedMsg(
        channel="feishu", message_id="om_q1", thread_root_id="om_q1",
        chat_id="oc_chat1", sender_id="ou_user", sender_name="u",
        text="?", mentions=[], raw_event={},
    )


def _fake_subprocess(returncode=0, stdout_json=None, stderr=b""):
    """Build a mock asyncio subprocess Process. Sync helper — returns the
    mock directly so it can be used as the return_value of an AsyncMock.
    """
    proc = AsyncMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(
        json.dumps(stdout_json or {}).encode(), stderr,
    ))
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = lambda: None
    return proc


@pytest.mark.asyncio
async def test_send_card_returns_new_msg_id_on_success():
    ch = _make_channel()
    parsed = _make_parsed()
    fake_proc = _fake_subprocess(0, {"message_id": "om_card1"})
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        msg_id = await ch.send_card(parsed, {"header": {"title": {"content": "test"}}})
    assert msg_id == "om_card1"


@pytest.mark.asyncio
async def test_send_card_failure_raises_degraded():
    ch = _make_channel()
    parsed = _make_parsed()
    fake_proc = _fake_subprocess(1, stderr=b"network error")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        with pytest.raises(StreamCardDegraded):
            await ch.send_card(parsed, {"header": {"title": {"content": "test"}}})


@pytest.mark.asyncio
async def test_update_card_uses_api_patch_with_content_body():
    """Regression: lark-cli upstream removed `+messages-patch`. update_card
    must now go through the generic `api PATCH /open-apis/im/v1/messages/<id>`
    route with the card serialized into a JSON-encoded `content` body.
    Production log evidence (2026-05-*): every update_card emitted
    `Error: unknown flag: --message-id` → exit 1 → cards stuck at progress.
    """
    ch = _make_channel()
    captured: dict = {}

    async def capture(*args, **kwargs):
        captured["args"] = args
        return _fake_subprocess(0, {"message_id": "om_card1"})

    card = {"header": {"title": {"content": "final"}}, "elements": []}
    with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=capture)):
        await ch.update_card("om_card1", card)
    args = captured["args"]
    assert args[0] == "lark-cli"
    assert args[1] == "api"
    assert args[2] == "PATCH"
    assert args[3] == "/open-apis/im/v1/messages/om_card1"
    # The card itself is a JSON string under `content` (Feishu API shape),
    # NOT a top-level `card` field.
    assert "--data" in args
    body = json.loads(args[args.index("--data") + 1])
    assert "content" in body
    inner_card = json.loads(body["content"])
    assert inner_card["header"]["title"]["content"] == "final"
    assert "--as" in args and args[args.index("--as") + 1] == "bot"
    # The dead --message-id flag must NOT appear (that's what triggers the
    # 'unknown flag' lark-cli error in production).
    assert "--message-id" not in args


@pytest.mark.asyncio
async def test_update_card_success_returns_true_resets_streak():
    ch = _make_channel()
    # Pre-populate failure streak
    ch._update_fail_streak["om_card1"] = 2
    fake_proc = _fake_subprocess(0, {"message_id": "om_card1"})
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        ok = await ch.update_card("om_card1", {"header": {"title": {"content": "v2"}}})
    assert ok is True
    assert ch._update_fail_streak.get("om_card1", 0) == 0  # reset


@pytest.mark.asyncio
async def test_update_card_failure_increments_streak_returns_false():
    ch = _make_channel()
    fake_proc = _fake_subprocess(1, stderr=b"patch failed")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        ok = await ch.update_card("om_card1", {})
    assert ok is False
    assert ch._update_fail_streak["om_card1"] == 1


@pytest.mark.asyncio
async def test_update_card_three_failures_raises_degraded():
    ch = _make_channel()
    fake_proc = _fake_subprocess(1, stderr=b"patch failed")
    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        # First 2 failures: returns False, no raise
        for i in range(1, 3):
            ok = await ch.update_card("om_card1", {})
            assert ok is False
            assert ch._update_fail_streak["om_card1"] == i
        # 3rd failure: raises StreamCardDegraded
        with pytest.raises(StreamCardDegraded):
            await ch.update_card("om_card1", {})
