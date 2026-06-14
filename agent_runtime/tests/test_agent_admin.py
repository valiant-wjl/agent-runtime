"""US-admin-001: agent_admin dispatcher — read commands first."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import agent_admin, agent_pending


def setup_function():
    agent_pending.clear_all()


def _msg(text: str, sender_id: str = "u_admin", chat_id: str = "oc_dm",
         chat_type: str = "p2p") -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id="m1",
        thread_root_id="m1",
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name="Admin",
        text=text,
        mentions=[],
        chat_type=chat_type,
        raw_event={},
    )


def _project_cfg(work_dir: Path) -> dict:
    return {
        "work_dir": str(work_dir),
        "admin_users": ["u_admin"],
        "chat_ids": ["oc_existing"],
        "read_phase": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"]},
    }


def _cfg(work_dir: Path) -> dict:
    return {
        "version": 1,
        "channels": {"feishu": {"enabled": True}},
        "projects": {"spring_billing": _project_cfg(work_dir)},
        "runtime": {"session_file": "./.state/sessions.json"},
        "alert_resolver": {
            "enabled": True,
            "ttl_days": 14,
            "retriever": "keyword",
            "top_k": 3,
            "alert_chats": [
                {"chat_id": "oc_existing", "project": "spring_billing"},
                {"chat_id": "oc_keep", "project": "spring_billing"},
            ],
        },
    }


@dataclass
class _StubCtx:
    cfg: dict
    config_path: Path
    backup_dir: Path
    restart_calls: list[None] = field(default_factory=list)

    async def restart_alert_polling(self) -> None:
        self.restart_calls.append(None)


@pytest.mark.asyncio
async def test_dispatch_permission_denied(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent show", sender_id="u_random")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    channel.reply.assert_awaited_once()
    body = channel.reply.await_args.args[1]
    assert "Permission denied" in body or "🚫" in body


@pytest.mark.asyncio
async def test_dispatch_show_lists_alert_chats(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent show")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "oc_existing" in body
    assert "spring_billing" in body


@pytest.mark.asyncio
async def test_dispatch_alert_list(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent alert list")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "oc_existing" in body


@pytest.mark.asyncio
async def test_dispatch_help_on_unknown(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent foo bar")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "用法" in body or "usage" in body.lower()


@pytest.mark.asyncio
async def test_dispatch_show_empty_alert_chats(tmp_path):
    cfg = _cfg(tmp_path)
    cfg["alert_resolver"]["alert_chats"] = []
    parsed = _msg("/agent show")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "0" in body or "暂无" in body or "把 bot 拉到群里" in body


@pytest.mark.asyncio
async def test_dispatch_alert_remove_stages_pending(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent alert remove oc_existing")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "[APPROVAL_REQUIRED]" in body
    assert "oc_existing" in body
    p = agent_pending.get(agent_admin._thread_key(parsed))
    assert p is not None
    assert p.action == "alert_remove"


@pytest.mark.asyncio
async def test_dispatch_alert_remove_unknown_id(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent alert remove oc_nope")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "未找到" in body
    assert agent_pending.get(agent_admin._thread_key(parsed)) is None


@pytest.mark.asyncio
async def test_dispatch_alert_register_idempotent_when_present(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent alert register", chat_id="oc_existing",
                  chat_type="group")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "已在监听" in body
    assert agent_pending.get(agent_admin._thread_key(parsed)) is None


@pytest.mark.asyncio
async def test_dispatch_alert_register_stages_pending(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent alert register", chat_id="oc_new_group",
                  chat_type="group")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "[APPROVAL_REQUIRED]" in body
    assert "oc_new_group" in body
    p = agent_pending.get(agent_admin._thread_key(parsed))
    assert p is not None and p.action == "alert_register"
    assert p.payload == {"chat_id": "oc_new_group", "project": "spring_billing"}


@pytest.mark.asyncio
async def test_apply_pending_register_writes_config_and_reloads(tmp_path):
    # Real cfg + real file → real writer round-trip
    cfg = _cfg(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    from ruamel.yaml import YAML
    YAML(typ="rt").dump(cfg, cfg_path.open("w"))

    parsed = _msg("/agent alert register", chat_id="oc_new_group",
                  chat_type="group")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=cfg_path,
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    pending = agent_pending.get(agent_admin._thread_key(parsed))
    await agent_admin.apply_pending(parsed, pending, cfg, channel, ctx)

    chats = cfg["alert_resolver"]["alert_chats"]
    assert any(e["chat_id"] == "oc_new_group" for e in chats)
    assert "oc_new_group" in cfg["projects"]["spring_billing"]["chat_ids"]
    assert len(ctx.restart_calls) == 1


@pytest.mark.asyncio
async def test_apply_pending_remove(tmp_path):
    # Fixture seeds 2 alert_chats; removing oc_existing leaves oc_keep so
    # schema validation (alert_chats must be non-empty when enabled) passes.
    cfg = _cfg(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    from ruamel.yaml import YAML
    YAML(typ="rt").dump(cfg, cfg_path.open("w"))

    parsed = _msg("/agent alert remove oc_existing")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=cfg_path,
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    pending = agent_pending.get(agent_admin._thread_key(parsed))
    await agent_admin.apply_pending(parsed, pending, cfg, channel, ctx)

    chats = cfg["alert_resolver"]["alert_chats"]
    assert not any(e["chat_id"] == "oc_existing" for e in chats)
    assert any(e["chat_id"] == "oc_keep" for e in chats)
    assert len(ctx.restart_calls) == 1
