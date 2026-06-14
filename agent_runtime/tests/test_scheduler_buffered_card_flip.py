"""US-003 follow-up: buffered read path flips the placeholder '🔄 分析中...'
card to a final card on completion, mirroring the stream path. Before the
fix, the placeholder stayed in chat and the user only saw the answer in a
separate text reply."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu.adapter import StreamCardDegraded
from agent_runtime import scheduler, session
from agent_runtime.claude_proc import RunResult


@pytest.fixture(autouse=True)
def _configure_session(tmp_path):
    session.configure(tmp_path / "sess.json")
    yield


@pytest.fixture
def fake_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.send_card = AsyncMock(return_value="om_card_buf")
    ch.update_card = AsyncMock(return_value=True)
    ch.reply = AsyncMock(return_value=None)
    ch.fetch_topic_history = AsyncMock(return_value=[])
    ch.fetch_message_text = AsyncMock(return_value=None)
    return ch


def _parsed() -> ParsedMsg:
    return ParsedMsg(
        channel="feishu", message_id="om_q", thread_root_id="om_q",
        chat_id="oc_chat", sender_id="ou_user", sender_name="u",
        text="hi", mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


@pytest.fixture
def project_cfg(tmp_path: Path) -> dict:
    return {
        "work_dir": str(tmp_path / "work"),
        "model": "sonnet",
        "read_phase": {"disallowed_tools": ["Edit"], "disallowed_bash_patterns": []},
        "supported_msg_types": ["text"],
        "approval_timeout": 1800,
        "admin_users": [],
    }


@pytest.fixture
def runtime_cfg() -> dict:
    """Stream card disabled → buffered path is taken."""
    return {
        "paths": {"meta_work_dir": "/tmp/meta"},
        "reply_timeout": 30,
        "channels": {"feishu": {"stream_card": {"enabled": False}}},
    }


@pytest.fixture
def features_cfg() -> dict:
    return {"verifier": {"enabled": False}}


@pytest.mark.asyncio
async def test_buffered_path_flips_placeholder_to_final(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch,
):
    """Happy path: claude returns answer → buffered path flips placeholder
    card with build_final_card. channel.reply must NOT be called (the
    final card carries the answer)."""

    async def _fake_run(**kw):
        return RunResult(text="hello world", session_id="s1", exit_code=0, timed_out=False)

    monkeypatch.setattr("agent_runtime.claude_proc.run", _fake_run)
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    await scheduler._handle_message_inner(
        fake_channel, _parsed(), "billing", project_cfg, runtime_cfg, features_cfg,
    )

    fake_channel.send_card.assert_called_once()  # placeholder
    fake_channel.update_card.assert_called_once()  # flipped to final
    # The flip call carries the answer in the card body.
    args, kwargs = fake_channel.update_card.call_args
    flipped_card = args[1] if len(args) > 1 else kwargs.get("card") or args[-1]
    body_text = str(flipped_card)
    assert "hello world" in body_text, flipped_card
    # The final card is the only delivery; no text reply.
    fake_channel.reply.assert_not_called()


@pytest.mark.asyncio
async def test_buffered_path_send_card_degraded_falls_back_to_reply(
    fake_channel, project_cfg, runtime_cfg, features_cfg, monkeypatch,
):
    """If the placeholder send_card raises StreamCardDegraded, the buffered
    path must continue (it never depended on cards historically) and the
    user gets the answer via channel.reply."""
    fake_channel.send_card.side_effect = StreamCardDegraded("lark-cli timeout")

    async def _fake_run(**kw):
        return RunResult(text="answer text", session_id="s1", exit_code=0, timed_out=False)

    monkeypatch.setattr("agent_runtime.claude_proc.run", _fake_run)
    monkeypatch.setattr(
        scheduler, "_maybe_verify",
        AsyncMock(side_effect=lambda **kw: kw["draft"]),
    )

    await scheduler._handle_message_inner(
        fake_channel, _parsed(), "billing", project_cfg, runtime_cfg, features_cfg,
    )

    # No card → no flip attempt
    fake_channel.update_card.assert_not_called()
    # Answer arrives via text reply
    fake_channel.reply.assert_called_once()
    args, _ = fake_channel.reply.call_args
    assert "answer text" in args[1]
