"""Integration: stream insertion points 2-4.

Verifies _run_read_stream:
  - emits a child tool_use span per tool_use event (#2)
  - sets digital_agent.auth_failed on the turn span when _auth_failed
    sentinel arrives (#3)
  - sets gen_ai.usage.input_tokens/output_tokens on the turn span when
    the final result event carries a usage payload (#4)
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import observability, scheduler


def _parsed() -> ParsedMsg:
    return ParsedMsg(
        channel="feishu", message_id="om_x", thread_root_id="om_x",
        chat_id="oc_x", sender_id="ou_1", sender_name="u",
        text="?", mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


def _project_cfg(tmp_path: Path) -> dict:
    return {
        "work_dir": str(tmp_path / "p"),
        "admin_users": [],
        "model": "haiku",
        "read_phase": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"]},
        "supported_msg_types": ["text"],
        "approval_timeout": 1800,
    }


def _runtime_cfg() -> dict:
    return {
        "reply_timeout": 10,
        "channels": {"feishu": {"stream_card": {"enabled": False}}},
    }


def _read_spans(d: Path) -> list[dict]:
    files = list(d.glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(ln) for ln in files[0].read_text().splitlines()]


@pytest.mark.asyncio
async def test_stream_emits_tool_use_child_spans(tmp_path: Path):
    observability.configure(trace_dir=tmp_path / "tr", enabled=True)

    async def fake_stream(**kw):
        # Two tool_use events then a result
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "content_block": {
                "type": "tool_use", "name": "Bash", "input": {"command": "ls"}}}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_start", "content_block": {
                "type": "tool_use", "name": "Read",
                "input": {"file_path": "/tmp/x"}}}}
        yield {"type": "stream_event", "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "ok"}}}
        yield {"type": "result", "subtype": "success",
               "duration_ms": 5, "session_id": "s",
               "usage": {"input_tokens": 100, "output_tokens": 10},
               "modelUsage": {"claude-haiku-4-5": {"inputTokens": 100}}}

    channel = AsyncMock()
    channel.send_card = AsyncMock(return_value=None)  # card disabled
    channel.update_card = AsyncMock(return_value=True)

    with patch("agent_runtime.claude_proc.run_stream", fake_stream):
        async with observability.start_turn_span(
            chat_id="oc_x", msg_id="om_x", is_alert=False,
        ):
            await scheduler._run_read_stream(
                channel=channel, parsed=_parsed(),
                project_cfg=_project_cfg(tmp_path),
                runtime_cfg=_runtime_cfg(),
                session_id=None,
            )

    spans = _read_spans(tmp_path / "tr")
    tool_spans = [s for s in spans if s["name"] == "tool_use"]
    assert len(tool_spans) == 2
    names = {s["attributes"]["digital_agent.tool_name"] for s in tool_spans}
    assert names == {"Bash", "Read"}

    # turn span has token attrs from the final result event
    turn = next(s for s in spans if s["name"] == "turn")
    assert turn["attributes"]["gen_ai.usage.input_tokens"] == 100
    assert turn["attributes"]["gen_ai.usage.output_tokens"] == 10
    assert turn["attributes"]["gen_ai.system"] == "anthropic"
    assert turn["attributes"]["gen_ai.request.model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_stream_marks_auth_failed_on_sentinel(tmp_path: Path):
    observability.configure(trace_dir=tmp_path / "tr", enabled=True)

    async def fake_stream(**kw):
        yield {"type": "_auth_failed"}

    channel = AsyncMock()
    channel.send_card = AsyncMock(return_value=None)
    channel.update_card = AsyncMock(return_value=True)

    with patch("agent_runtime.claude_proc.run_stream", fake_stream):
        async with observability.start_turn_span(
            chat_id="oc_x", msg_id="om_x", is_alert=True,
        ):
            await scheduler._run_read_stream(
                channel=channel, parsed=_parsed(),
                project_cfg=_project_cfg(tmp_path),
                runtime_cfg=_runtime_cfg(),
                session_id=None,
            )

    spans = _read_spans(tmp_path / "tr")
    turn = next(s for s in spans if s["name"] == "turn")
    assert turn["attributes"].get("digital_agent.auth_failed") is True
