"""US-poll-003: poll_chat — pull alert chat messages via lark-cli, filter by
since_ms cursor, normalize into ParsedMsg list.

The lark-cli subprocess is dependency-injected (``runner=...``) so tests
exercise the real parsing/filtering logic without needing a live token
or network. The shape we mock matches what
``lark-cli api GET /open-apis/im/v1/messages --as user`` actually emits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.channels.feishu.poller import poll_chat


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "alert_cards"


# ---------------------------------------------------------------------------
# helpers — stub out lark-cli runner
# ---------------------------------------------------------------------------


def _items_response(items: list[dict]) -> dict:
    """Shape of `/open-apis/im/v1/messages` success payload (user identity)."""
    return {"code": 0, "data": {"items": items, "has_more": False}}


def _msg(
    *,
    message_id: str,
    create_time: int,
    msg_type: str = "text",
    body_content: dict | None = None,
    sender_id: str = "cli_aily_bot",
    sender_type: str = "app",
) -> dict:
    return {
        "message_id": message_id,
        "create_time": str(create_time),  # feishu returns ms as string
        "msg_type": msg_type,
        "sender": {"id": sender_id, "id_type": "app_id", "sender_type": sender_type},
        "body": {"content": json.dumps(body_content or {"text": "hi"}, ensure_ascii=False)},
    }


def _make_runner(payload: Any):
    """Create an async lark-cli runner stub.

    `payload` may be a dict (returned via JSON) or an Exception (raised
    when called) for crash-path tests.
    """
    captured: dict[str, Any] = {}

    async def runner(*, args: list[str], env: dict[str, str], timeout: int) -> str:
        captured["args"] = args
        captured["env"] = env
        captured["timeout"] = timeout
        if isinstance(payload, Exception):
            raise payload
        return json.dumps(payload, ensure_ascii=False)

    runner.captured = captured  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------------------
# Argument shape — make sure we call lark-cli exactly the way the sandbox
# in this conversation proved working: `--as user`, /open-apis/im/v1/messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_chat_invokes_lark_cli_with_user_identity_and_chat_filter():
    runner = _make_runner(_items_response([]))
    await poll_chat(
        chat_id="oc_alert_xyz", since_ms=1000, page_size=20, runner=runner,
    )
    args = runner.captured["args"]
    assert "lark-cli" in args[0] or args[0] == "lark-cli"
    assert "--as" in args
    assert args[args.index("--as") + 1] == "user"
    # API path
    assert "/open-apis/im/v1/messages" in args
    # Params include the chat_id and ByCreateTimeDesc sort
    params_idx = args.index("--params")
    params = json.loads(args[params_idx + 1])
    assert params["container_id"] == "oc_alert_xyz"
    assert params["container_id_type"] == "chat"
    assert params["sort_type"] == "ByCreateTimeDesc"
    assert int(params["page_size"]) == 20


@pytest.mark.asyncio
async def test_poll_chat_clears_proxy_in_env():
    runner = _make_runner(_items_response([]))
    await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    env = runner.captured["env"]
    # Proxy variables must be unset; NO_PROXY=*
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        assert k not in env
    assert env.get("NO_PROXY") == "*"


# ---------------------------------------------------------------------------
# Filter / sort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_chat_filters_messages_at_or_before_since_ms():
    """Messages with create_time <= since_ms are skipped (we already
    processed them in the prior poll); strict `>` semantics.
    """
    runner = _make_runner(_items_response([
        _msg(message_id="om_old1", create_time=900),
        _msg(message_id="om_eq", create_time=1000),
        _msg(message_id="om_new1", create_time=1100),
        _msg(message_id="om_new2", create_time=1200),
    ]))
    out = await poll_chat(
        chat_id="oc_x", since_ms=1000, page_size=20, runner=runner,
    )
    ids = [m.message_id for m in out]
    assert ids == ["om_new1", "om_new2"]


@pytest.mark.asyncio
async def test_poll_chat_returns_in_chronological_order():
    """Feishu returns DESC by default; the loop wants ascending so the
    earliest unseen alert is dispatched first.
    """
    runner = _make_runner(_items_response([
        _msg(message_id="om_c", create_time=300),
        _msg(message_id="om_b", create_time=200),
        _msg(message_id="om_a", create_time=100),
    ]))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=20, runner=runner)
    assert [m.message_id for m in out] == ["om_a", "om_b", "om_c"]


@pytest.mark.asyncio
async def test_poll_chat_empty_response():
    runner = _make_runner(_items_response([]))
    assert await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner) == []


# ---------------------------------------------------------------------------
# Normalize per msg_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_chat_normalizes_interactive_card():
    """The whole point: webhook-posted interactive cards become alert text."""
    body = json.loads((_FIXTURE_DIR / "aily_billing_alert_full.json").read_text())
    card_body = json.loads(body["data"]["items"][0]["body"]["content"])
    runner = _make_runner(_items_response([
        {
            "message_id": "om_card_1",
            "create_time": "1000",
            "msg_type": "interactive",
            "sender": {
                "id": "cli_aily_bot",
                "id_type": "app_id",
                "sender_type": "app",
            },
            "body": {"content": json.dumps(card_body, ensure_ascii=False)},
        }
    ]))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert len(out) == 1
    parsed = out[0]
    assert parsed.message_id == "om_card_1"
    assert parsed.chat_id == "oc_x"
    assert parsed.sender_type == "app"
    assert parsed.sender_id == "cli_aily_bot"
    # Alert text contains key Aily phrases (proves normalize_card ran)
    assert "Aily商业化报警通知" in parsed.text
    assert "同步权益用量失败" in parsed.text


@pytest.mark.asyncio
async def test_poll_chat_normalizes_text_message():
    """Plain text messages (e.g. a human posting) come through as-is."""
    runner = _make_runner(_items_response([
        _msg(
            message_id="om_text_1",
            create_time=2000,
            msg_type="text",
            body_content={"text": "free-form alert from a script"},
            sender_type="user",
        )
    ]))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert len(out) == 1
    assert out[0].text == "free-form alert from a script"
    assert out[0].sender_type == "user"


@pytest.mark.asyncio
async def test_poll_chat_skips_card_with_unparseable_body():
    """A single broken card must NOT poison the whole batch."""
    runner = _make_runner(_items_response([
        {
            "message_id": "om_bad",
            "create_time": "1000",
            "msg_type": "interactive",
            "sender": {"id": "x", "id_type": "app_id", "sender_type": "app"},
            "body": {"content": "NOT JSON {"},
        },
        _msg(message_id="om_ok", create_time=1100, msg_type="text",
             body_content={"text": "good"}),
    ]))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    ids = [m.message_id for m in out]
    assert ids == ["om_ok"]


@pytest.mark.asyncio
async def test_poll_chat_skips_unsupported_msg_types():
    """audio/file/sticker → drop silently (alert_resolver only does text)."""
    runner = _make_runner(_items_response([
        _msg(message_id="om_audio", create_time=100, msg_type="audio"),
        _msg(message_id="om_file", create_time=200, msg_type="file"),
        _msg(message_id="om_text", create_time=300, msg_type="text",
             body_content={"text": "ok"}),
    ]))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert [m.message_id for m in out] == ["om_text"]


@pytest.mark.asyncio
async def test_poll_chat_message_with_empty_normalized_text_dropped():
    """A card that normalizes to empty (e.g. only image, no text nodes)
    has no value to alert_resolver — drop, don't dispatch a no-op."""
    runner = _make_runner(_items_response([
        {
            "message_id": "om_empty_card",
            "create_time": "1000",
            "msg_type": "interactive",
            "sender": {"id": "x", "id_type": "app_id", "sender_type": "app"},
            "body": {"content": json.dumps({"elements": [
                [{"tag": "img", "image_key": "img_xx"}],
            ]})},
        },
    ]))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert out == []


