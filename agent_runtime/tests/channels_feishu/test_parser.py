"""Parser tests migrated from feishu-agent-gateway/tests/test_parse_event.py."""

import json
from pathlib import Path

from agent_runtime.channels.feishu import parser


_FIXTURE = Path(__file__).parent / "fixtures" / "sample_event.json"


def test_parse_text_message():
    """Standard lark-cli event yields a valid ParsedMsg."""
    event = json.loads(_FIXTURE.read_text())
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.message_id == "om_x100b53970b92d4b8b2ccdf82de630c5"
    assert parsed.chat_id == "oc_a879bbae4d9941a7852b03302500b12f"
    assert parsed.sender_id == "ou_57573843aaeaa7cb3c3146b58ce8cd62"
    # mention prefix "@_user_1 " should be stripped, leaving "hello"
    assert parsed.text == "hello"
    assert hasattr(parsed, "text")
    assert isinstance(parsed.mentions, list)


def test_parse_non_message_event_returns_none():
    """url_verification 等非 im.message.receive_v1 事件返回 None."""
    event = {"header": {"event_type": "url_verification"}, "challenge": "x"}
    assert parser.parse(event) is None


def test_dedup_rejects_seen_message_id():
    """Same message_id 第二次 parse 被 dedup 拦截返回 None."""
    event = json.loads(_FIXTURE.read_text())
    first = parser.parse(event)
    assert first is not None
    second = parser.parse(event)
    assert second is None


def test_parse_post_message():
    """post 类型消息提取 title + text + at 节点."""
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_post_001",
                "chat_id": "oc_x",
                "message_type": "post",
                "content": json.dumps({
                    "title": "PostTitle",
                    "content": [[
                        {"tag": "text", "text": "Hello "},
                        {"tag": "at", "user_id": "u-a"},
                    ]],
                }),
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    parsed = parser.parse(event)
    assert parsed is not None
    assert "PostTitle" in parsed.text
    assert "Hello" in parsed.text
    assert "@u-a" in parsed.text


def test_parse_multiple_mentions():
    """多个 mention 都收入 mentions 列表."""
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_multi_m",
                "chat_id": "oc_x",
                "message_type": "text",
                "content": json.dumps({"text": "@_user_1 @_user_2 hello"}),
                "mentions": [
                    {"id": {"open_id": "ou_1"}, "key": "@_user_1"},
                    {"id": {"open_id": "ou_2"}, "key": "@_user_2"},
                ],
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    parsed = parser.parse(event)
    assert parsed is not None
    assert set(parsed.mentions) == {"ou_1", "ou_2"}


def test_parse_image_extracts_key_and_placeholder():
    """Image message → text 含占位符 `[图片#1 (img_xxx)]`，image_keys 收 key."""
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_img_001",
                "chat_id": "oc_x",
                "message_type": "image",
                "content": json.dumps({"image_key": "img_xxx"}),
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.image_keys == ["img_xxx"]
    assert parsed.text == "[图片#1 (img_xxx)]"


def test_parse_image_missing_key_yields_empty_keys():
    """image content 缺 image_key（脏数据）：image_keys 空，text 空，不崩."""
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_img_002",
                "chat_id": "oc_x",
                "message_type": "image",
                "content": json.dumps({}),
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.image_keys == []
    assert parsed.text == ""


def test_parse_post_with_inline_images():
    """post 节点里 tag=img 应内联占位符并按顺序收集 image_keys."""
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_post_img",
                "chat_id": "oc_x",
                "message_type": "post",
                "content": json.dumps({
                    "title": "",
                    "content": [[
                        {"tag": "text", "text": "Look "},
                        {"tag": "img", "image_key": "img_aaa"},
                        {"tag": "text", "text": " and "},
                        {"tag": "img", "image_key": "img_bbb"},
                    ]],
                }),
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.image_keys == ["img_aaa", "img_bbb"]
    assert "Look " in parsed.text
    assert "[图片#1 (img_aaa)]" in parsed.text
    assert " and " in parsed.text
    assert "[图片#2 (img_bbb)]" in parsed.text
    # ordering preserved: #1 must appear before #2
    assert parsed.text.index("#1") < parsed.text.index("#2")


def test_parse_text_msg_has_empty_image_keys():
    """普通文本消息 image_keys 默认空列表 (向后兼容)."""
    event = json.loads(_FIXTURE.read_text())
    # bypass dedup by mutating message_id (fixture is reused across tests)
    event["event"]["message"]["message_id"] = "om_text_no_image"
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.image_keys == []


def test_parse_topic_thread_message_extracts_topic_id():
    """话题群 message.thread_id 字段被拷到 ParsedMsg.topic_id."""
    event = {
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "message_id": "om_topic_001",
                "chat_id": "oc_topic_chat",
                "message_type": "text",
                "thread_id": "omt_thread_xyz",
                "content": json.dumps({"text": "讨论一下这个方案"}),
            },
            "sender": {"sender_id": {"open_id": "ou_sender"}},
        },
    }
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.topic_id == "omt_thread_xyz"
    assert parsed.text == "讨论一下这个方案"


def test_parse_non_topic_message_topic_id_none():
    """普通群/p2p 消息无 thread_id 字段时 topic_id=None."""
    event = json.loads(_FIXTURE.read_text())
    event["event"]["message"]["message_id"] = "om_text_no_topic"
    parsed = parser.parse(event)
    assert parsed is not None
    assert parsed.topic_id is None
