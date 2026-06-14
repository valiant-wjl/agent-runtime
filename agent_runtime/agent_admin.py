"""Agent admin orchestrator — dispatch /agent commands.

Read commands (show / list) reply directly. Write commands (remove /
register) stage a Pending via ``agent_pending`` and rely on the
scheduler reply branch to call ``apply_pending`` on approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Protocol

from agent_runtime.channels import ChannelAdapter, ParsedMsg
from agent_runtime.channels.feishu._env import build_lark_cli_env
from agent_runtime import agent_cmd, agent_pending, config_writer

log = logging.getLogger(__name__)


# How long (seconds) to wait for the lark-cli chat-list subprocess.
_CHAT_LIST_TIMEOUT = 30.0

_USAGE = (
    "用法：\n"
    "  /agent show — 查看监听总览\n"
    "  /agent alert list — 列出告警群\n"
    "  /agent alert remove <chat_id> — 摘除告警群（需确认）\n"
    "  /agent alert register [--project <name>] — 把当前群注册为告警群（需确认）\n"
    "  /agent project list — 列出项目总览\n"
    '  /agent project add <name> <work_dir> --group "<群名>" — 新增只读答疑项目（需确认）\n'
    "  /agent project rm <name> — 删除项目（需确认）"
)

_DENIED = "🚫 Permission denied（仅 admin_users 可使用 /agent 命令）"


class SchedulerContext(Protocol):
    cfg: dict
    config_path: Path
    backup_dir: Path

    async def restart_alert_polling(self) -> None: ...

    async def restart_consume(self) -> None: ...


def _is_admin(parsed: ParsedMsg, project_cfg: dict) -> bool:
    admins = project_cfg.get("admin_users") or []
    return parsed.sender_id in admins


def _thread_key(parsed: ParsedMsg) -> str:
    # Delegate to agent_pending so stage + scheduler lookup use the same
    # keying. We deliberately do NOT use scheduler._thread_key (topic /
    # thread_root_id) because feishu only populates root_id on explicit
    # "回复" gestures — a plain group reply of "同意" wouldn't match.
    # See runtime/agent_pending.py module docstring for full rationale.
    return agent_pending.thread_key(parsed)


async def dispatch(
    parsed: ParsedMsg,
    project_cfg: dict,
    cfg: dict,
    channel: ChannelAdapter,
    ctx: SchedulerContext,
) -> None:
    if not _is_admin(parsed, project_cfg):
        await channel.reply(parsed, _DENIED)
        return

    cmd = agent_cmd.parse_agent(parsed.text)
    if cmd is None:
        # Should not happen — scheduler already gated by is_agent_command
        log.warning("agent_admin.dispatch called on non-agent text: %r",
                    parsed.text)
        return

    if cmd.verb == "_help":
        await channel.reply(parsed, _USAGE)
        return

    if cmd.verb == "show":
        await _render_show(parsed, cfg, channel)
        return

    if cmd.verb == "alert" and cmd.sub == "list":
        await _render_alert_list(parsed, cfg, channel)
        return

    if cmd.verb == "alert" and cmd.sub == "remove":
        await _stage_remove(parsed, cmd, cfg, channel)
        return

    if cmd.verb == "alert" and cmd.sub == "register":
        await _stage_register(parsed, cmd, project_cfg, cfg, channel)
        return

    if cmd.verb == "project" and cmd.sub == "list":
        await _render_project_list(parsed, cfg, channel)
        return

    if cmd.verb == "project" and cmd.sub == "add":
        await _stage_project_add(parsed, cmd, project_cfg, cfg, channel)
        return

    if cmd.verb == "project" and cmd.sub == "rm":
        await _stage_project_rm(parsed, cmd, project_cfg, cfg, channel)
        return

    await channel.reply(parsed, _USAGE)


async def _render_show(parsed: ParsedMsg, cfg: dict, channel: ChannelAdapter) -> None:
    alert_cfg = cfg.get("alert_resolver") or {}
    chats = alert_cfg.get("alert_chats") or []
    polling = (alert_cfg.get("polling") or {}).get("enabled", False)
    if not chats:
        body = (
            "📋 当前 0 个监听群。\n"
            "把 bot 拉到群里 + 在群里 @bot /agent alert register 即可注册。"
        )
    else:
        lines = [f"📋 当前监听 {len(chats)} 个告警群（polling: "
                 f"{'on' if polling else 'off'}）："]
        for e in chats:
            lines.append(f"  - {e.get('chat_id')} → {e.get('project')}")
        body = "\n".join(lines)
    await channel.reply(parsed, body)


async def _render_alert_list(parsed: ParsedMsg, cfg: dict, channel: ChannelAdapter) -> None:
    chats = (cfg.get("alert_resolver") or {}).get("alert_chats") or []
    if not chats:
        await channel.reply(parsed, "📋 alert_chats 暂无条目")
        return
    lines = ["#  chat_id                              project"]
    for i, e in enumerate(chats, 1):
        cid = e.get("chat_id", "?")
        proj = e.get("project", "?")
        lines.append(f"{i:<2} {cid:<36} {proj}")
    await channel.reply(parsed, "```\n" + "\n".join(lines) + "\n```")


async def _stage_remove(
    parsed: ParsedMsg, cmd: agent_cmd.AgentCommand, cfg: dict,
    channel: ChannelAdapter,
) -> None:
    chat_id = cmd.args[0]
    chats = (cfg.get("alert_resolver") or {}).get("alert_chats") or []
    target = next((e for e in chats if e.get("chat_id") == chat_id), None)
    if target is None:
        await channel.reply(
            parsed,
            f"未找到 chat_id={chat_id}，请用 `/agent alert list` 查看",
        )
        return
    # Scope confirmation auth to the target chat's project admins only.
    # In a multi-project deploy, project-A admins should not approve
    # removal of project-B's alert chats.
    target_project = target.get("project")
    projects = cfg.get("projects") or {}
    admin_users = list(
        (projects.get(target_project) or {}).get("admin_users") or [],
    )
    agent_pending.stage(
        thread_key=_thread_key(parsed),
        action="alert_remove",
        payload={"chat_id": chat_id},
        sender_id=parsed.sender_id,
        admin_users=admin_users,
    )
    await channel.reply(
        parsed,
        f"[APPROVAL_REQUIRED]\n"
        f"确认从 alert_chats 摘除 {chat_id}？\n"
        f"回复 同意 / Y 执行，其他视为取消。",
    )


async def _stage_register(
    parsed: ParsedMsg, cmd: agent_cmd.AgentCommand, project_cfg: dict,
    cfg: dict, channel: ChannelAdapter,
) -> None:
    chat_id = parsed.chat_id
    chats = (cfg.get("alert_resolver") or {}).get("alert_chats") or []
    if any(e.get("chat_id") == chat_id for e in chats):
        existing = next(e for e in chats if e.get("chat_id") == chat_id)
        await channel.reply(
            parsed,
            f"✅ 该群已在监听中（id={chat_id}，project={existing.get('project')}）",
        )
        return

    projects = cfg.get("projects") or {}
    target_project = cmd.flags.get("project")
    if target_project:
        if target_project not in projects:
            await channel.reply(
                parsed, f"project={target_project} 不存在",
            )
            return
    else:
        if len(projects) == 1:
            target_project = next(iter(projects.keys()))
        else:
            names = ", ".join(projects.keys())
            await channel.reply(
                parsed,
                f"请用 --project <name> 显式指定。可选：{names}",
            )
            return

    agent_pending.stage(
        thread_key=_thread_key(parsed),
        action="alert_register",
        payload={"chat_id": chat_id, "project": target_project},
        sender_id=parsed.sender_id,
        admin_users=projects[target_project].get("admin_users") or [],
    )
    await channel.reply(
        parsed,
        f"[APPROVAL_REQUIRED]\n"
        f"确认把当前群 {chat_id} 注册为 {target_project} 的告警群？\n"
        f"回复 同意 / Y 执行，其他视为取消。",
    )


async def _render_project_list(
    parsed: ParsedMsg, cfg: dict, channel: ChannelAdapter,
) -> None:
    projects = cfg.get("projects") or {}
    if not projects:
        await channel.reply(parsed, "📋 当前 0 个项目")
        return
    lines = [f"📋 当前 {len(projects)} 个项目："]
    for name, proj in projects.items():
        wd = proj.get("work_dir", "?")
        cids = ", ".join(proj.get("chat_ids") or []) or "(无)"
        lines.append(f"  - {name} → {wd}  [chat_ids: {cids}]")
    await channel.reply(parsed, "\n".join(lines))


def _resolve_chat_id_by_group(
    chats: list[dict], group_name: str,
) -> "str | list[dict]":
    """Match bot's chats by exact name. Returns the unique chat_id (str) on
    a single match, or the list of candidate dicts (possibly empty) so the
    caller can render 0-match / multi-match messages."""
    matched = [c for c in chats if c.get("name") == group_name]
    if len(matched) == 1:
        return matched[0]["chat_id"]
    return matched


async def _list_bot_chats(*, lark_cli: str = "lark-cli") -> list[dict]:
    """Page through ``lark-cli im +chat-list --as bot`` and return all chats
    the bot belongs to as ``[{"name", "chat_id", ...}, ...]``. On any
    subprocess / parse error, returns ``[]`` (caller renders a 0-match
    message). Isolated here so dispatch tests can monkeypatch it."""
    chats: list[dict] = []
    page_token = ""
    while True:
        args = [
            lark_cli, "im", "+chat-list",
            "--as", "bot",
            "--page-size", "100",
        ]
        if page_token:
            args += ["--page-token", page_token]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CHAT_LIST_TIMEOUT,
            )
        except (FileNotFoundError, PermissionError) as e:
            log.warning("_list_bot_chats spawn failed: %s", e)
            return chats
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("_list_bot_chats timed out")
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (ProcessLookupError, asyncio.TimeoutError, TimeoutError):
                pass
            return chats
        if proc.returncode != 0:
            log.warning(
                "_list_bot_chats lark-cli exit %d: %s",
                proc.returncode, stderr.decode(errors="replace")[:200],
            )
            return chats
        try:
            payload = json.loads(stdout.decode(errors="replace") or "{}")
        except json.JSONDecodeError as e:
            log.warning("_list_bot_chats stdout not JSON: %s", e)
            return chats
        data = payload.get("data") or {}
        chats.extend(data.get("chats") or [])
        page_token = data.get("page_token") or ""
        if not data.get("has_more") or not page_token:
            break
    return chats


async def _stage_project_add(
    parsed: ParsedMsg, cmd: agent_cmd.AgentCommand, project_cfg: dict,
    cfg: dict, channel: ChannelAdapter,
) -> None:
    name, work_dir = cmd.args[0], cmd.args[1]
    group_name = cmd.flags["group"]

    chats = await _list_bot_chats()
    resolved = _resolve_chat_id_by_group(chats, group_name)
    if isinstance(resolved, list):
        if not resolved:
            await channel.reply(
                parsed,
                f"未解析到群「{group_name}」：bot 不在该群或群名不符",
            )
            return
        lines = [f"群名「{group_name}」匹配到多个，请改用确切 chat_id 指定："]
        for c in resolved:
            lines.append(f"  - {c.get('chat_id')}  ({c.get('name')})")
        await channel.reply(parsed, "\n".join(lines))
        return

    chat_id = resolved
    agent_pending.stage(
        thread_key=_thread_key(parsed),
        action="project_add",
        payload={"name": name, "work_dir": work_dir, "chat_id": chat_id},
        sender_id=parsed.sender_id,
        admin_users=project_cfg.get("admin_users") or [],
    )
    await channel.reply(
        parsed,
        f"[APPROVAL_REQUIRED]\n"
        f"确认新增只读答疑项目 {name}？\n"
        f"  work_dir: {work_dir}\n"
        f"  群「{group_name}」→ chat_id: {chat_id}\n"
        f"回复 同意 / Y 执行，其他视为取消。",
    )


async def _stage_project_rm(
    parsed: ParsedMsg, cmd: agent_cmd.AgentCommand, project_cfg: dict,
    cfg: dict, channel: ChannelAdapter,
) -> None:
    name = cmd.args[0]
    projects = cfg.get("projects") or {}
    if name not in projects:
        await channel.reply(
            parsed,
            f"未找到项目 {name}，请用 `/agent project list` 查看",
        )
        return
    agent_pending.stage(
        thread_key=_thread_key(parsed),
        action="project_rm",
        payload={"name": name},
        sender_id=parsed.sender_id,
        admin_users=project_cfg.get("admin_users") or [],
    )
    await channel.reply(
        parsed,
        f"[APPROVAL_REQUIRED]\n"
        f"确认删除项目 {name}？\n"
        f"回复 同意 / Y 执行，其他视为取消。",
    )


async def apply_pending(
    parsed: ParsedMsg, pending: agent_pending.Pending, cfg: dict,
    channel: ChannelAdapter, ctx: SchedulerContext,
) -> None:
    """Run the agreed-on action after approval. Called from scheduler
    reply branch."""
    try:
        if pending.action == "alert_remove":
            config_writer.remove_alert_chat(
                ctx.config_path,
                chat_id=pending.payload["chat_id"],
                backup_dir=ctx.backup_dir,
            )
            _reload_cfg_in_place(ctx, cfg)
            await ctx.restart_alert_polling()
            await channel.reply(
                parsed,
                f"✅ 已摘除 {pending.payload['chat_id']}",
            )
            return

        if pending.action == "alert_register":
            config_writer.add_alert_chat(
                ctx.config_path,
                chat_id=pending.payload["chat_id"],
                project=pending.payload["project"],
                backup_dir=ctx.backup_dir,
            )
            _reload_cfg_in_place(ctx, cfg)
            await ctx.restart_alert_polling()
            chats = (cfg.get("alert_resolver") or {}).get("alert_chats") or []
            await channel.reply(
                parsed,
                f"✅ 注册成功，已开始监听（alert_chats 现 {len(chats)} 个）",
            )
            return

        if pending.action == "project_add":
            config_writer.add_project(
                ctx.config_path,
                name=pending.payload["name"],
                work_dir=pending.payload["work_dir"],
                chat_id=pending.payload["chat_id"],
                backup_dir=ctx.backup_dir,
            )
            _reload_cfg_in_place(ctx, cfg)
            await ctx.restart_consume()
            await channel.reply(
                parsed,
                f"✅ 已新增项目 {pending.payload['name']}，开始监听",
            )
            return

        if pending.action == "project_rm":
            config_writer.remove_project(
                ctx.config_path,
                name=pending.payload["name"],
                backup_dir=ctx.backup_dir,
            )
            _reload_cfg_in_place(ctx, cfg)
            await ctx.restart_consume()
            await channel.reply(
                parsed,
                f"✅ 已删除项目 {pending.payload['name']}",
            )
            return
    except config_writer.ConfigWriteError as e:
        log.warning("agent_admin.apply_pending failed: %s", e)
        await channel.reply(
            parsed,
            f"⚠️ 配置写入失败：{e}；已 rollback，状态未变更",
        )
    except Exception as e:
        log.exception("agent_admin.apply_pending crashed: %s", e)
        await channel.reply(parsed, f"⚠️ 写入异常：{e!r}")


def _reload_cfg_in_place(ctx: SchedulerContext, cfg: dict) -> None:
    """Replace the top-level cfg keys in place so callers holding the
    same dict reference observe the new values.

    Caveat: ``cfg.clear() + cfg.update(new)`` is *shallow* — any caller
    that captured a NESTED reference (e.g. `alert_cfg = cfg["alert_resolver"]`
    at startup) still points to the OLD nested dict. Two implications:

    1. Code paths that look up `cfg["alert_resolver"]` per use (e.g.
       `is_alert_message(parsed, cfg["alert_resolver"])`) are fine.
    2. Long-lived loops that captured a nested ref must be restarted —
       this is why ``ctx.restart_alert_polling()`` is mandatory after
       reload: the polling task captures alert_cfg at create time and
       cancel/recreate is the only way to make it pick up new chats.

    If we ever want to support hot-reload without restarting polling,
    switch to a deep merge here — but that breaks identity for any
    consumer that compared by `is`.
    """
    from runtime.config import load_config
    new_cfg = load_config(ctx.config_path)
    cfg.clear()
    cfg.update(new_cfg)
    ctx.cfg = cfg
