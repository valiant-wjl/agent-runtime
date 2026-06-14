"""Tests for the start_*_span context managers.

Verifies:
- turn span carries chat_id/msg_id/is_alert/branch
- tool span links to parent turn via trace_id + parent_span_id
- disabled mode emits nothing (zero side-effects on disk)
- flush failures inside exporter don't raise out of the span
- current_span() returns the active span (used by insertion point 3)
"""
import json
from pathlib import Path

import pytest

from agent_runtime.observability import (
    configure, current_span, start_judge_span, start_tool_span, start_turn_span,
)


def _read_spans(d: Path) -> list[dict]:
    files = list(d.glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(ln) for ln in files[0].read_text().splitlines()]


async def test_start_turn_span_emits_one_turn_span(tmp_path: Path):
    configure(trace_dir=tmp_path, enabled=True)
    async with start_turn_span(
        chat_id="oc_x", msg_id="om_y", is_alert=True,
    ) as span:
        span.set_attribute("digital_agent.branch", "deep")
    spans = _read_spans(tmp_path)
    assert len(spans) == 1
    assert spans[0]["name"] == "turn"
    a = spans[0]["attributes"]
    assert a["digital_agent.chat_id"] == "oc_x"
    assert a["digital_agent.msg_id"] == "om_y"
    assert a["digital_agent.is_alert"] is True
    assert a["digital_agent.branch"] == "deep"


async def test_tool_span_links_to_turn_span(tmp_path: Path):
    configure(trace_dir=tmp_path, enabled=True)
    async with start_turn_span(chat_id="oc_x", msg_id="om_y", is_alert=False):
        with start_tool_span(tool_name="Bash", input_preview="ls"):
            pass
    spans = _read_spans(tmp_path)
    assert len(spans) == 2
    turn_s = next(s for s in spans if s["name"] == "turn")
    tool_s = next(s for s in spans if s["name"] == "tool_use")
    assert tool_s["trace_id"] == turn_s["trace_id"]
    assert tool_s["parent_span_id"] == turn_s["span_id"]
    assert tool_s["attributes"]["digital_agent.tool_name"] == "Bash"
    assert tool_s["attributes"]["digital_agent.tool_input_preview"] == "ls"


async def test_disabled_emits_nothing(tmp_path: Path):
    configure(trace_dir=tmp_path, enabled=False)
    async with start_turn_span(chat_id="x", msg_id="y", is_alert=False) as s:
        s.set_attribute("k", "v")  # must not raise
    assert _read_spans(tmp_path) == []


async def test_emit_failure_does_not_raise(tmp_path: Path, monkeypatch):
    """Even with a broken exporter, the turn must complete cleanly."""
    configure(trace_dir=tmp_path, enabled=True)
    from agent_runtime import observability as obs

    def boom(_):
        raise RuntimeError("disk full")
    monkeypatch.setattr(obs._state.exporter, "export", boom)

    # Must not raise:
    async with start_turn_span(chat_id="x", msg_id="y", is_alert=False):
        pass


async def test_current_span_returns_active_span(tmp_path: Path):
    """Insertion-point 3 reads current_span() outside the with-block."""
    configure(trace_dir=tmp_path, enabled=True)
    async with start_turn_span(chat_id="x", msg_id="y", is_alert=False) as turn:
        active = current_span()
        assert active is turn
        active.set_attribute("digital_agent.auth_failed", True)
    spans = _read_spans(tmp_path)
    assert spans[0]["attributes"]["digital_agent.auth_failed"] is True


async def test_judge_span_can_run_outside_turn(tmp_path: Path):
    """Observer meta-tracing may run without an enclosing turn."""
    configure(trace_dir=tmp_path, enabled=True)
    async with start_judge_span(judge_kind="llm_judge") as js:
        js.set_attribute("digital_agent.judge_model", "haiku")
    spans = _read_spans(tmp_path)
    assert len(spans) == 1
    assert spans[0]["name"] == "judge"
    assert spans[0]["attributes"]["digital_agent.judge_kind"] == "llm_judge"


async def test_tool_span_without_active_turn_is_noop(tmp_path: Path):
    """A tool_use observed outside a turn span emits nothing (no orphans)."""
    configure(trace_dir=tmp_path, enabled=True)
    with start_tool_span(tool_name="Bash", input_preview="x"):
        pass
    assert _read_spans(tmp_path) == []


async def test_exception_in_turn_marks_status_error(tmp_path: Path):
    configure(trace_dir=tmp_path, enabled=True)
    with pytest.raises(ValueError):
        async with start_turn_span(chat_id="x", msg_id="y", is_alert=False):
            raise ValueError("boom")
    spans = _read_spans(tmp_path)
    assert len(spans) == 1
    assert spans[0]["status"]["code"] == "ERROR"
