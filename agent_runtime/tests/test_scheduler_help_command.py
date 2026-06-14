"""Scheduler integration: `/help` returns the unified menu in any chat.

Spec:
  - `/help` is recognised before /agent so it never trips admin-gating.
  - Reply is the rendered help text from agent_runtime.help_cmd.render_help().
  - claude_proc.run is NEVER invoked (zero-cost informational path).
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import concurrency, help_cmd, scheduler, session


def _parsed(text: str) -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id="m-help",
        thread_root_id="t-help",
        chat_id="c-help",
        sender_id="ou-nonadmin",  # deliberately NOT an admin
        sender_name="u",
        text=text,
        mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


def _channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.reply = AsyncMock(return_value=None)
    ch.send_card = AsyncMock(return_value="card-1")
    ch.update_card = AsyncMock(return_value=None)
    return ch


def _project_cfg(work_dir: Path) -> dict:
    return {
        "work_dir": str(work_dir),
        "display_name": "lbp-growth-agent",
        "model": "opus",
        "admin_users": ["ou_admin_only"],  # /help sender is NOT in here
        "approval_timeout": 1800,
        "read_phase": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "disallowed_bash_patterns": [],
        },
        "write_phase": {"timeout": 600},
        "supported_msg_types": ["text", "post"],
        "unsupported_msg_reply": "暂不支持",
    }


_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


@pytest.fixture(autouse=True)
def _init():
    concurrency.init_global(10)
    yield


@pytest.mark.asyncio
async def test_help_command_replies_menu_without_invoking_claude(tmp_path):
    """/help → reply unified menu, no claude_proc, no admin gating."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, _parsed("/help"), "p", _project_cfg(work_dir), _RUNTIME_CFG,
        )

    run_mock.assert_not_called()
    ch.reply.assert_called_once()
    reply_text = ch.reply.call_args[0][1]
    assert reply_text == help_cmd.render_help()


@pytest.mark.asyncio
async def test_help_command_works_for_non_admin(tmp_path):
    """/help must NOT route through agent_admin (which would 'permission
    denied' a non-admin). The user is not in admin_users; reply should
    still be the help menu, not the denial string."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()):
        await scheduler.handle_message(
            ch, _parsed("/help"), "p", _project_cfg(work_dir), _RUNTIME_CFG,
        )

    reply_text = ch.reply.call_args[0][1]
    assert "Permission denied" not in reply_text
    assert "🚫" not in reply_text
    assert "/lesson" in reply_text  # sanity: it's the menu


@pytest.mark.asyncio
async def test_help_command_with_trailing_args_still_renders_menu(tmp_path):
    """`/help foo` → still the full menu (args ignored)."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _channel()

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()):
        await scheduler.handle_message(
            ch, _parsed("/help anything"), "p", _project_cfg(work_dir),
            _RUNTIME_CFG,
        )

    assert ch.reply.call_args[0][1] == help_cmd.render_help()
