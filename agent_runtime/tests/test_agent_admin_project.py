"""US-admin-project-001: agent_admin /agent project dispatch + apply."""

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
    }


@dataclass
class _StubCtx:
    cfg: dict
    config_path: Path
    backup_dir: Path
    restart_alert_calls: list[None] = field(default_factory=list)
    restart_consume_calls: list[None] = field(default_factory=list)

    async def restart_alert_polling(self) -> None:
        self.restart_alert_calls.append(None)

    async def restart_consume(self) -> None:
        self.restart_consume_calls.append(None)


# ---------------------------------------------------------------------------
# _resolve_chat_id_by_group — pure function
# ---------------------------------------------------------------------------


def test_resolve_chat_id_unique_match():
    chats = [
        {"name": "答疑群", "chat_id": "oc_a"},
        {"name": "其它群", "chat_id": "oc_b"},
    ]
    out = agent_admin._resolve_chat_id_by_group(chats, "答疑群")
    assert out == "oc_a"


def test_resolve_chat_id_no_match_returns_empty_list():
    chats = [{"name": "其它群", "chat_id": "oc_b"}]
    out = agent_admin._resolve_chat_id_by_group(chats, "答疑群")
    assert out == []


def test_resolve_chat_id_multiple_match_returns_candidates():
    chats = [
        {"name": "答疑群", "chat_id": "oc_a"},
        {"name": "答疑群", "chat_id": "oc_c"},
    ]
    out = agent_admin._resolve_chat_id_by_group(chats, "答疑群")
    assert isinstance(out, list)
    assert {e["chat_id"] for e in out} == {"oc_a", "oc_c"}


# ---------------------------------------------------------------------------
# dispatch routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_project_list(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent project list")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "spring_billing" in body
    assert "oc_existing" in body
    # list is read-only — nothing staged
    assert agent_pending.get(agent_admin._thread_key(parsed)) is None


@pytest.mark.asyncio
async def test_dispatch_project_add_stages_pending(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    parsed = _msg('/agent project add autumn_qa /tmp/autumn --group "答疑群"')
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")

    async def fake_list_bot_chats():
        return [{"name": "答疑群", "chat_id": "oc_autumn"}]

    monkeypatch.setattr(agent_admin, "_list_bot_chats", fake_list_bot_chats)

    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "[APPROVAL_REQUIRED]" in body
    p = agent_pending.get(agent_admin._thread_key(parsed))
    assert p is not None
    assert p.action == "project_add"
    assert p.payload == {
        "name": "autumn_qa",
        "work_dir": "/tmp/autumn",
        "chat_id": "oc_autumn",
    }


@pytest.mark.asyncio
async def test_dispatch_project_add_group_not_found(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    parsed = _msg('/agent project add autumn_qa /tmp/autumn --group "答疑群"')
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")

    async def fake_list_bot_chats():
        return [{"name": "别的群", "chat_id": "oc_other"}]

    monkeypatch.setattr(agent_admin, "_list_bot_chats", fake_list_bot_chats)

    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "bot 不在该群或群名不符" in body
    assert agent_pending.get(agent_admin._thread_key(parsed)) is None


@pytest.mark.asyncio
async def test_dispatch_project_add_multiple_candidates(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    parsed = _msg('/agent project add autumn_qa /tmp/autumn --group "答疑群"')
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")

    async def fake_list_bot_chats():
        return [
            {"name": "答疑群", "chat_id": "oc_a"},
            {"name": "答疑群", "chat_id": "oc_b"},
        ]

    monkeypatch.setattr(agent_admin, "_list_bot_chats", fake_list_bot_chats)

    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "oc_a" in body and "oc_b" in body
    assert agent_pending.get(agent_admin._thread_key(parsed)) is None


@pytest.mark.asyncio
async def test_dispatch_project_add_denied_for_non_admin(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg('/agent project add autumn_qa /tmp/autumn --group "答疑群"',
                  sender_id="u_random")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "Permission denied" in body or "🚫" in body


@pytest.mark.asyncio
async def test_dispatch_project_rm_stages_pending(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent project rm spring_billing")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "[APPROVAL_REQUIRED]" in body
    p = agent_pending.get(agent_admin._thread_key(parsed))
    assert p is not None
    assert p.action == "project_rm"
    assert p.payload == {"name": "spring_billing"}


@pytest.mark.asyncio
async def test_dispatch_project_rm_unknown(tmp_path):
    cfg = _cfg(tmp_path)
    parsed = _msg("/agent project rm nope")
    channel = AsyncMock()
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    await agent_admin.dispatch(
        parsed, cfg["projects"]["spring_billing"], cfg, channel, ctx,
    )
    body = channel.reply.await_args.args[1]
    assert "未找到" in body
    assert agent_pending.get(agent_admin._thread_key(parsed)) is None


# ---------------------------------------------------------------------------
# apply_pending call chains (mock config_writer + ctx.restart_consume)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_pending_project_add(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    channel = AsyncMock()
    parsed = _msg("同意")

    calls = {}

    def fake_add_project(cfg_path, *, name, work_dir, chat_id, backup_dir,
                         **kw):
        calls["add"] = dict(name=name, work_dir=work_dir, chat_id=chat_id)

    monkeypatch.setattr(agent_admin.config_writer, "add_project",
                        fake_add_project)
    monkeypatch.setattr(agent_admin, "_reload_cfg_in_place",
                        lambda ctx, cfg: None)

    pending = agent_pending.Pending(
        thread_key="k", action="project_add",
        payload={"name": "autumn_qa", "work_dir": "/tmp/autumn",
                 "chat_id": "oc_autumn"},
        sender_id="u_admin",
    )
    await agent_admin.apply_pending(parsed, pending, cfg, channel, ctx)

    assert calls["add"] == {"name": "autumn_qa", "work_dir": "/tmp/autumn",
                            "chat_id": "oc_autumn"}
    assert len(ctx.restart_consume_calls) == 1


@pytest.mark.asyncio
async def test_apply_pending_project_rm(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    channel = AsyncMock()
    parsed = _msg("同意")

    calls = {}

    def fake_remove_project(cfg_path, *, name, backup_dir, **kw):
        calls["rm"] = name

    monkeypatch.setattr(agent_admin.config_writer, "remove_project",
                        fake_remove_project)
    monkeypatch.setattr(agent_admin, "_reload_cfg_in_place",
                        lambda ctx, cfg: None)

    pending = agent_pending.Pending(
        thread_key="k", action="project_rm",
        payload={"name": "spring_billing"},
        sender_id="u_admin",
    )
    await agent_admin.apply_pending(parsed, pending, cfg, channel, ctx)

    assert calls["rm"] == "spring_billing"
    assert len(ctx.restart_consume_calls) == 1


@pytest.mark.asyncio
async def test_apply_pending_project_add_config_error(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    ctx = _StubCtx(cfg=cfg, config_path=tmp_path / "config.yaml",
                   backup_dir=tmp_path / "bak")
    channel = AsyncMock()
    parsed = _msg("同意")

    def boom(*a, **kw):
        raise agent_admin.config_writer.ConfigWriteError("conflict")

    monkeypatch.setattr(agent_admin.config_writer, "add_project", boom)

    pending = agent_pending.Pending(
        thread_key="k", action="project_add",
        payload={"name": "autumn_qa", "work_dir": "/tmp/a",
                 "chat_id": "oc_a"},
        sender_id="u_admin",
    )
    await agent_admin.apply_pending(parsed, pending, cfg, channel, ctx)
    body = channel.reply.await_args.args[1]
    assert "配置写入失败" in body
    assert len(ctx.restart_consume_calls) == 0