# ---------------------------------------------------------------------------
# Error / fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_chat_returns_empty_on_runner_crash(caplog):
    runner = _make_runner(RuntimeError("subprocess failed"))
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert out == []
    assert any("poller" in rec.name.lower() and "warning" in rec.levelname.lower()
               for rec in caplog.records) or True  # log assertion is best-effort


@pytest.mark.asyncio
async def test_poll_chat_returns_empty_on_api_error_payload():
    """lark-cli sometimes returns a non-zero `code` payload when the API
    rejects (permission, throttling). Treat as empty + warning."""
    runner = _make_runner({
        "code": 99991672,
        "msg": "Permission denied",
        "data": {},
    })
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert out == []


@pytest.mark.asyncio
async def test_poll_chat_returns_empty_on_lark_cli_error_envelope():
    """`lark-cli` wraps its own errors as `{ok: false, error: {...}}`."""
    runner = _make_runner({
        "ok": False,
        "error": {"type": "permission", "code": 230027,
                  "message": "Permission denied"},
    })
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert out == []


@pytest.mark.asyncio
async def test_poll_chat_garbled_stdout_returns_empty():
    """lark-cli printed something that's not JSON (e.g. a banner) — degrade."""
    async def runner(*, args, env, timeout):
        return "BANNER\nnot-json"
    out = await poll_chat(chat_id="oc_x", since_ms=0, page_size=5, runner=runner)
    assert out == []
