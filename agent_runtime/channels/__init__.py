"""Channel adapter contract for digital-agent.

All channel implementations must satisfy the ChannelAdapter Protocol defined here.
ParsedMsg is the canonical message representation passed between channels and the runtime.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Protocol


@dataclass
class ParsedMsg:
    """Canonical message representation, channel-agnostic."""

    channel: str
    message_id: str
    thread_root_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    mentions: list[str]
    reply_target_msg_id: str | None = None
    raw_event: dict | None = None  # 原始 event 字典，parser 可存完整结构供需要富文本的下游查用
    # IM platform conversation kind. Feishu uses "p2p" (1:1 DM) and "group".
    # routing uses this so single-project deployments auto-route p2p messages
    # without requiring mention or keyword (1:1 chat = unambiguous intent).
    # None means the channel adapter didn't populate it; routing must NOT
    # assume p2p in that case (legacy callers, mocks).
    chat_type: str | None = None
    # 用户消息中携带的图片 file_key 列表，按出现顺序。
    # text 字段会包含 `[图片#N (img_xxx)]` 占位符，N 与 image_keys 索引一一对应。
    # scheduler 在 read 阶段前据此调 channel.download_image() 落盘并塞 prompt。
    image_keys: list[str] = field(default_factory=list)
    # 话题群（feishu chat_mode="thread"）的话题标识，None 表示非话题群消息。
    # 用于按话题隔离 session / approval / 历史拉取。
    topic_id: str | None = None
    # 飞书 event.sender.sender_type："app"=机器人/webhook，"user"=自然人。
    # alert_resolver 用此字段判定告警群里哪些消息算告警（仅 bot 发的算）。
    # 旧 fixture 不传 → None，alert_resolver 不会把 None 视为告警。
    sender_type: str | None = None


class ChannelAdapter(Protocol):
    """Protocol every channel adapter must implement.

    Adapters are responsible for:
    - subscribing to incoming events from their platform
    - parsing raw events into ParsedMsg
    - sending replies, cards, and updates back to the platform
    - fetching thread history for context
    """

    name: str

    def __init__(self, config: dict) -> None: ...

    def subscribe(self) -> AsyncIterator[dict]:
        """Yield raw events from the IM platform. Implement as async generator."""
        ...

    async def parse(self, event: dict) -> ParsedMsg | None: ...

    async def reply(self, parsed: ParsedMsg, text: str) -> None: ...

    async def send_card(self, parsed: ParsedMsg, card: dict) -> str: ...

    async def update_card(self, card_msg_id: str, card: dict) -> bool: ...

    async def fetch_thread_history(self, root_id: str) -> list[str]: ...

    async def download_image(
        self,
        *,
        message_id: str,
        image_key: str,
        dest_dir: Path,
        max_bytes: int = 10_000_000,
    ) -> Path:
        """Download a platform image to ``dest_dir`` and return its absolute Path.

        Implementations raise channel-specific exceptions on failure (e.g.,
        ``ImageDownloadFailed`` / ``ImageTooLarge`` from feishu adapter); the
        scheduler catches broadly and degrades the prompt rather than aborting.
        Channels without image support should raise ``NotImplementedError``.
        """
        ...

    async def fetch_topic_history(
        self, topic_id: str, limit: int = 20,
    ) -> list[str]:
        """Return up to ``limit`` recent messages in a topic, asc chronological.

        Format: ``"<sender>: <text>"`` per message. Implementations MUST
        return ``[]`` (not raise) on any subprocess / network failure —
        topic history is non-critical context and must not block the read
        flow. Channels without topic support return ``[]``.
        """
        ...

    async def close(self) -> None: ...
