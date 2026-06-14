"""Feishu event NDJSON parser. Extracts ParsedMsg from lark-cli events."""

import json
import logging

from agent_runtime.channels import ParsedMsg
from agent_runtime import dedup

log = logging.getLogger(__name__)


def parse(event: dict, *, dedup_window: int = 300) -> ParsedMsg | None:
    """Return ParsedMsg or None (non-message event / duplicate).

    `image` messages and `post` 内联 img 节点会提取 image_keys 并在 text 内插入
    占位符 `[图片#N (img_xxx)]`，scheduler 在 read 阶段前据此调
    `channel.download_image()` 落盘并塞 prompt 头部。
    其他不支持类型（file/audio/sticker）仍返回 text=""，由 scheduler
    `supported_msg_types` 过滤拒绝。

    Caveat: sender_name 语义见 inline 注释，非 display name。

    Based on legacy feishu-agent-gateway gateway.parse_event().
    """
    header = event.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None

    e = event.get("event") or {}
    msg = e.get("message") or {}
    msg_id = msg.get("message_id")
    if not msg_id:
        return None
    if dedup.is_duplicate(msg_id, window=dedup_window):
        return None

    # content 字段是 JSON 字符串
    content_raw = msg.get("content", "{}")
    try:
        content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
    except json.JSONDecodeError:
        content = {"text": content_raw if isinstance(content_raw, str) else ""}

    msg_type = msg.get("message_type")
    image_keys: list[str] = []
    text = _extract_text(msg_type, content, image_keys)

    # Strip mention key markers (e.g. "@_user_1 hello" → "hello")
    mentions_raw = msg.get("mentions") or []
    for m in mentions_raw:
        key = m.get("key", "")
        if key:
            text = text.replace(key, "", 1)
    text = text.strip()

    sender = e.get("sender") or {}
    sender_id_block = sender.get("sender_id") or {}
    sender_id = sender_id_block.get("open_id", "")
    # NOTE: feishu event payload does NOT carry the sender's display name.
    # `sender_name` here is the user_id (工号 / login id), falling back to
    # open_id if user_id is absent. A true display name requires
    # contact.v3.user.get API lookup (deferred to M3+).
    sender_name = sender_id_block.get("user_id") or sender_id
    # sender_type: "app" = bot/webhook, "user" = 自然人；缺失 → None。
    # alert_resolver 仅把 "app" 视为告警源。
    sender_type = sender.get("sender_type") or None

    mentions: list[str] = []
    for m in mentions_raw:
        mid = (m.get("id") or {}).get("open_id", "")
        if mid:
            mentions.append(mid)

    return ParsedMsg(
        channel="feishu",
        message_id=msg_id,
        thread_root_id=msg.get("root_id") or msg_id,
        chat_id=msg.get("chat_id", ""),
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        mentions=mentions,
        reply_target_msg_id=msg.get("parent_id") or None,
        raw_event=event,
        # Feishu's chat_type is "p2p" (1:1 DM) or "group". Routing uses this
        # so single-project p2p chats auto-route without requiring mention.
        chat_type=msg.get("chat_type"),
        image_keys=image_keys,
        topic_id=msg.get("thread_id") or None,
        sender_type=sender_type,
    )


def _img_placeholder(idx_1based: int, image_key: str) -> str:
    return f"[图片#{idx_1based} ({image_key})]"


def _extract_text(msg_type: str | None, content: dict, image_keys: list[str]) -> str:
    """Extract text from content dict; mutate image_keys with collected keys.

    image / post 中的图片节点 → 占位符 `[图片#N (img_xxx)]` 内联到 text；
    image_keys 按出现顺序累加（N 与索引 0-based + 1 一致）。
    """
    if msg_type == "text":
        return content.get("text", "")
    if msg_type == "image":
        key = content.get("image_key")
        if key:
            image_keys.append(key)
            return _img_placeholder(len(image_keys), key)
        return ""
    if msg_type == "post":
        # post: {"title": str, "content": [[{tag:"text"|"at"|"a"|"img", ...}, ...], ...]}
        title = content.get("title", "")
        chunks: list[str] = []
        for para in content.get("content", []) or []:
            for node in para or []:
                tag = node.get("tag")
                if tag == "text":
                    chunks.append(node.get("text", ""))
                elif tag == "at":
                    chunks.append(f"@{node.get('user_id', '')}")
                elif tag == "a":
                    # href fallback: feishu post 里 <a> 若无 text 只有 href,
                    # 把 URL 当文本插入（保留信息优于丢弃）
                    chunks.append(node.get("text") or node.get("href") or "")
                elif tag == "img":
                    key = node.get("image_key")
                    if key:
                        image_keys.append(key)
                        chunks.append(_img_placeholder(len(image_keys), key))
        return (title + "\n" + "".join(chunks)).strip() if title or chunks else ""
    # 其他类型（file/audio/sticker 等）返回空 text，由 scheduler 过滤
    return content.get("text", "")
