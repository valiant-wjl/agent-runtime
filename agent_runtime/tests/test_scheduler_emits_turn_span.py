"""Integration: each call to _handle_message_inner emits exactly one
turn span carrying chat_id / msg_id / branch / etc, even on early return
or exception path.
"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import observability, scheduler


def _parsed(chat="oc_x", msg="om_y") -> ParsedMsg:
    return ParsedMsg(
        channel="feishu", message_id=msg, thread_root_id=msg,
        chat_id=chat, sender_id="ou_1", sender_name="u",
        text="?", mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


def _read_spans(d: Path) -> list[dict]:
    files = list(d.glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(ln) for ln in files[0].read_text().splitlines()]


@pytest.fixture
def project_cfg(tmp_path: Path) -> dict:
    return {
        "work_dir": str(tmp_path / "p"),
        "admin_users": [],
        "model": "haiku",
        "read_phase": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"]},
        "supported_msg_types": ["text"],
        "approval_timeout": 1800,
    }


@pytest.fixture
def runtime_cfg() -> dict:
    return {"reply_timeout": 10, "channels": {"feishu": {}}}


@pytest.mark.asyncio
async def test_inner_handler_emits_one_turn_span(tmp_path, project_cfg, runtime_cfg):
    observability.configure(trace_dir=tmp_path / "traces", enabled=True)

    async def fake_impl(*a, **kw):
        kw["summary"].branch = "deep"
        kw["summary"].text_len = 42
        kw["summary"].reply_text = "bot 的完整回复内容 (full reply text)"
        return None

    with patch.object(scheduler, "_handle_message_inner_impl", side_effect=fake_impl):
        await scheduler._handle_message_inner(
            channel=AsyncMock(), parsed=_parsed(),
            project_name="p", project_cfg=project_cfg,
            runtime_cfg=runtime_cfg, alert_cfg=None,
        )

    spans = _read_spans(tmp_path / "traces")
    turn = [s for s in spans if s["name"] == "turn"]
    assert len(turn) == 1
    a = turn[0]["attributes"]
    assert a["digital_agent.chat_id"] == "oc_x"
    assert a["digital_agent.msg_id"] == "om_y"
    assert a["digital_agent.branch"] == "deep"
    assert a["digital_agent.text_len"] == 42
    # Bug A fix: full reply text on span so observer judge can see it
    assert a["digital_agent.text"] == "bot 的完整回复内容 (full reply text)"
    # Bug B fix: source text (user question / alert body) emitted on entry
    assert a["digital_agent.alert_text"] == "?"
    assert turn[0]["status"]["code"] == "OK"


@pytest.mark.asyncio
async def test_long_reply_truncated_to_3000_chars(tmp_path, project_cfg, runtime_cfg):
    """Span attribute size is bounded to keep jsonl rows reasonable."""
    observability.configure(trace_dir=tmp_path / "traces", enabled=True)

    async def fake_impl(*a, **kw):
        kw["summary"].reply_text = "A" * 5000
        return None

    with patch.object(scheduler, "_handle_message_inner_impl", side_effect=fake_impl):
        await scheduler._handle_message_inner(
            channel=AsyncMock(), parsed=_parsed(),
            project_name="p", project_cfg=project_cfg,
            runtime_cfg=runtime_cfg, alert_cfg=None,
        )
    spans = _read_spans(tmp_path / "traces")
    a = next(s for s in spans if s["name"] == "turn")["attributes"]
    assert len(a["digital_agent.text"]) == 3000


@pytest.mark.asyncio
async def test_inner_handler_marks_error_on_exception(tmp_path, project_cfg, runtime_cfg):
    observability.configure(trace_dir=tmp_path / "traces", enabled=True)

    async def explode(*a, **kw):
        raise RuntimeError("boom")

    with patch.object(scheduler, "_handle_message_inner_impl", side_effect=explode):
        with pytest.raises(RuntimeError, match="boom"):
            await scheduler._handle_message_inner(
                channel=AsyncMock(), parsed=_parsed(),
                project_name="p", project_cfg=project_cfg,
                runtime_cfg=runtime_cfg, alert_cfg=None,
            )

    spans = _read_spans(tmp_path / "traces")
    assert len(spans) == 1
    assert spans[0]["status"]["code"] == "ERROR"


@pytest.mark.asyncio
async def test_alert_branch_reflected_on_span(tmp_path, project_cfg, runtime_cfg):
    observability.configure(trace_dir=tmp_path / "traces", enabled=True)

    async def fake_impl(*a, **kw):
        kw["summary"].branch = "alert_hit"
        kw["summary"].is_alert = True
        return None

    with patch.object(scheduler, "_handle_message_inner_impl", side_effect=fake_impl):
        await scheduler._handle_message_inner(
            channel=AsyncMock(), parsed=_parsed(),
            project_name="p", project_cfg=project_cfg,
            runtime_cfg=runtime_cfg, alert_cfg=None,
        )
    spans = _read_spans(tmp_path / "traces")
    a = spans[0]["attributes"]
    assert a["digital_agent.is_alert"] is True
    assert a["digital_agent.branch"] == "alert_hit"
