"""`/help` slash command — single index of every DM-usable command.

Why a dedicated handler (not delegated to claude):
  - Zero claude API cost: command listing is bookkeeping, not reasoning.
  - Deterministic: `/help` always renders the same menu regardless of
    model availability or session state.
  - Discoverability: new users open a DM with the bot, type ``/help``,
    and see the full surface immediately.

The text intentionally enumerates every slash command (including
admin-only ones) so the listing acts as a contract — adding a new
command means updating ``_HELP_TEXT`` here, full stop. Mark admin-only
verbs visibly so non-admins don't waste a round-trip.
"""

from __future__ import annotations

_PREFIX = "/help"


def is_help_command(text: str) -> bool:
    """Return True iff `text` starts (after leading whitespace) with
    exactly `/help` followed by EOS, space, or tab. Avoids false matches
    on `/helper`, `/help2`, or substrings mid-sentence."""
    if not text:
        return False
    stripped = text.lstrip()
    if stripped == _PREFIX:
        return True
    return stripped.startswith(_PREFIX + " ") or stripped.startswith(_PREFIX + "\t")


_HELP_TEXT = (
    "📖 可用命令（在飞书 DM 或群里 @bot 发送）：\n"
    "\n"
    "  /help — 查看本帮助\n"
    "  /lesson <内容> — 记下纠正/学习要点（追加到 knowledge/lessons.md）\n"
    "  /alert <告警原文> — 测试 alert resolver（不入库、不投递）\n"
    "  /agent show — 查看监听总览\n"
    "  /agent alert list — 列出告警群\n"
    "  /agent alert register [--project <name>] — 把当前群注册为告警群（admin）\n"
    "  /agent alert remove <chat_id> — 摘除告警群（admin）\n"
    "\n"
    "普通对话直接发消息即可，不需要前缀。"
)


def render_help() -> str:
    """Return the unified help menu."""
    return _HELP_TEXT
