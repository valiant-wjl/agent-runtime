"""Tests for channels/feishu/stream_card — M6-T02."""

import json
import time

from agent_runtime.channels.feishu.stream_card import (
    MAX_ANSWER_CHARS,
    MAX_EVENTS_SHOWN,
    MAX_SUMMARY_CHARS,
    Throttler,
    ToolUse,
    build_final_card,
    build_initial_card,
    build_progress_card,
)


def test_initial_card_has_analyzing_marker():
    card = build_initial_card("limit 多少", start_time=time.monotonic())
    # Card must round-trip as JSON for feishu API
    serialized = json.dumps(card, ensure_ascii=False)
    # "分析中" is the canonical Chinese marker
    assert "分析中" in serialized


def test_progress_card_accumulates_tools():
    events = [
        ToolUse(name="Glob", input_summary="runtime/*.py"),
        ToolUse(name="Read", input_summary="runtime/scheduler.py"),
        ToolUse(name="Grep", input_summary="rate_limit"),
        ToolUse(name="Read", input_summary="config.yaml"),
        ToolUse(name="Bash", input_summary="ls -la"),
    ]
    card = build_progress_card(events, elapsed_s=12.4)
    serialized = json.dumps(card, ensure_ascii=False)
    # Each tool name + summary should appear
    for e in events:
        assert e.name in serialized
        assert e.input_summary in serialized
    # Tool count is shown (tightened from "5" substring to label match)
    assert "工具数: 5" in serialized


def test_throttler_respects_min_interval():
    t = Throttler(min_ms=1000, max_calls=999)
    now = time.monotonic()
    assert t.should_emit(now, pending_tool_count=0) is True  # first call always
    t.mark_emitted(now)
    # Within 1000 ms, no new tools → False
    assert t.should_emit(now + 0.5, pending_tool_count=0) is False
    # After 1000 ms → True
    assert t.should_emit(now + 1.1, pending_tool_count=0) is True


def test_throttler_emits_on_tool_count():
    t = Throttler(min_ms=10000, max_calls=3)
    now = time.monotonic()
    t.should_emit(now, pending_tool_count=0)
    t.mark_emitted(now)
    # 100 ms later (well under min_ms=10000) but 3 tools pending → True
    assert t.should_emit(now + 0.1, pending_tool_count=3) is True


def test_final_card_includes_stats():
    card = build_final_card(
        answer="The answer is 100 qps",
        stats={"elapsed_s": 28, "tool_count": 4, "template": "green"},
    )
    serialized = json.dumps(card, ensure_ascii=False)
    assert "100 qps" in serialized
    assert "28s" in serialized       # elapsed inside title
    assert "4 tools" in serialized   # tool_count inside title
    assert "完成" in serialized


# ----- Boundary tests added in M6-T02 fix round -----

def test_progress_card_empty_events():
    """Empty events list renders 尚未调用工具 placeholder, count=0."""
    card = build_progress_card([], elapsed_s=0.5)
    serialized = json.dumps(card, ensure_ascii=False)
    assert "尚未调用工具" in serialized
    assert "工具数: 0" in serialized


def test_progress_card_caps_long_event_list():
    """When events exceed MAX_EVENTS_SHOWN, oldest are omitted with marker."""
    events = [ToolUse(name=f"Tool{i}", input_summary=f"arg{i}") for i in range(30)]
    card = build_progress_card(events, elapsed_s=60.0)
    serialized = json.dumps(card, ensure_ascii=False)
    assert "工具数: 30" in serialized           # total still reported truthfully
    assert f"... {30 - MAX_EVENTS_SHOWN} earlier omitted" in serialized
    # Newest events present, oldest absent
    assert "Tool29" in serialized
    assert "Tool0 " not in serialized           # space prevents Tool0 matching Tool29


def test_progress_card_truncates_long_summary():
    """Per-event input_summary trimmed to MAX_SUMMARY_CHARS."""
    long_input = "x" * (MAX_SUMMARY_CHARS + 50)
    card = build_progress_card([ToolUse(name="Bash", input_summary=long_input)], elapsed_s=1.0)
    serialized = json.dumps(card, ensure_ascii=False)
    # Should NOT contain the full long_input verbatim
    assert long_input not in serialized
    # Should contain ellipsis marker
    assert "…" in serialized


def test_final_card_caps_long_answer():
    """Answer over MAX_ANSWER_CHARS is truncated with explanatory suffix."""
    long_answer = "y" * (MAX_ANSWER_CHARS + 1000)
    card = build_final_card(answer=long_answer, stats={"elapsed_s": 5, "tool_count": 1})
    serialized = json.dumps(card, ensure_ascii=False)
    assert long_answer not in serialized
    assert "answer truncated" in serialized


def test_final_card_invalid_template_falls_back_to_green():
    card = build_final_card(answer="ok", stats={"elapsed_s": 1, "tool_count": 0, "template": "hot pink"})
    assert card["header"]["template"] == "green"


def test_throttler_below_max_calls_does_not_emit():
    """Boundary: pending_tool_count BELOW max_calls (and within min_ms) → False."""
    t = Throttler(min_ms=10000, max_calls=3)
    now = time.monotonic()
    t.should_emit(now, pending_tool_count=0)
    t.mark_emitted(now)
    # 100 ms later, only 2 tools pending → False (need >= 3)
    assert t.should_emit(now + 0.1, pending_tool_count=2) is False
