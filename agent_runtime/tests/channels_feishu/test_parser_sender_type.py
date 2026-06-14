"""US-004: parser must extract event.sender.sender_type into ParsedMsg.

The alert_resolver gates "is this an alert" on sender_type == 'app'.
Missing field → None (alert_resolver treats as not-alert; safe default).
"""

from __future__ import annotations

from agent_runtime.channels.feishu import parser


def _base_event(sender_obj: dict | None, *, msg_id: str = "om_test") -> dict:
    """Minimal valid feishu event with custom sender block."""
    e: dict = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": msg_id,
                "message_type": "text",
                "content": '{"text":"hello"}',
                "chat_id": "oc_chat",
                "chat_type": "group",
            },
        },
    }
    if sender_obj is not None:
        e["event"]["sender"] = sender_obj
    return e


def test_sender_type_app_preserved():
    event = _base_event(
        {
            "sender_id": {"open_id": "ou_bot_1", "user_id": "bot_user"},
            "sender_type": "app",
        },
        msg_id="om_app_1",
    )
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.sender_type == "app"


def test_sender_type_user_preserved():
    event = _base_event(
        {
            "sender_id": {"open_id": "ou_human_1", "user_id": "alice"},
            "sender_type": "user",
        },
        msg_id="om_user_1",
    )
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.sender_type == "user"


def test_sender_type_missing_field_is_none():
    event = _base_event(
        {"sender_id": {"open_id": "ou_unknown", "user_id": "x"}},
        msg_id="om_no_type",
    )
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.sender_type is None


def test_sender_block_missing_is_none():
    event = _base_event(None, msg_id="om_no_sender")
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.sender_type is None
