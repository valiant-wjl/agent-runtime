"""Feishu interactive card builders + throttler for streaming Claude output.

Cards transition through 3 states during a single conversation:
  1. initial  — sent right when the question arrives ("🔄 分析中...")
  2. progress — updated periodically as Claude calls tools / streams text
  3. final    — sent when Claude finishes ("✅ 完成 · 用时 Xs · N tools")

Throttler decides WHEN to emit a progress update — based on min interval
between updates (default 1000ms) and tool-call count (default 3 pending tools).

Designed against docs/samples/stream-json-sample.md event reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

# Size caps to prevent feishu interactive card oversize rejection.
# Feishu has practical render limits around 5-10 KB per lark_md block.
MAX_ANSWER_CHARS = 4000          # final card body cap
MAX_EVENTS_SHOWN = 20            # progress card: keep last N events
MAX_SUMMARY_CHARS = 80           # progress card: per-event input_summary cap

# Allowed feishu interactive card header templates.
_ALLOWED_TEMPLATES = frozenset(
    {
        "blue",
        "green",
        "orange",
        "red",
        "grey",
        "indigo",
        "purple",
        "wathet",
        "turquoise",
        "carmine",
    }
)


@dataclass
class ToolUse:
    """One observed tool_use entry for the progress card."""

    name: str  # e.g. "Glob", "Read", "Bash"
    input_summary: str = ""  # short string summary of the tool input


def build_initial_card(question: str, start_time: float) -> dict:
    """Initial 'analyzing' card sent when question arrives.

    ``start_time`` is a monotonic timestamp (seconds), recorded by the caller
    so subsequent progress/final cards can compute elapsed time. It is not
    rendered in the initial card itself.
    """
    truncated_q = question if len(question) <= 100 else question[:100] + "..."
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔄 分析中..."},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**问题**: {truncated_q}"},
            },
        ],
    }


def build_progress_card(events: Sequence[ToolUse], elapsed_s: float) -> dict:
    """Progress card showing accumulated tool calls + elapsed time.

    Caps applied to keep card body within feishu render limits:
      - shows at most ``MAX_EVENTS_SHOWN`` most-recent events
      - truncates each ``input_summary`` to ``MAX_SUMMARY_CHARS``
    """
    total = len(events)
    if not events:
        tool_block = "_尚未调用工具_"
    else:
        shown = list(events)[-MAX_EVENTS_SHOWN:]
        omitted = total - len(shown)
        lines = []
        if omitted > 0:
            lines.append(f"_(... {omitted} earlier omitted)_")
        for e in shown:
            line = f"- `{e.name}`"
            if e.input_summary:
                summary = e.input_summary
                if len(summary) > MAX_SUMMARY_CHARS:
                    summary = summary[: MAX_SUMMARY_CHARS - 1] + "…"
                line += f" {summary}"
            lines.append(line)
        tool_block = "\n".join(lines)

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": f"🔄 分析中... · {int(elapsed_s)}s",
            },
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "**已调用工具**:"}},
            {"tag": "div", "text": {"tag": "lark_md", "content": tool_block}},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": f"工具数: {total}"}
                ],
            },
        ],
    }


def build_final_card(answer: str, stats: dict) -> dict:
    """Final card with full answer + tool count and elapsed seconds.

    ``stats`` keys:
      - ``elapsed_s``: int/float, total elapsed seconds
      - ``tool_count``: int, total tool calls observed
      - ``template``: optional header template (default "green"); use "red"
        for error finals.
    """
    elapsed_s = stats.get("elapsed_s", 0)
    tool_count = stats.get("tool_count", 0)
    template = stats.get("template", "green")
    if template not in _ALLOWED_TEMPLATES:
        template = "green"
    title_emoji = "✅" if template == "green" else "⚠️"
    title = f"{title_emoji} 完成 · 用时 {int(elapsed_s)}s · {tool_count} tools"
    if len(answer) > MAX_ANSWER_CHARS:
        answer = (
            answer[: MAX_ANSWER_CHARS - 1] + "…\n\n_(answer truncated; see full reply in thread)_"
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": answer}},
        ],
    }


class Throttler:
    """Decide when to emit a progress card update.

    Emits when EITHER:
      - >= ``min_ms`` have passed since last emit (time-based pulse), OR
      - ``pending_tool_count`` >= ``max_calls`` (work-based pulse)

    Uses monotonic time (seconds) supplied by the caller; the caller MUST
    invoke :py:meth:`mark_emitted` after a successful update_card. Until
    mark_emitted is called, ``should_emit`` keeps returning True (so a
    failed update_card naturally retries on the next observation; combine
    with adapter-level streak counter to break runaway loops).

    Mixed-unit warning: ``min_ms`` is milliseconds while ``now`` is seconds;
    both are kept by name for parity with the spec text "throttle_ms: 1000".
    """

    def __init__(self, min_ms: int = 1000, max_calls: int = 3) -> None:
        self.min_ms = min_ms
        self.max_calls = max_calls
        self._last_emit_at: float | None = None  # monotonic seconds

    def should_emit(self, now: float, pending_tool_count: int) -> bool:
        """Return True if caller should call update_card now.

        ``pending_tool_count`` is the number of tool_use blocks accumulated
        since the last successful ``mark_emitted`` (i.e. delta, not
        in-flight). Caller is responsible for resetting its own counter
        after a successful emit.
        """
        if self._last_emit_at is None:
            # First call: always emit
            return True
        if pending_tool_count >= self.max_calls:
            return True
        elapsed_ms = (now - self._last_emit_at) * 1000
        if elapsed_ms >= self.min_ms:
            return True
        return False

    def mark_emitted(self, now: float) -> None:
        self._last_emit_at = now
