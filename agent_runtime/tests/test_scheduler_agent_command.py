"""US-sched-agent-001: end-to-end /agent integration through scheduler."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from ruamel.yaml import YAML

from agent_runtime.channels import ParsedMsg
from agent_runtime import agent_pending, scheduler
from agent_runtime.scheduler import SchedulerContext, _handle_message_inner_impl


def setup_function():
    agent_pending.clear_all()


def _seed_config(tmp_path: Path) -> tuple[Path, dict]:
    data = {
        "version": 1,
        "channels": {"feishu": {"enabled": True}},
        "projects": {
            "example_project": {
                "work_dir": str(tmp_path / "wd"),
                "admin_users": ["u_admin"],
                "chat_ids": ["oc_existing"],
                "read_phase": {"disallowed_tools":
                                ["Edit", "Write", "NotebookEdit"]},
            },
        },
        "runtime": {"session_file": str(tmp_path / ".state/sessions.json")},
        "alert_resolver": {
            "enabled": True,
            "ttl_days": 14,
            "retriever": "keyword",
            "top_k": 3,
            "alert_chats": [
                {"chat_id": "oc_existing", "project": "example_project"},
            ],
        },
    }
    p = tmp_path / "config.yaml"
    YAML(typ="rt").dump(data, p.open("w"))
    return p, data


def _msg(text, sender="u_admin", chat="oc_dm", chat_type="p2p",
         topic_id=None, message_id="m1", thread_root_id=None):
    # Provide every ParsedMsg required field. thread_root_id defaults to
    # message_id (treated as a single-message thread) so _thread_key
    # picks up the same key for the approval reply.
    return ParsedMsg(
        channel="feishu",
        chat_id=chat,
        message_id=message_id,
        thread_root_id=thread_root_id or message_id,
        sender_id=sender,
        sender_name="Admin",
        text=text,
        chat_type=chat_type,
        mentions=[],
        raw_event={},
        topic_id=topic_id,
    )


def _make_ctx(cfg_path, cfg):
    return SchedulerContext(
        cfg=cfg, config_path=cfg_path,
        backup_dir=cfg_path.parent / "bak",
        restart_alert_polling_fn=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_scheduler_dispatches_agent_show(tmp_path):
    cfg_path, cfg = _seed_config(tmp_path)
    ctx = _make_ctx(cfg_path, cfg)
    channel = AsyncMock()
    parsed = _msg("/agent show")
    summary = scheduler._TurnSummary(msg_id="m1", chat_id="oc_dm")
    await _handle_message_inner_impl(
        channel, parsed, "example_project",
        cfg["projects"]["example_project"], cfg["runtime"],
        features_cfg={}, alert_cfg=cfg["alert_resolver"], summary=summary,
        scheduler_ctx=ctx,
    )
    assert summary.branch == "agent_command"
    body = channel.reply.await_args.args[1]
    assert "oc_existing" in body


@pytest.mark.asyncio
async def test_scheduler_register_full_flow(tmp_path):
    cfg_path, cfg = _seed_config(tmp_path)
    ctx = _make_ctx(cfg_path, cfg)
    channel = AsyncMock()
    summary = scheduler._TurnSummary(msg_id="m1", chat_id="oc_new")

    parsed_register = _msg("/agent alert register", chat="oc_new",
                            chat_type="group", message_id="m1")
    await _handle_message_inner_impl(
        channel, parsed_register, "example_project",
        cfg["projects"]["example_project"], cfg["runtime"],
        features_cfg={}, alert_cfg=cfg["alert_resolver"], summary=summary,
        scheduler_ctx=ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "[APPROVAL_REQUIRED]" in body

    # Approve as a PLAIN group message (no feishu "回复" gesture →
    # thread_root_id falls back to the approve message's own id, NOT
    # the register's). agent_pending uses chat_id+sender_id keying so
    # this still matches; this assertion pins that property.
    parsed_approve = _msg("同意", chat="oc_new", chat_type="group",
                           message_id="m2", thread_root_id="m2")
    summary2 = scheduler._TurnSummary(msg_id="m2", chat_id="oc_new")
    await _handle_message_inner_impl(
        channel, parsed_approve, "example_project",
        cfg["projects"]["example_project"], cfg["runtime"],
        features_cfg={}, alert_cfg=cfg["alert_resolver"], summary=summary2,
        scheduler_ctx=ctx,
    )
    assert summary2.branch == "agent_command_apply"
    chats = [e["chat_id"] for e in cfg["alert_resolver"]["alert_chats"]]
    assert "oc_new" in chats
    assert ctx.restart_alert_polling_fn.await_count == 1


@pytest.mark.asyncio
async def test_scheduler_non_admin_denied(tmp_path):
    cfg_path, cfg = _seed_config(tmp_path)
    ctx = _make_ctx(cfg_path, cfg)
    channel = AsyncMock()
    parsed = _msg("/agent show", sender="u_random")
    summary = scheduler._TurnSummary(msg_id="m1", chat_id="oc_dm")
    await _handle_message_inner_impl(
        channel, parsed, "example_project",
        cfg["projects"]["example_project"], cfg["runtime"],
        features_cfg={}, alert_cfg=cfg["alert_resolver"], summary=summary,
        scheduler_ctx=ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "Permission denied" in body or "🚫" in body


@pytest.mark.asyncio
async def test_scheduler_cancel_pending(tmp_path):
    cfg_path, cfg = _seed_config(tmp_path)
    # Seed 2 alert_chats so removing one keeps the list non-empty
    cfg["alert_resolver"]["alert_chats"].append(
        {"chat_id": "oc_keep", "project": "example_project"},
    )
    YAML(typ="rt").dump(cfg, cfg_path.open("w"))

    ctx = _make_ctx(cfg_path, cfg)
    channel = AsyncMock()
    summary = scheduler._TurnSummary(msg_id="m1", chat_id="oc_dm")
    parsed_remove = _msg("/agent alert remove oc_existing")
    await _handle_message_inner_impl(
        channel, parsed_remove, "example_project",
        cfg["projects"]["example_project"], cfg["runtime"],
        features_cfg={}, alert_cfg=cfg["alert_resolver"], summary=summary,
        scheduler_ctx=ctx,
    )
    parsed_cancel = _msg("取消", message_id="m2", thread_root_id="m1")
    summary2 = scheduler._TurnSummary(msg_id="m2", chat_id="oc_dm")
    await _handle_message_inner_impl(
        channel, parsed_cancel, "example_project",
        cfg["projects"]["example_project"], cfg["runtime"],
        features_cfg={}, alert_cfg=cfg["alert_resolver"], summary=summary2,
        scheduler_ctx=ctx,
    )
    assert summary2.branch == "agent_command_cancel"
    body = channel.reply.await_args.args[1]
    assert "取消" in body
    chats = [e["chat_id"] for e in cfg["alert_resolver"]["alert_chats"]]
    assert "oc_existing" in chats  # unchanged
