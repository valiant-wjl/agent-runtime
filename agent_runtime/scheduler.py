"""agent-runtime scheduler.

Full implementation for M2-T13: assembles all runtime components and
provides the main event loop.

Public API:
  handle_message(channel, parsed, project_name, project_cfg, runtime_cfg)
  consume(channel, projects, runtime_cfg, bot_mention_key)
  run_forever(cfg)
  main() -> int
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_runtime.channels import ChannelAdapter, ParsedMsg
from agent_runtime.channels.feishu.adapter import (
    ImageDownloadFailed,
    ImageTooLarge,
    StreamCardDegraded,
)
from agent_runtime.channels.feishu.poller import PollerCursor, poll_chat as _poll_chat_default
from agent_runtime.channels.feishu.stream_card import (
    Throttler,
    ToolUse,
    build_final_card,
    build_initial_card,
    build_progress_card,
)
from agent_runtime.channels.registry import load_channel
from agent_runtime import (
    agent_admin,
    agent_cmd,
    agent_pending,
    alert_cmd,
    alert_judge,
    alert_resolver,
    alert_retriever as alert_retriever_mod,
    approval,
    claude_proc,
    concurrency,
    config as config_mod,
    health,
    help_cmd,
    lesson,
    observability,
    push,
    routing,
    session,
    stream_card_metrics,
    verifier,
)
from agent_runtime.logging import setup_file_logging

log = logging.getLogger(__name__)

# Throttle self-push so a burst of alerts during an auth outage doesn't spam
# the operator's own DM. 30 min is short enough to remind on the next failure
# window after re-login, long enough to avoid notification fatigue.
_AUTH_PUSH_INTERVAL_S = 1800.0
_last_auth_push_at: float = 0.0

# Set by main() before run_forever(); consumed by run_forever to build the
# SchedulerContext.config_path needed by /agent commands (config_writer must
# know which file to edit). Module-level rather than an arg to run_forever
# because run_forever's signature is part of the public API and lots of
# unit tests still call it with just (cfg,).
_LOADED_CONFIG_PATH: str | None = None


def _apply_observability_config(cfg: dict) -> None:
    """Wire observability singleton at scheduler boot. Idempotent.

    Reads `cfg["observability"]` (filled with defaults by config loader);
    if absent, applies defensive defaults so trace emission still works
    in tests / minimal configs.
    """
    obs = cfg.get("observability") or {}
    observability.configure(
        trace_dir=obs.get("trace_dir", "./.state/traces"),
        enabled=obs.get("enabled", True),
    )


async def _notify_auth_failed() -> None:
    """Best-effort self-push when claude CLI auth is detected as broken.

    Throttled by _AUTH_PUSH_INTERVAL_S. Never raises. Skips silently if
    LARK_SELF_OPEN_ID is unset (push_to_self handles that case).
    """
    global _last_auth_push_at
    now = time.monotonic()
    if now - _last_auth_push_at < _AUTH_PUSH_INTERVAL_S:
        return
    _last_auth_push_at = now
    try:
        await push.push_to_self(claude_proc.AUTH_FAILED_TEXT)
    except Exception as e:  # defensive — push.push_to_self already swallows
        log.warning("auth-failed self-push raised: %r", e)


# Module-level CostTracker singleton (lazy-configured on first verifier use).
# Bound once at first use (lazy singleton); restart scheduler to pick up
# config changes. Tests reset via ``scheduler._cost_tracker = None``.
_cost_tracker: verifier.CostTracker | None = None


# Per-process set of work_dirs that have already had their image cache
# GC'd this run. Lazy first-touch sweep — keeps the .cache/images/ dir
# from accumulating orphaned subdirs after crash/SIGKILL between download
# and the try/finally cleanup in _handle_message_inner.
_gc_done_work_dirs: set[str] = set()

# Stale subdirs whose mtime is older than this are removed by the GC.
_IMAGE_CACHE_MAX_AGE_SECONDS = 86400


def _maybe_gc_image_cache(work_dir: str) -> None:
    """One-shot best-effort cleanup of stale image cache subdirs per work_dir.

    Idempotent: subsequent calls are O(1) set lookup. Any OSError is logged
    and swallowed — startup must never crash on a leftover .cache/ artefact.
    """
    if work_dir in _gc_done_work_dirs:
        return
    _gc_done_work_dirs.add(work_dir)
    cache_dir = Path(work_dir) / ".cache" / "images"
    if not cache_dir.is_dir():
        return
    cutoff = time.time() - _IMAGE_CACHE_MAX_AGE_SECONDS
    try:
        children = list(cache_dir.iterdir())
    except OSError as e:
        log.warning("image cache GC: list %s failed: %s", cache_dir, e)
        return
    for sub in children:
        try:
            if sub.is_dir() and sub.stat().st_mtime < cutoff:
                shutil.rmtree(sub, ignore_errors=True)
                log.info("image cache GC: removed stale %s", sub)
        except OSError as e:
            log.warning("image cache GC: %s: %s", sub, e)


async def _handle_alert_command(
    parsed: ParsedMsg,
    project_cfg: dict,
    alert_cfg: dict | None,
    channel: ChannelAdapter,
) -> None:
    """Handle the `/alert <text>` test entry.

    Resolution order, all branches → one debug-prefixed reply, then return:
      1. body empty → usage hint
      2. alert_resolver disabled → "未启用"
      3. alert_chats empty → "alert_chats 为空"
      4. retriever returns no candidates → "未找到候选"
      5. judge raises → "judge 异常: <err>"
      6. judge miss / no rewritten → "未命中" + candidate list for debugging
      7. judge hit → "🧪 命中 <id> ... dry-run 不计数 ..." + rewritten conclusion

    The command never calls mark_hit / sink_after_deep / claude_proc.run
    so it cannot pollute kb or burn deep-investigation Claude quota. It
    is purely a window into resolver behaviour for the current kb state.
    """
    body = alert_cmd.parse_alert(parsed.text)
    if not body:
        await channel.reply(
            parsed,
            "用法：`/alert <告警原文>` — 测试 alert resolver "
            "（不修改 kb，不跑深度排查）",
        )
        return

    if not (alert_cfg and alert_cfg.get("enabled")):
        await channel.reply(parsed, "🧪 /alert: alert_resolver 未启用")
        return

    chats = alert_cfg.get("alert_chats") or []
    if not chats:
        await channel.reply(parsed, "🧪 /alert: alert_chats 为空，无目标 kb")
        return

    target_chat_id = chats[0].get("chat_id")
    kb = alert_resolver.make_kb(project_cfg["work_dir"])
    retriever = _build_alert_retriever(alert_cfg, kb)
    candidates = retriever.search(
        chat_id=target_chat_id,
        alert_text=body,
        top_k=int(alert_cfg.get("top_k", 3)),
    )
    if not candidates:
        await channel.reply(
            parsed,
            f"🧪 /alert 未找到候选（kb 空或全过期）\n目标 kb: {target_chat_id}",
        )
        return

    judge_model = alert_cfg.get("judge_model") or project_cfg.get("model")
    judge_timeout = int(alert_cfg.get("judge_timeout", 60))
    try:
        result = await alert_judge.judge(
            alert_text=body,
            candidates=candidates,
            model=judge_model,
            timeout=judge_timeout,
            work_dir=project_cfg["work_dir"],
        )
    except Exception as e:
        await channel.reply(parsed, f"🧪 /alert judge 异常: {e!r}")
        return

    if result is None or not result.is_match:
        cand_summary = ", ".join(
            f"{c.entry.id} ({c.score:.2f})" for c in candidates[:3]
        )
        reason = (result.reason if result else "candidates empty")
        await channel.reply(
            parsed,
            "🧪 /alert 未命中（judge 不认为同类）\n"
            f"候选: {cand_summary}\n"
            f"原因: {reason}",
        )
        return

    matched = next(
        (c.entry for c in candidates if c.entry.id == result.matched_id), None,
    )
    if matched is None:
        await channel.reply(
            parsed,
            f"🧪 /alert: judge 返回的 matched_id={result.matched_id!r} "
            "不在候选集，已忽略",
        )
        return

    await channel.reply(
        parsed,
        f"🧪 /alert 命中 {matched.id} (hit_count={matched.hit_count}, "
        "dry-run 不计数 / 不写 kb)\n\n"
        f"{result.rewritten_conclusion}",
    )


def _build_alert_retriever(alert_cfg: dict, kb) -> alert_retriever_mod.Retriever:
    """Construct the configured retriever (US-007). Defaults to keyword.

    EmbeddingRetriever is an M2 placeholder that raises on __init__ — config
    validation already ensures the value is in {keyword, embedding}, so the
    fallthrough is unreachable in practice.
    """
    name = alert_cfg.get("retriever", "keyword")
    ttl = int(alert_cfg.get("ttl_days", 14)) * 86400
    if name == "embedding":
        return alert_retriever_mod.EmbeddingRetriever(kb=kb, ttl_seconds=ttl)
    return alert_retriever_mod.KeywordRetriever(kb=kb, ttl_seconds=ttl)


def _thread_key(parsed: ParsedMsg) -> str:
    """Single source of truth for session / approval / verifier-budget keying.

    Topic-group messages (chat_mode=thread) isolate by ``topic_id`` so a
    topic's state can't leak across topics in the same parent chat —
    critical for approval state (a stale "确认" from another topic must
    not match here) and for verifier rate-limit buckets. Falls back to
    the reply-chain root for non-topic chats.
    """
    return parsed.topic_id or parsed.thread_root_id


@dataclass
class SchedulerContext:
    """Mutable bag threaded through handle_message → inner_impl so /agent
    write commands can mutate cfg + restart polling.

    cfg is the SAME dict instance the scheduler started with; mutating
    via cfg.clear() + cfg.update(new) is visible to every per-turn code
    path that reads cfg (alert_resolver.is_alert_message, routing.route).
    Long-lived loops that captured a NESTED ref (alert polling task)
    must be restarted via restart_alert_polling() — see
    runtime/agent_admin.py:_reload_cfg_in_place docstring.

    restart_alert_polling_fn is wired in run_forever; None in unit tests
    that don't exercise the polling task lifecycle (call sites tolerate
    None via the restart_alert_polling() wrapper below).
    """

    cfg: dict
    config_path: Path
    backup_dir: Path
    restart_alert_polling_fn: Callable[[], Awaitable[None]] | None = None
    restart_consume_fn: Callable[[], Awaitable[None]] | None = None

    async def restart_alert_polling(self) -> None:
        if self.restart_alert_polling_fn is not None:
            await self.restart_alert_polling_fn()

    async def restart_consume(self) -> None:
        if self.restart_consume_fn is not None:
            await self.restart_consume_fn()


# ---------------------------------------------------------------------------
# Per-turn observability — turn_summary structured log line
# ---------------------------------------------------------------------------


@dataclass
class _TurnSummary:
    """Per-turn metrics collected by _handle_message_inner.

    Fields are intentionally flat (no nested dicts) so the emit helper can
    serialize them as space-separated ``key=value`` tokens that are trivial
    to grep / regex from a log file. ``-1`` denotes 'stage did not run' so
    every record has the same column shape regardless of branch.
    """

    msg_id: str
    chat_id: str
    branch: str = "unknown"
    is_alert: bool = False
    history_ms: int = -1
    image_ms: int = -1
    read_ms: int = -1
    exit_code: int = 0
    timed_out: bool = False
    text_len: int = 0
    # Full bot reply text — copied onto the OTel turn span as
    # digital_agent.text (≤3000 chars) so observer's judge can read what
    # bot actually said. Distinct from text_len which is the existing
    # log-line counter (kept for backward-compat with grep tooling).
    reply_text: str = ""
    tool_count: int = 0
    # Token accounting + model id; surface on OTel span as gen_ai.usage.*
    # and gen_ai.request.model. Both buffered and stream paths fill this.
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    model: str | None = None
    card_msg_id_set: bool = False
    card_degraded: bool = False
    # Surfaces the lbp-growth-agent "stuck 🔄 分析中..." root cause: the
    # FINAL update_card after the stream completes can return False
    # (single transient lark-cli failure not reaching the 3-streak that
    # raises StreamCardDegraded). Before US-003 this was swallowed and the
    # card stayed at progress state; now we fall back to channel.reply
    # AND record the event here for ops grep.
    final_card_update_failed: bool = False


def _emit_turn_summary(s: _TurnSummary, total_ms: int) -> None:
    """Emit one INFO line summarising the turn. Best-effort; never raises."""
    try:
        log.info(
            "turn_summary "
            "msg_id=%s chat_id=%s branch=%s is_alert=%s "
            "total_ms=%d history_ms=%d image_ms=%d read_ms=%d "
            "exit_code=%d timed_out=%s text_len=%d tool_count=%d "
            "tokens_in=%d tokens_out=%d model=%s "
            "card_msg_id_set=%s card_degraded=%s final_card_update_failed=%s",
            s.msg_id, s.chat_id, s.branch, "true" if s.is_alert else "false",
            total_ms, s.history_ms, s.image_ms, s.read_ms,
            s.exit_code, "true" if s.timed_out else "false",
            s.text_len, s.tool_count,
            s.usage_input_tokens, s.usage_output_tokens, s.model or "-",
            "true" if s.card_msg_id_set else "false",
            "true" if s.card_degraded else "false",
            "true" if s.final_card_update_failed else "false",
        )
    except Exception:
        # Logging must never break the handler.
        pass


# ---------------------------------------------------------------------------
# handle_message (public) — wraps _handle_message_inner with semaphores
# ---------------------------------------------------------------------------


async def handle_message(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_name: str,
    project_cfg: dict,
    runtime_cfg: dict,
    features_cfg: dict | None = None,
    *,
    alert_cfg: dict | None = None,
    scheduler_ctx: "SchedulerContext | None" = None,
) -> None:
    """Main message handler: concurrency gate → supported check → approval →
    session → claude → reply.

    Acquires global semaphore + per-chat semaphore before delegating to
    _handle_message_inner. Tests that bypass semaphores should call
    _handle_message_inner directly (or call concurrency.init_global first).

    ``features_cfg`` comes from top-level ``cfg["features"]`` (per spec L285,
    features lives at config TOP-LEVEL, not under runtime). Defaults to ``{}``
    so legacy callers in tests still work.

    ``alert_cfg`` is the top-level ``cfg["alert_resolver"]`` dict (US-007).
    None / disabled → resolver branch is skipped, behaviour unchanged.

    ``scheduler_ctx`` is the SchedulerContext for /agent admin commands
    (cfg mutation + polling restart). None in unit tests that bypass the
    /agent path — the dispatch branch logs+returns when it sees None.
    """
    if features_cfg is None:
        features_cfg = {}
    per_chat_limit = runtime_cfg.get("per_chat_concurrent", 2)
    async with concurrency.global_sem():
        async with concurrency.chat_sem(parsed.chat_id, per_chat_limit):
            await _handle_message_inner(
                channel, parsed, project_name, project_cfg, runtime_cfg,
                features_cfg, alert_cfg=alert_cfg,
                scheduler_ctx=scheduler_ctx,
            )


async def _handle_message_inner(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_name: str,
    project_cfg: dict,
    runtime_cfg: dict,
    features_cfg: dict | None = None,
    *,
    alert_cfg: dict | None = None,
    scheduler_ctx: "SchedulerContext | None" = None,
) -> None:
    """Public entry: wraps _handle_message_inner_impl with the turn_summary
    finally so every turn (including early returns and exceptions) emits
    exactly one structured observability log line.

    Insertion point #1 (observer spec § 3.7): each turn is also wrapped in
    an OTel turn span, so observer can read the same lifecycle from
    .state/traces/YYYY-MM.jsonl. Attributes are filled from _TurnSummary
    after inner returns so we capture branch / exit_code / text_len / etc.
    in a single source of truth.
    """
    summary = _TurnSummary(
        msg_id=parsed.message_id,
        chat_id=parsed.chat_id,
    )
    turn_start = time.monotonic()
    # is_alert is computed inside impl; start the span with a provisional
    # value and overwrite from summary in the finally block.
    # alert_text is the user-visible source text (alert body for bot-posted
    # alerts, or user question for normal turns); observer's judge reads it.
    async with observability.start_turn_span(
        chat_id=parsed.chat_id,
        msg_id=parsed.message_id,
        is_alert=False,
    ) as turn_span:
        # Always emit the source text on the way in (≤3000 chars to bound
        # span size; full alert / question text is the SOURCE for observer
        # judge's "did bot dodge the question?" check).
        turn_span.set_attribute(
            "digital_agent.alert_text", (parsed.text or "")[:3000],
        )
        try:
            await _handle_message_inner_impl(
                channel, parsed, project_name, project_cfg, runtime_cfg,
                features_cfg, alert_cfg=alert_cfg,
                scheduler_ctx=scheduler_ctx, summary=summary,
            )
        except asyncio.CancelledError:
            if summary.branch == "unknown":
                summary.branch = "cancelled"
            turn_span.set_status("ERROR")
            raise
        except Exception:
            turn_span.set_status("ERROR")
            raise
        finally:
            # Reflect summary fields onto the span (overwrites provisional
            # is_alert with the actual computed value). reply_text comes
            # from summary if the impl stashed it (M6 stream path stores
            # the assembled text on summary.reply_text).
            turn_span.set_attribute("digital_agent.is_alert", summary.is_alert)
            turn_span.set_attribute("digital_agent.branch", summary.branch)
            turn_span.set_attribute("digital_agent.exit_code", summary.exit_code)
            turn_span.set_attribute("digital_agent.timed_out", summary.timed_out)
            turn_span.set_attribute("digital_agent.text_len", summary.text_len)
            turn_span.set_attribute("digital_agent.tool_use_count", summary.tool_count)
            # Bot reply full text (≤3000 chars). observer's hallucination
            # rule needs to compare bot claims against tool_use sequence —
            # without this attribute the judge was operating blind.
            reply_text = getattr(summary, "reply_text", "") or ""
            turn_span.set_attribute(
                "digital_agent.text", reply_text[:3000],
            )
            # gen_ai.* attrs — emit once here so the buffered path is
            # covered too (stream path also emits inline in the final
            # event handler, but only fires when stream_card is enabled).
            if summary.usage_input_tokens or summary.usage_output_tokens:
                turn_span.set_attribute(
                    "gen_ai.usage.input_tokens", summary.usage_input_tokens,
                )
                turn_span.set_attribute(
                    "gen_ai.usage.output_tokens", summary.usage_output_tokens,
                )
                turn_span.set_attribute("gen_ai.system", "anthropic")
            if summary.model:
                turn_span.set_attribute(
                    "gen_ai.request.model", summary.model,
                )
            total_ms = int((time.monotonic() - turn_start) * 1000)
            _emit_turn_summary(summary, total_ms)


async def _handle_message_inner_impl(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_name: str,
    project_cfg: dict,
    runtime_cfg: dict,
    features_cfg: dict | None = None,
    *,
    alert_cfg: dict | None = None,
    scheduler_ctx: "SchedulerContext | None" = None,
    summary: _TurnSummary,
) -> None:
    """Inner handler: no semaphore held. Called by _handle_message_inner.

    Flow (per spec §6.1):
    1. Check message type via raw_event; if not supported → reply; return
    2. approval.get(thread_root_id):
       - EXECUTING state → reply "执行中"; return
       - other states → handle_reply branch
    3. session.get(thread_root_id) → session_id for --resume
    4. channel.send_card "🔄 分析中..."
    5. claude_proc.run (read phase)
    6. parse_approval_block → if block: create approval + send card
       else: channel.reply with result text
    7. session.put only on exit_code==0 and not timed_out

    ``features_cfg`` comes from top-level ``cfg["features"]`` (per spec L285).
    """
    if features_cfg is None:
        features_cfg = {}
    # Lazy startup GC of stale image cache (no-op after first call per work_dir).
    _maybe_gc_image_cache(project_cfg.get("work_dir", ""))
    # Topic group (chat_mode=thread) isolates session/approval per topic.
    # See ``_thread_key`` docstring for rationale.
    thread_key = _thread_key(parsed)
    admin_users = project_cfg.get("admin_users") or []

    # 0.4) /help — zero-cost menu listing every DM-usable slash command.
    # Intercepted before /agent so a user typing /help in any chat (DM or
    # group, admin or not) always gets the unified menu, never a "permission
    # denied" reply. See runtime/help_cmd.py for the rendered text.
    if help_cmd.is_help_command(parsed.text):
        summary.branch = "help"
        await channel.reply(parsed, help_cmd.render_help())
        return

    # 0.5) /agent <subcommand> — DM/group admin commands. Intercepted before
    # /alert because the prefixes don't overlap; ordering is for branch-name
    # predictability only.
    if agent_cmd.is_agent_command(parsed.text):
        summary.branch = "agent_command"
        if scheduler_ctx is None:
            log.warning(
                "agent_command received but scheduler_ctx is None; skipping "
                "(test path without scheduler_ctx — production always sets it)",
            )
            return
        await agent_admin.dispatch(
            parsed, project_cfg, scheduler_ctx.cfg, channel, scheduler_ctx,
        )
        return

    # 0.9) /alert <text> — alert resolver test entry. Lets a developer try
    # the resolver path from any chat (DM included) without firing a real
    # webhook. Test-entry semantics: NO mark_hit, NO sink, NO deep
    # investigation — surface retriever + judge behaviour visibly.
    if alert_cmd.is_alert_command(parsed.text):
        summary.branch = "alert_command"
        await _handle_alert_command(parsed, project_cfg, alert_cfg, channel)
        return

    # 1.0) Alert resolver (US-007 + polling fallback): bot-posted messages
    # in alert chats are an INDEPENDENT path — they're not user @-mentions,
    # so the project's supported_msg_types whitelist does not apply (alert
    # cards are msg_type=interactive, which the whitelist does not include).
    # Run alert resolver FIRST so this path is never blocked by the
    # downstream user-message gates.
    alert_kb = None
    is_alert_turn = False
    if alert_cfg and alert_cfg.get("enabled"):
        is_alert, _route = alert_resolver.is_alert_message(parsed, alert_cfg)
        if is_alert:
            is_alert_turn = True
            summary.is_alert = True
            alert_kb = alert_resolver.make_kb(project_cfg["work_dir"])
            try:
                hit = await alert_resolver.try_handle_alert_hit(
                    parsed, project_cfg, alert_cfg, channel,
                    kb=alert_kb,
                    retriever=_build_alert_retriever(alert_cfg, alert_kb),
                )
            except Exception:
                # Fail-open: a resolver crash must not block the message —
                # fall through to deep investigation. We keep ``alert_kb``
                # set so the conclusion still lands in the kb.
                log.exception("alert_resolver crashed; falling through to deep path")
                hit = False
            if hit:
                # Reply already sent by the resolver. No session put,
                # no approval, no claude_proc — same shape as /lesson.
                summary.branch = "alert_hit"
                return

    # 1) supported msg types — default includes "image" so picture messages
    # flow into the multimodal-via-Read path; project config can still
    # narrow this list to disable image support.
    # Alert turns bypass this check: an alert card's raw msg_type is
    # `interactive` (not in the user-msg whitelist), but the poller has
    # already normalised parsed.text to plain text — the supported list
    # is a user-message gate, not applicable to bot-posted alerts.
    supported = project_cfg.get("supported_msg_types", ["text", "post", "image"])
    raw_msg_type = (
        (parsed.raw_event or {})
        .get("event", {})
        .get("message", {})
        .get("message_type", "text")
    )
    if raw_msg_type not in supported and not is_alert_turn:
        summary.branch = "unsupported"
        reject = project_cfg.get("unsupported_msg_reply") or "暂不支持此消息类型"
        await channel.reply(parsed, reject)
        return

    # 1.5) /lesson <content> slash command — Tier 1 self-improvement loop.
    # Intercepted before approval/claude so feedback is zero-cost and
    # deterministic. See runtime/lesson.py for the file format.
    if lesson.is_lesson_command(parsed.text):
        summary.branch = "lesson"
        body = lesson.parse_lesson(parsed.text)
        if body is None:
            await channel.reply(
                parsed,
                "用法：`/lesson <内容>` — 把要纠正/记住的事写下来。\n"
                "例：`/lesson 自我介绍 ≤ 3 句`",
            )
            return
        try:
            lesson.append_lesson(Path(project_cfg["work_dir"]), body)
        except Exception as e:
            log.exception("lesson.append_lesson failed: %s", e)
            await channel.reply(parsed, f"⚠️ 记录失败：{e}")
            return
        # Truncate echo so a giant lesson doesn't spam the chat.
        echo = body if len(body) <= 80 else body[:77] + "..."
        await channel.reply(parsed, f"✅ 已记下：{echo}")
        return

    # 1.8) /agent pending confirmation reply. Must precede the approval
    # branch: agent_pending and approval are parallel stores; we drain
    # agent replies first so an unrelated pending approval can't swallow
    # them. NOTE: agent_pending uses its OWN keying (chat_id:sender_id —
    # not scheduler thread_key) so plain "同意" replies match without
    # requiring the user to use feishu's "回复" gesture. See
    # runtime/agent_pending.py module docstring.
    agent_thread_key = agent_pending.thread_key(parsed)
    pending_agent = agent_pending.get(agent_thread_key)
    if pending_agent is not None:
        res = agent_pending.handle_reply(
            agent_thread_key, parsed.sender_id, parsed.text,
        )
        if res.action == "approved":
            summary.branch = "agent_command_apply"
            if scheduler_ctx is None:
                # In production this branch is unreachable — run_forever always
                # constructs a SchedulerContext. Hitting it means an admin
                # approval is being silently dropped, which is worse than a
                # warning; surface it as error so prod logs scream.
                log.error(
                    "agent_pending approved but scheduler_ctx is None; "
                    "approval silently dropped (likely a test path that "
                    "exercised the reply branch without wiring a ctx)",
                )
                return
            await agent_admin.apply_pending(
                parsed, res.pending, scheduler_ctx.cfg, channel, scheduler_ctx,
            )
            return
        if res.action == "cancelled":
            summary.branch = "agent_command_cancel"
            await channel.reply(parsed, "操作已取消")
            return
        if res.action == "ignored":
            log.info(
                "agent_pending reply ignored (non-admin or unrelated): "
                "thread=%s sender=%s", agent_thread_key, parsed.sender_id,
            )
            return
        # action == "unrelated" → fall through to approval branch below

    # 2) approval reply branch
    existing = approval.get(thread_key)
    if existing is not None:
        # All paths under this block (except the "unrelated" fall-through
        # below) terminate as branch=approval_reply. Tag once here and let
        # the fall-through reset to "deep" later.
        summary.branch = "approval_reply"
        # Write phase in progress — do not start a new read phase
        if existing.state == approval.State.EXECUTING:
            await channel.reply(parsed, "上一个操作正在执行中，请稍候...")
            return

        result = approval.handle_reply(
            thread_key, parsed.sender_id, parsed.text, admin_users
        )
        if result.action == "approved":
            await _execute_write_phase(channel, parsed, result.approval, project_cfg)
            return
        elif result.action == "cancelled":
            await channel.reply(parsed, "操作已取消")
            approval.remove(thread_key)
            return
        elif result.action == "retry":
            await _execute_write_phase(channel, parsed, result.approval, project_cfg)
            return
        elif result.action == "ignored":
            log.info(
                "approval reply ignored: thread=%s sender=%s reason=%s",
                thread_key,
                parsed.sender_id,
                result.reason,
            )
            # Give the sender actionable feedback instead of silence.
            if result.reason == "needs_admin":
                # 线上 write — name the required approver via a real @mention
                # so the admin is pinged (card lark_md renders <at id=...>).
                await channel.send_card(parsed, {"fallback_text": (
                    f"⚠️ 这是**线上写操作**，仅管理员可批准，你的「确认」无效。\n"
                    f"请 {_admin_at_mentions(admin_users)} 确认。"
                )})
            elif result.reason == "permission":
                await channel.reply(
                    parsed, "你不是该操作的发起人，也不是管理员，无权批准/取消。"
                )
            # "bad_state" → stay silent (already terminal / executing)
            return
        # "unrelated" → fall through to normal read phase

    # 3) Normal read-phase flow
    summary.branch = "deep"
    sess = session.get(thread_key)
    session_id = sess["session_id"] if sess else None

    # 3a) Topic context: ALWAYS fetch recent topic history when topic_id is
    # set, and always inject into prompt. Reason: a first-touch-only
    # heuristic breaks if turn 1 is an empty @-mention (placeholder session
    # gets created with no real history baked in; subsequent turns then
    # never see the topic context). Always-inject + Claude --resume is
    # mildly redundant but Claude handles repeated context fine, and
    # `topic_history_limit` (default 20) keeps token cost bounded.
    topic_history: list[str] = []
    if parsed.topic_id:
        _history_start = time.monotonic()
        try:
            topic_history = await channel.fetch_topic_history(
                parsed.topic_id,
                limit=int(project_cfg.get("topic_history_limit", 20)),
            )
        except Exception:
            log.exception("fetch_topic_history failed; continuing without history")
        # Prepend thread anchor: Feishu's threads-messages-list excludes
        # the message the thread was started from (thread_message_position
        # == -1). For alarm-driven topics that root *is* the alarm card —
        # without it the bot has no incident context. Skip when root_id
        # equals current message_id (we are the root) or fetch fails.
        if (
            parsed.thread_root_id
            and parsed.thread_root_id != parsed.message_id
            and not any(parsed.thread_root_id in h for h in topic_history)
        ):
            try:
                root_text = await channel.fetch_message_text(parsed.thread_root_id)
            except Exception:
                log.exception("fetch_message_text(thread root) failed; continuing")
                root_text = None
            if root_text:
                topic_history.insert(0, root_text)
        summary.history_ms = int((time.monotonic() - _history_start) * 1000)

    # 3b) Image multimodal: download each attached image into
    # <work_dir>/.cache/images/<msg_id>/ and inject absolute paths into
    # the prompt so Claude can pick them up via the Read tool.
    _image_start = time.monotonic()
    image_results = await _download_message_images(channel, parsed, project_cfg)
    summary.image_ms = int((time.monotonic() - _image_start) * 1000)

    # 3c) Compose augmented prompt. Order: user text inner, image header
    # wraps text, topic history wraps everything (broader context first
    # when read top-down).
    # Empty-prompt guard: bare `@bot` with no text, no images, and no
    # topic history would assemble to "" → claude --print rejects with
    # "Input must be provided". Substitute a placeholder so the agent
    # still gets to respond in persona.
    text_for_prompt = parsed.text
    if not text_for_prompt.strip() and not image_results and not topic_history:
        text_for_prompt = (
            "（用户 @ 了你但没有发送具体文字内容。请简短问候并询问需要什么帮助，"
            "不要假装看到了不存在的内容。）"
        )
        log.info(
            "empty prompt guard fired: msg=%s — using placeholder text",
            parsed.message_id,
        )
    prompt = _build_read_prompt(text_for_prompt, image_results, topic_history)

    # 4+5) Run read phase: streaming card path (M6) or legacy buffered path.
    # We branch on channels.feishu.stream_card.enabled. Test fixtures that
    # don't set this fall through to the buffered path, preserving M2/M7
    # behaviour. Production config.example.yaml sets enabled: true.
    _read_start = time.monotonic()
    try:
        if _stream_card_enabled(channel, runtime_cfg):
            result = await _run_read_stream(
                channel, parsed, project_cfg, runtime_cfg, session_id,
                prompt=prompt,
            )
        else:
            read_phase = project_cfg["read_phase"]
            result = await _run_read_buffered(
                channel, parsed, project_cfg, runtime_cfg, session_id, read_phase,
                prompt=prompt,
            )
    finally:
        # Capture read-phase timing + observable result fields BEFORE the
        # cleanup helper runs so a leftover-cache OSError can't drop the
        # turn_summary metrics.
        summary.read_ms = int((time.monotonic() - _read_start) * 1000)
        _result_obj = locals().get("result")
        if _result_obj is not None:
            summary.exit_code = int(getattr(_result_obj, "exit_code", 0))
            summary.timed_out = bool(getattr(_result_obj, "timed_out", False))
            summary.text_len = len(getattr(_result_obj, "text", "") or "")
            summary.reply_text = str(getattr(_result_obj, "text", "") or "")
            summary.tool_count = int(getattr(_result_obj, "_tool_count", 0))
            # Token usage + model (populated by both run() and run_stream
            # paths into RunResult / the stream final event handler).
            summary.usage_input_tokens = int(
                getattr(_result_obj, "usage_input_tokens", 0) or 0,
            )
            summary.usage_output_tokens = int(
                getattr(_result_obj, "usage_output_tokens", 0) or 0,
            )
            summary.model = getattr(_result_obj, "model", None)
            summary.card_msg_id_set = bool(getattr(_result_obj, "_card_msg_id", None))
            if getattr(_result_obj, "_card_degraded_mid_stream", False):
                summary.card_degraded = True
        # Best-effort cleanup of downloaded image cache files. Failures are
        # logged warnings — leftover cache is non-fatal (see startup GC).
        _cleanup_image_paths(image_results)

    # 6) Check for [APPROVAL_REQUIRED]
    info = approval.parse_approval_block(result.text)
    if info is not None:
        approval.create(
            thread_key=thread_key,
            agent_name=project_name,
            info=info,
            sender_id=parsed.sender_id,
            admin_users=admin_users,
            approval_timeout=project_cfg.get("approval_timeout", 1800),
        )
        card_text = _build_approval_card_text(info, admin_users)
        # If the stream path produced an in-flight progress card, flip it
        # to a terminal "审批中" state before sending the approval card —
        # otherwise the user sees a stuck "🔄 分析中..." card next to the
        # approval card. Best-effort; ignore failures.
        stream_card_msg_id = getattr(result, "_card_msg_id", None)
        if stream_card_msg_id:
            try:
                await channel.update_card(
                    stream_card_msg_id,
                    build_final_card(
                        "_⏸ 已生成审批申请，等待批准_",
                        {
                            "elapsed_s": getattr(result, "_elapsed_s", 0.0),
                            "tool_count": getattr(result, "_tool_count", 0),
                            "template": "orange",
                        },
                    ),
                )
            except StreamCardDegraded:
                stream_card_metrics.bump_throttled()
            except Exception:
                log.exception("flipping stream card to approval-pending failed")
        # MVP: approval card uses placeholder shape via send_card so the
        # existing M2 tests (which assert call_count >= 2 and inspect
        # ``fallback_text``) remain green. Real approval interactive card
        # design is post-MVP.
        await channel.send_card(parsed, {"fallback_text": card_text})
    else:
        # 6b) Verifier (M7-T03): optionally second-pair-of-eyes the draft.
        # Failure of the verifier path must NEVER block the user reply.
        try:
            final_text = await _maybe_verify(
                channel=channel,
                parsed=parsed,
                project_cfg=project_cfg,
                runtime_cfg=runtime_cfg,
                features_cfg=features_cfg,
                question=parsed.text,
                draft=result.text,
            )
        except Exception:
            log.exception("verifier path crashed; falling back to raw draft")
            final_text = result.text + "\n\n_⚠️ verifier 未跑通_"

        # 6c) Alert kb sink (US-007): when this turn was an alert that
        # missed retrieval, persist (alert_text, conclusion) for next time.
        # Only sink stable conclusions: exit 0, not timed out. The
        # ``[APPROVAL_REQUIRED]`` branch is the sibling ``if info is not
        # None`` above — being in this ``else`` already means no approval
        # is pending.
        if (
            alert_kb is not None
            and result.exit_code == 0
            and not result.timed_out
        ):
            await alert_resolver.sink_after_deep(parsed, final_text, kb=alert_kb)

        # If the stream path produced a card, finalise it; otherwise plain reply.
        card_msg_id = getattr(result, "_card_msg_id", None)
        if card_msg_id:
            stats = {
                "elapsed_s": getattr(result, "_elapsed_s", 0.0),
                "tool_count": getattr(result, "_tool_count", 0),
                "template": "green" if result.exit_code == 0 and not result.timed_out else "red",
            }
            try:
                update_ok = await channel.update_card(
                    card_msg_id, build_final_card(final_text, stats),
                )
            except StreamCardDegraded:
                summary.card_degraded = True
                summary.final_card_update_failed = True
                log.warning("final update_card degraded; falling back to text reply")
                stream_card_metrics.bump_throttled()
                await channel.reply(parsed, final_text)
            else:
                # update_card returns False on a SINGLE transient lark-cli
                # failure (the streak counter only raises once it hits
                # _max_update_failures). Pre-US-003 we ignored that False
                # — the card stayed at "🔄 分析中..." forever. Now: fall
                # back to text reply so the user gets the answer either
                # way, and surface the event in turn_summary so ops can
                # see the lark-cli flake.
                if update_ok is False:
                    summary.card_degraded = True
                    summary.final_card_update_failed = True
                    log.warning(
                        "final update_card returned False; falling back to text reply"
                    )
                    stream_card_metrics.bump_throttled()
                    await channel.reply(parsed, final_text)
        else:
            # No card was ever attached (initial send_card failed or
            # streaming was disabled) — text reply is the only path.
            # summary.card_degraded was already set in the read-phase
            # finally when result._card_degraded_mid_stream is true.
            await channel.reply(parsed, final_text)

    # 7) Persist session only on successful completion
    if result.session_id and result.exit_code == 0 and not result.timed_out:
        session.put(thread_key, result.session_id, agent=project_name)


# ---------------------------------------------------------------------------
# Image multimodal + topic-history helpers
# ---------------------------------------------------------------------------


# Tuple per attached image: (image_key, downloaded_path_or_None, error_msg_or_None).
_ImageResult = tuple[str, Path | None, str | None]


async def _download_message_images(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_cfg: dict,
) -> list[_ImageResult]:
    """Download every attached image into ``<work_dir>/.cache/images/<msg_id>/``.

    Each entry of the returned list mirrors the order of
    ``parsed.image_keys``. Failures (download error / oversized) are
    reported in-band so the prompt can show partial successes — the read
    phase must NOT abort because one image was unreachable.
    """
    if not parsed.image_keys:
        return []
    work_dir = Path(project_cfg["work_dir"])
    # Per-message cache subdir; adapter.download_image creates it on first call.
    dest = work_dir / ".cache" / "images" / parsed.message_id
    max_bytes = int(project_cfg.get("image_max_bytes", 10_000_000))
    out: list[_ImageResult] = []
    for key in parsed.image_keys:
        try:
            path = await channel.download_image(
                message_id=parsed.message_id,
                image_key=key,
                dest_dir=dest,
                max_bytes=max_bytes,
            )
            out.append((key, path, None))
        except (ImageDownloadFailed, ImageTooLarge) as e:
            log.warning("download_image failed for %s: %s", key, e)
            out.append((key, None, str(e)))
        except Exception as e:
            log.exception("download_image crashed for %s", key)
            out.append((key, None, f"unexpected error: {e}"))
    return out


def _build_read_prompt(
    text: str,
    image_results: list[_ImageResult],
    topic_history: list[str],
) -> str:
    """Compose the read-phase prompt: text → image header → topic-history header.

    Order matters for what Claude reads top-down: topic history (broadest
    context) wraps image header which wraps the user's literal text.
    Empty inputs produce no header (clean prompt for plain text messages).
    """
    body = text
    if image_results:
        n = len(image_results)
        lines: list[str] = []
        for i, (key, path, err) in enumerate(image_results, 1):
            if path is not None:
                lines.append(f"- {path}")
            else:
                lines.append(
                    f"- [图片#{i} ({key})] 下载失败: {err or '未知错误'}"
                )
        body = (
            f"用户附带 {n} 张图片，请用 Read 工具查看后再回答：\n"
            + "\n".join(lines)
            + f"\n\n用户文本：{body}"
        )
    if topic_history:
        body = (
            "话题历史（按时间顺序）：\n"
            + "\n".join(topic_history)
            + f"\n\n当前用户消息：{body}"
        )
    return body


def _cleanup_image_paths(image_results: list[_ImageResult]) -> None:
    """Best-effort delete each successfully downloaded file. Logs on miss."""
    for _, path, _ in image_results:
        if path is None:
            continue
        try:
            Path(path).unlink(missing_ok=True)
        except OSError as e:
            log.warning("cleanup: failed to remove %s: %s", path, e)


# ---------------------------------------------------------------------------
# Read-phase implementations (buffered vs streaming)
# ---------------------------------------------------------------------------


def _stream_card_enabled(channel: ChannelAdapter, runtime_cfg: dict) -> bool:
    """Return True if this channel + config opts into the M6 streaming card path.

    Gate is intentionally conservative: only feishu, only when explicitly
    enabled. Test fixtures that omit the channels.feishu.stream_card section
    get the legacy buffered path — preserves all M2/M7 tests.
    """
    if getattr(channel, "name", None) != "feishu":
        return False
    return bool(
        ((runtime_cfg.get("channels") or {}).get("feishu") or {})
        .get("stream_card", {})
        .get("enabled", False)
    )


async def _run_read_buffered(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_cfg: dict,
    runtime_cfg: dict,
    session_id: str | None,
    read_phase: dict,
    *,
    prompt: str | None = None,
) -> claude_proc.RunResult:
    """Legacy buffered path: send placeholder card + claude_proc.run.

    Captures the placeholder card msg_id so the downstream finalisation
    path can flip it to a final card via update_card / build_final_card —
    parity with the stream path. Pre-fix, the placeholder stayed in chat
    forever and the user got the answer in a separate text reply.

    ``prompt`` defaults to ``parsed.text`` for backward compatibility;
    callers that prepend image / topic-history headers should pass the
    augmented string explicitly.
    """
    start = time.monotonic()
    card_msg_id: str | None = None
    try:
        card_msg_id = await channel.send_card(parsed, {"fallback_text": "🔄 分析中..."})
    except StreamCardDegraded as e:
        log.warning("buffered send_card failed; degrading to text reply: %s", e)
        stream_card_metrics.bump_throttled()
        card_msg_id = None

    result = await claude_proc.run(
        work_dir=project_cfg["work_dir"],
        prompt=parsed.text if prompt is None else prompt,
        timeout=runtime_cfg.get("reply_timeout", 300),
        session_id=session_id,
        disallowed_tools=read_phase["disallowed_tools"],
        disallowed_bash_patterns=read_phase.get("disallowed_bash_patterns"),
        model=project_cfg.get("model"),
        meta_work_dir=project_cfg.get("meta_work_dir"),
    )
    # Stash card-related state mirroring the stream path so the downstream
    # ``if card_msg_id:`` branch in _handle_message_inner_impl can flip the
    # placeholder to a final card uniformly. _tool_count=0: buffered mode
    # has no event stream.
    result._card_msg_id = card_msg_id  # type: ignore[attr-defined]
    result._elapsed_s = time.monotonic() - start  # type: ignore[attr-defined]
    result._tool_count = 0  # type: ignore[attr-defined]
    return result


# A "meta-closer" is a SHORT final assistant message that refers to an answer
# already delivered elsewhere ("已交付 / 结论不变 / 无需再… / 已纳入… / 如上所述")
# rather than containing the substance itself. Deliberately TIGHT: it does NOT
# match generic completion phrasing like a bare "已完成" / "分析完成", because a
# genuinely concise conclusion often opens with those — matching them would
# wrongly swap a good answer for earlier narration (the very bug the
# result-event fix removed). See _recover_superseded_answer.
_META_CLOSER_RE = re.compile(
    r"已(经)?[^，。；\n]{0,6}(交付|给出过|说过|回复过|答复过|提交过|发出去了)"
    r"|结论(不变|同上|如前|已(给出|交付|说明))"
    r"|无需(再|额外|进一步|更多)[^，。；\n]{0,4}(操作|确认|处理|补充|说明|回复|动作)"
    r"|不(再|用)(赘述|重复|展开)"
    r"|已纳入(证据|结论|分析)"
    r"|(同上|如上(所述)?|见上(文|述)?|如前(所述)?)"
    r"|already (delivered|provided|stated|answered|given)"
    r"|no (further|additional|more) (action|response|steps?)"
    r"|(see|as (stated|noted|explained|described)) above",
    re.IGNORECASE,
)


def _looks_like_meta_closer(text: str) -> bool:
    return bool(_META_CLOSER_RE.search(text or ""))


def _recover_superseded_answer(result_text: str, text_runs: list[str]) -> str:
    """The `result` event carries only the turn's LAST assistant message. When
    the model delivers its real answer mid-turn, keeps working (extra tool
    calls), then ends with a SHORT meta-acknowledgement that the answer was
    already given ("分析已完成并交付…结论不变。无需再操作"), that closer becomes
    result_text and the rich answer — which only streamed to the ephemeral
    progress card — is lost. Recover the richest earlier streamed text block.

    Conservative: only fires when the final message is BOTH short (<400 chars)
    AND matches a self-referential meta-closer pattern, and a substantially
    longer earlier block exists. A genuinely concise conclusion (no meta
    phrasing) is never touched, so this cannot reintroduce the narration-as-
    answer bug. ``text_runs`` are per-block (split on tool_use), so the
    recovered value is a single coherent assistant message — never a glued
    concatenation of inter-tool narration.
    """
    rt = (result_text or "").strip()
    if len(rt) >= 400 or not _looks_like_meta_closer(rt):
        return result_text
    candidates = [r.strip() for r in text_runs if r.strip() and r.strip() != rt]
    if not candidates:
        return result_text
    richest = max(candidates, key=len)
    if len(richest) >= 120 and len(richest) >= 1.5 * max(len(rt), 1):
        return richest
    return result_text


async def _run_read_stream(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_cfg: dict,
    runtime_cfg: dict,
    session_id: str | None,
    *,
    prompt: str | None = None,
) -> claude_proc.RunResult:
    """M6 streaming path: real initial card + throttled progress + final card.

    Returns a RunResult shaped like the buffered path so the rest of
    ``_handle_message_inner`` is identical, plus three private attributes:
      - ``_card_msg_id``: card id for final update (None if degraded)
      - ``_elapsed_s``:   total elapsed seconds (for final card stats)
      - ``_tool_count``:  total tool_use observations (for final card stats)

    Degrades to text mode (``_card_msg_id=None``) on any StreamCardDegraded.

    The progress card always shows ``parsed.text`` (the user-visible
    message); ``prompt`` (augmented with image / topic-history headers
    when present) is what reaches the LLM.
    """
    read_phase = project_cfg["read_phase"]
    stream_cfg = (
        ((runtime_cfg.get("channels") or {}).get("feishu") or {})
        .get("stream_card", {})
    )
    throttle_ms = int(stream_cfg.get("throttle_ms", 1000))
    throttle_calls = int(stream_cfg.get("throttle_tool_calls", 3))
    throttler = Throttler(min_ms=throttle_ms, max_calls=throttle_calls)

    # 1) Initial card
    start = time.monotonic()
    initial_card = build_initial_card(parsed.text, start)
    card_msg_id: str | None = None
    try:
        card_msg_id = await channel.send_card(parsed, initial_card)
    except StreamCardDegraded as e:
        log.warning("send_card failed; degrading to text mode: %s", e)
        stream_card_metrics.bump_throttled()
        card_msg_id = None

    # 2) Stream events
    events: list[ToolUse] = []
    pending_tool_count = 0
    answer_chunks: list[str] = []
    # Per-block text runs: a maximal run of consecutive text_deltas, flushed on
    # each tool_use. Lets _recover_superseded_answer compare distinct assistant
    # text blocks (e.g. a rich mid-turn answer vs. a short closing remark)
    # rather than one flat blob. ``answer_chunks`` stays the flat fallback.
    text_runs: list[str] = []
    cur_run: list[str] = []
    final_payload: dict = {}
    timed_out = False
    exit_code = 0
    # Observability counters — emitted in the finally below.
    first_event_at: float | None = None
    first_text_delta_at: float | None = None
    last_event_at: float | None = None
    text_delta_count = 0
    tool_use_count = 0
    final_event_seen = False
    update_attempts = 0
    update_failures = 0
    card_degraded_mid_stream = False
    auth_failed = False

    try:
        try:
            async for ev in claude_proc.run_stream(
                work_dir=project_cfg["work_dir"],
                prompt=parsed.text if prompt is None else prompt,
                timeout=runtime_cfg.get("reply_timeout", 300),
                session_id=session_id,
                disallowed_tools=read_phase["disallowed_tools"],
                disallowed_bash_patterns=read_phase.get("disallowed_bash_patterns"),
                model=project_cfg.get("model"),
                meta_work_dir=project_cfg.get("meta_work_dir"),
            ):
                now = time.monotonic()
                if first_event_at is None:
                    first_event_at = now
                last_event_at = now

                # Synthetic event from claude_proc.run_stream when the CLI
                # exited 1 with empty stdout/stderr (token expired). We
                # swap in a clear message + push self-alert downstream.
                if ev.get("type") == "_auth_failed":
                    auth_failed = True
                    exit_code = 1
                    # Insertion #3 (spec § 3.7): observer's hard rule
                    # `hard:auth_failed` reads this attribute from the trace.
                    cur = observability.current_span()
                    if cur is not None:
                        cur.set_attribute("digital_agent.auth_failed", True)
                    continue

                extracted = _extract_stream_event(ev)
                if extracted is None:
                    continue
                kind, payload = extracted

                if kind == "tool_use":
                    # A tool call ends the current text run (block boundary).
                    if cur_run:
                        text_runs.append("".join(cur_run))
                        cur_run = []
                    events.append(payload)
                    pending_tool_count += 1
                    tool_use_count += 1
                    # Insertion #2 (spec § 3.7): emit child span for the
                    # tool_use observation. The span is short-lived (we
                    # don't know per-tool duration from stream-json), but
                    # gives observer the call sequence + previews for
                    # hallucination judging.
                    with observability.start_tool_span(
                        tool_name=getattr(payload, "name", "?"),
                        input_preview=getattr(payload, "input_summary", ""),
                    ):
                        pass
                elif kind == "text_delta":
                    answer_chunks.append(payload)
                    cur_run.append(payload)
                    text_delta_count += 1
                    if first_text_delta_at is None:
                        first_text_delta_at = now
                elif kind == "final":
                    final_payload = payload
                    final_event_seen = True
                    if payload.get("is_error"):
                        exit_code = 1
                    # Insertion #4 (spec § 5.1 gen_ai.usage.*): the raw
                    # result event from claude --print --output-format
                    # stream-json carries `usage.{input_tokens, output_tokens}`
                    # and `modelUsage.{<model_id>: {...}}` (first key = model).
                    # Surface to the active turn span for daily report.
                    usage = ev.get("usage") or {}
                    model_usage = ev.get("modelUsage") or {}
                    cur = observability.current_span()
                    if cur is not None and usage:
                        cur.set_attribute(
                            "gen_ai.usage.input_tokens",
                            int(usage.get("input_tokens", 0)),
                        )
                        cur.set_attribute(
                            "gen_ai.usage.output_tokens",
                            int(usage.get("output_tokens", 0)),
                        )
                        cur.set_attribute("gen_ai.system", "anthropic")
                        # Model id (e.g. "claude-opus-4-7[1m]" / "claude-haiku-4-5")
                        # is the first key of modelUsage. Daily report
                        # groups token totals by this.
                        if model_usage:
                            model_id = next(iter(model_usage), "unknown")
                            cur.set_attribute(
                                "gen_ai.request.model", model_id,
                            )

                # Throttle progress card updates while still in card mode.
                if card_msg_id is not None and throttler.should_emit(
                    time.monotonic(), pending_tool_count,
                ):
                    update_attempts += 1
                    try:
                        progress = build_progress_card(
                            events, time.monotonic() - start,
                        )
                        ok = await channel.update_card(card_msg_id, progress)
                        if ok:
                            throttler.mark_emitted(time.monotonic())
                            pending_tool_count = 0
                        else:
                            update_failures += 1
                    except StreamCardDegraded as e:
                        update_failures += 1
                        card_degraded_mid_stream = True
                        log.warning("update_card degraded mid-stream: %s", e)
                        stream_card_metrics.bump_throttled()
                        card_msg_id = None
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("claude stream timed out")
            timed_out = True
            exit_code = -1
        except Exception as exc:
            log.exception("claude stream failed")
            exit_code = 1
            # Surface the failure to the user instead of "(no answer)" — otherwise
            # verifier wastes a Max-quota call on an empty draft and the user
            # sees nothing useful.
            if not answer_chunks:
                answer_chunks.append(f"(claude stream failed: {type(exc).__name__})")
    finally:
        # Flush the trailing text run on every exit path (normal end, timeout,
        # exception) so the last assistant block is available to
        # _recover_superseded_answer.
        if cur_run:
            text_runs.append("".join(cur_run))
            cur_run = []
        # Emit stream_summary unconditionally — even if a later raise escapes —
        # so a stalled "🔄 分析中..." card still leaves a structured trace
        # of WHERE in the stream it died (first event arrived? final event?).
        elapsed_now = time.monotonic() - start
        log.info(
            "stream_summary "
            "first_event_ms=%d first_text_delta_ms=%d last_event_ms=%d "
            "elapsed_ms=%d text_delta_count=%d tool_use_count=%d "
            "final_event_seen=%s update_attempts=%d update_failures=%d "
            "card_degraded_mid_stream=%s exit_code=%d timed_out=%s",
            int((first_event_at - start) * 1000) if first_event_at is not None else -1,
            int((first_text_delta_at - start) * 1000) if first_text_delta_at is not None else -1,
            int((last_event_at - start) * 1000) if last_event_at is not None else -1,
            int(elapsed_now * 1000),
            text_delta_count, tool_use_count,
            "true" if final_event_seen else "false",
            update_attempts, update_failures,
            "true" if card_degraded_mid_stream else "false",
            exit_code, "true" if timed_out else "false",
        )

    # 3) Compose RunResult
    #
    # The final answer is the CLI-assembled final assistant message carried on
    # the `result` event (``result_text``) — the SAME source the buffered path
    # uses (claude_proc.run → data["result"]). It is NOT the concatenation of
    # every text_delta across the turn: stream-json emits a text_delta for every
    # assistant text block, including the "what I'm about to do" narration
    # between tool calls. Joining all of them glued a whole turn's running
    # commentary into the card body (observed: a 20-tool turn posted "先 dry-run…
    # 校验器有坑… Build 成功… 已发布… 复验线上值…" instead of the conclusion).
    # text_delta now feeds only the live progress card; ``composed`` is kept
    # purely as a fallback for when no usable result event arrives (mid-stream
    # exception, timeout, or a CLI build that doesn't emit `result`).
    #
    # The result event is present regardless of --include-partial-messages, so
    # the answer no longer depends on that flag (the flag still drives the
    # progress card's tool list + live text).
    if auth_failed:
        # Auth-failed beats both stream-timed-out and (no answer) — it's the
        # actionable signal to the operator. Fire a throttled self-push so
        # they hear about it even if no one is watching the alert group.
        final_text = claude_proc.AUTH_FAILED_TEXT
        await _notify_auth_failed()
    else:
        composed = "".join(answer_chunks).strip()
        fp = final_payload or {}
        result_text = (fp.get("result_text") or "").strip()
        api_status = fp.get("api_error_status")
        if fp.get("is_error") and result_text:
            # api error result (e.g. 429 weekly quota): surface it to the user
            # instead of the opaque '(no answer)' — and ahead of any partial
            # narration that streamed before the error.
            final_text = f"(claude api error{(' ' + str(api_status)) if api_status else ''}: {result_text})"
            exit_code = api_status or exit_code or 1
        elif result_text:
            # The result event = the turn's LAST assistant message. If that is a
            # short self-referential closer ("已交付…结论不变。无需再操作"), the real
            # answer was delivered mid-turn and superseded — recover it.
            final_text = _recover_superseded_answer(result_text, text_runs)
        elif composed:
            # Fallback: no result-event text available — degrade to the joined
            # deltas rather than dropping a partial answer on the floor.
            final_text = composed
        elif timed_out:
            final_text = "(claude stream timed out)"
        else:
            final_text = "(no answer)"
    result = claude_proc.RunResult(
        text=final_text,
        session_id=final_payload.get("session_id"),
        exit_code=exit_code,
        timed_out=timed_out,
    )
    # Stash card-related state for the caller (final card update + observability).
    result._card_msg_id = card_msg_id  # type: ignore[attr-defined]
    result._elapsed_s = time.monotonic() - start  # type: ignore[attr-defined]
    result._tool_count = len(events)  # type: ignore[attr-defined]
    result._card_degraded_mid_stream = card_degraded_mid_stream  # type: ignore[attr-defined]
    return result


def _extract_stream_event(ev: dict) -> tuple[str, Any] | None:
    """Extract relevant info from a stream-json event.

    Returns ``(kind, payload)`` for one of:
      - ``("tool_use", ToolUse)``  — a content_block_start with tool_use
      - ``("text_delta", str)``    — a content_block_delta text_delta
      - ``("final", dict)``        — a result event (terminal stats)

    Returns None for irrelevant events (system/hook_*, api_retry, deltas
    other than text, etc.) so the caller can simply ``continue``.
    """
    et = ev.get("type")
    if et == "stream_event":
        inner = ev.get("event") or {}
        inner_type = inner.get("type")
        if inner_type == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                name = block.get("name") or "?"
                inp = block.get("input") or {}
                return ("tool_use", ToolUse(
                    name=name, input_summary=_summarize_tool_input(name, inp),
                ))
        elif inner_type == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                return ("text_delta", delta.get("text", ""))
    elif et == "result":
        return ("final", {
            "is_error": ev.get("subtype") != "success" or bool(ev.get("is_error")),
            "duration_ms": ev.get("duration_ms"),
            "total_cost_usd": ev.get("total_cost_usd"),
            "session_id": ev.get("session_id"),
            "result_text": ev.get("result") or "",
            "api_error_status": ev.get("api_error_status"),
        })
    return None


def _summarize_tool_input(name: str, inp: dict) -> str:
    """Short-summarize a tool_use input for progress card display.

    Picks the most user-meaningful field if present (file_path, pattern,
    command, ...). Falls back to a stringified dict, capped at 60 chars
    so even pathological inputs don't blow up the progress card body
    (further capped to MAX_SUMMARY_CHARS in stream_card.build_progress_card).
    """
    if not inp:
        return ""
    for key in ("file_path", "path", "pattern", "query", "command"):
        if key in inp:
            return str(inp[key])[:60]
    return str(inp)[:60]


# ---------------------------------------------------------------------------
# Verifier integration (M7-T03)
# ---------------------------------------------------------------------------


def _get_cost_tracker(features_cfg: dict) -> verifier.CostTracker:
    """Return module-level CostTracker singleton, lazy-configured on first call.

    ``features_cfg`` is the top-level ``cfg["features"]`` dict (per spec L285).
    State file path defaults to ``.state/verifier-counters.json`` (the
    spec-mandated location), but can be overridden via
    ``features.verifier.cost_cap.state_file_path``. The override is
    primarily a test-hygiene knob: the prod daemon and pytest both wrote
    to the same file under the default, so a long-running daemon could
    saturate the per-chat budget for fixture chat ids and break tests.
    """
    global _cost_tracker
    if _cost_tracker is None:
        tracker = verifier.CostTracker()
        feat = features_cfg.get("verifier", {})
        cap = feat.get("cost_cap", {})
        state_file = cap.get("state_file_path") or ".state/verifier-counters.json"
        tracker.configure(
            state_file_path=state_file,
            daily_limit=cap.get("daily_trigger_limit", 200),
            per_chat_limit=cap.get("per_chat_trigger_limit", 30),
        )
        _cost_tracker = tracker
    return _cost_tracker


def _make_verifier_runner(meta_work_dir: str, project_cfg: dict):
    """Build a runner closure that forks claude with the verifier subagent.

    The closure matches verifier.verify's runner contract:
      ``async def runner(*, work_dir, question, draft) -> str``
    Returns the verifier's textual output (PASS / REVISE: ...).
    """
    async def _run(*, work_dir, question, draft):
        prompt = (
            f"USER_QUESTION:\n{question}\n\n"
            f"DRAFT_ANSWER:\n{draft}\n\n"
            f"WORK_DIR: {work_dir}\n\n"
            f"按 .claude/agents/verifier.md 的 5 条检查执行。"
            f"只输出 PASS 或 REVISE: 列表，无别的废话。"
        )
        verifier_model = project_cfg.get("verifier_model") or project_cfg.get("model")
        # Per-round timing + model log: lets ops see whether verifier_model
        # config is actually flowing through (haiku alias vs full ID), and
        # quantify how much of perceived "verifier slow" is CLI startup
        # overhead (~25-35s on every cold fork) vs LLM inference.
        import time as _time
        start = _time.monotonic()
        # work_dir = meta dir so .claude/agents/verifier.md is discoverable.
        # Read-only enforced via disallowed_tools (verifier must not write).
        result = await claude_proc.run(
            work_dir=str(work_dir),
            prompt=prompt,
            timeout=300,  # verifier should be quick; 5min cap
            session_id=None,
            disallowed_tools=["Edit", "Write", "NotebookEdit"],
            model=verifier_model,
        )
        log.info(
            "verifier round: model=%s elapsed=%.1fs exit=%s",
            verifier_model, _time.monotonic() - start, result.exit_code,
        )
        return result.text
    return _run


def _maybe_inject_lessons(project_cfg: dict, question: str) -> str:
    """Prepend project lessons.md (when present and non-empty) to the verifier
    question. Returns the original question unchanged on any miss/empty.

    Why this is needed (US-003): verifier runs with ``meta_work_dir`` for
    echo-chamber isolation, so it never auto-reads ``project/knowledge/
    lessons.md``. /lesson slash command writes there; without injection
    those corrections never reach verifier and the feedback loop is
    half-open (lessons influence the main agent but not verifier).
    """
    work_dir = project_cfg.get("work_dir")
    if not work_dir:
        return question
    lessons_path = Path(work_dir) / "knowledge" / "lessons.md"
    if not lessons_path.is_file():
        return question
    try:
        body = lessons_path.read_text(encoding="utf-8").strip()
    except OSError:
        # Read failure is non-fatal: verifier still runs without lessons.
        return question
    if not body:
        return question
    return (
        "PRIOR LESSONS (管理员 has corrected the agent before; verify the "
        "draft below does not violate any of these):\n\n"
        f"{body}\n\n"
        "---\n\nUSER QUESTION:\n"
        f"{question}"
    )


async def _maybe_verify(
    *,
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    project_cfg: dict,
    runtime_cfg: dict,
    features_cfg: dict,
    question: str,
    draft: str,
) -> str:
    """Run verifier subagent if config + trigger rules say so.

    Returns the text to send to the user. On any verifier-path failure
    (rate-limit, crash, REVISE-persistent) returns the original draft with
    an appended hint so the user is never silently left hanging.

    ``features_cfg`` comes from top-level ``cfg["features"]`` (per spec L285,
    features lives at config TOP-LEVEL, sibling to runtime).
    """
    feat = features_cfg.get("verifier", {})
    if not feat.get("enabled", True):
        return draft

    user_hint = None  # MVP: no per-message hint extraction yet
    decision = verifier.should_trigger(question, draft, user_hint=user_hint)
    if not decision.trigger:
        log.debug("verifier skipped (%s)", decision.reason)
        return draft

    # Verifier budget bucket: keyed the same way as session/approval so
    # one topic's verifier spend can't drain another topic's quota.
    chat_id = _thread_key(parsed)
    tracker = _get_cost_tracker(features_cfg)
    if not tracker.can_trigger(chat_id):
        log.warning("verifier rate-limited for chat=%s", chat_id)
        return draft + "\n\n_⚠️ verifier 限额已用尽_"

    # Safe: no await between can_trigger() and record() on a single event loop.
    # Adding awaits here would introduce a budget-overrun race.
    # Note: record() runs BEFORE verify() so a crashed/timed-out verify still
    # consumes budget — we treat "decided to consume Max quota" as the unit
    # of accounting.
    tracker.record(chat_id)

    # Best-effort "verifying" indicator card; never let this block the reply.
    try:
        await channel.send_card(parsed, {"fallback_text": "🔍 验证中..."})
    except Exception:
        log.exception("send_card '验证中' failed (continuing)")

    meta_work_dir = (
        runtime_cfg.get("paths", {}).get("meta_work_dir")
        or project_cfg["work_dir"]
    )

    # Tier 2 (US-003): inject project-level lessons.md into the verifier's
    # question so verifier checks the draft against accumulated user
    # corrections (`/lesson` slash command writes here). verifier runs in
    # meta_work_dir for echo-chamber isolation, so it doesn't auto-load
    # example_project/knowledge/lessons.md — we explicitly fold it into the
    # prompt instead.
    verifier_question = _maybe_inject_lessons(project_cfg, question)

    # MVP: verify() iterates with the SAME draft across rounds (no main-agent
    # re-draft between rounds). v1.x will re-draft using concerns. See
    # verifier.py docstring.
    result = await verifier.verify(
        work_dir=meta_work_dir,
        question=verifier_question,
        draft_answer=draft,
        max_rounds=feat.get("max_revise_rounds", 2),
        _runner=_make_verifier_runner(meta_work_dir, project_cfg),
    )

    if result.verified is None:
        log.warning("verifier crashed: %s", result.error_msg)
        return draft + "\n\n_⚠️ verifier 未跑通_"
    if result.verified is True:
        log.info("verifier PASS (rounds=%d)", result.rounds_used)
        return draft
    # verified is False — REVISE persistent after max_rounds
    concerns_text = "\n".join(f"- {c}" for c in result.concerns)
    return (
        draft
        + f"\n\n_⚠️ verifier 仍有疑虑（{result.rounds_used}轮后）：_\n"
        + concerns_text
    )


def _admin_at_mentions(admin_users: list[str]) -> str:
    """Render Feishu lark_md @mentions for the admins.

    ``<at id="ou_xxx"></at>`` is auto-rendered by Feishu into the user's
    display name and pushes a notification — so a 线上 approval card pings
    the admin (e.g. @管理员) without us needing their name string. Falls back
    to plain '管理员' when no admin open_id is configured.
    """
    mentions = " ".join(f'<at id="{uid}"></at>' for uid in admin_users if uid)
    return mentions or "管理员"


def _build_approval_card_text(info: approval.ApprovalInfo, admin_users: list[str]) -> str:
    """Approval card text, tiered by target environment.

    线上/production: only an admin may approve, so the card names the
    required approver via a real @mention. BOE/test: the requester can
    clear it themselves.
    """
    fields = (
        f"操作: {info.operation}\n"
        f"原因: {info.reason}\n"
        f"影响: {info.impact}\n"
        f"回滚: {info.rollback}\n"
    )
    env_label = info.environment.strip() or "未声明"
    if approval.is_production(info):
        return (
            f"⚠️ **线上写操作 · 需管理员确认**（环境: {env_label}）\n"
            f"{fields}\n"
            f"请 {_admin_at_mentions(admin_users)} 回复「确认」批准 / 「取消」放弃。\n"
            f"_（线上操作仅管理员可批准，发起人确认无效）_"
        )
    return (
        f"🔧 **需要审批的写操作**（环境: {env_label}）\n"
        f"{fields}\n"
        f"回复「确认」执行 / 「取消」放弃（发起人或管理员可批准）"
    )


async def _execute_write_phase(
    channel: ChannelAdapter,
    parsed: ParsedMsg,
    appr: approval.Approval,
    project_cfg: dict,
) -> None:
    """Fork claude with full perms to execute approved write operation."""
    approval.transition(appr, approval.State.EXECUTING)
    write_phase = project_cfg.get("write_phase", {})
    result = await claude_proc.run(
        work_dir=project_cfg["work_dir"],
        prompt=(
            f"用户已审批通过以下操作，请执行并在完成后报告结果。\n\n"
            f"操作: {appr.info.operation}\n"
            f"原因: {appr.info.reason}\n"
            f"影响: {appr.info.impact}\n"
            f"环境: {appr.info.environment or '未声明'}\n"
            f"如需回滚: {appr.info.rollback}\n"
        ),
        timeout=write_phase.get("timeout", 600),
        session_id=None,
        disallowed_tools=None,
        model=project_cfg.get("model"),
        meta_work_dir=project_cfg.get("meta_work_dir"),
    )
    if result.exit_code == 0 and not result.timed_out:
        approval.transition(appr, approval.State.DONE)
        await channel.reply(parsed, f"✅ 执行完成\n{result.text}")
        # MUST match the key used in approval.create() at handle_message_inner
        # (topic_id when present, else thread_root_id). Using the wrong key
        # leaks the approval entry and breaks the next message in this
        # topic — see runtime/scheduler.py:_thread_key docstring.
        approval.remove(_thread_key(parsed))
    else:
        approval.transition(appr, approval.State.FAILED)
        await channel.reply(parsed, f"❌ 执行失败 (回复「重试」再跑)\n{result.text}")


# ---------------------------------------------------------------------------
# consume loop — with exponential backoff reconnect
# ---------------------------------------------------------------------------

# In-flight handler tasks (Bug P fix): consume() dispatches each message
# through asyncio.create_task so per-channel intake is no longer serialized
# behind a single await. The set keeps strong refs alive (asyncio only weakly
# refs tasks, GC mid-flight will silently drop them) and is drained on
# shutdown via drain_in_flight() so SIGTERM doesn't half-kill an alert.
_in_flight: set[asyncio.Task] = set()


def _on_handler_done(task: asyncio.Task) -> None:
    """done_callback: discard from _in_flight + surface unhandled exceptions.

    Without this, a handler exception only fires when the task is garbage-
    collected ("Task exception was never retrieved"). We pull the exception
    explicitly so it lands in runtime.log immediately.
    """
    _in_flight.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "handle_message task crashed: %s",
            task.get_name(),
            exc_info=exc,
        )


async def drain_in_flight() -> None:
    """Await every currently-tracked handler task; called on graceful shutdown.

    Idempotent and safe to call when no tasks are in flight. Exceptions
    from individual tasks are swallowed (already logged via the done
    callback) — drain must not itself raise.
    """
    if not _in_flight:
        return
    pending = list(_in_flight)
    await asyncio.gather(*pending, return_exceptions=True)


async def _rebuild_consume_tasks(
    *,
    channels: list,
    projects: dict,
    holder: dict,
    make_task,
) -> None:
    """Cancel any consume task in ``holder`` and recreate one per channel
    bound to the (current) ``projects`` dict.

    Why this exists: ``_reload_cfg_in_place`` does ``cfg.clear(); cfg.update``
    which swaps ``cfg["projects"]`` for a NEW dict. The original consume
    tasks captured the OLD projects dict by reference, so a freshly-added
    /agent project would never be routed until the consume task is rebuilt.

    ``make_task(channel, projects)`` is injected so callers (run_forever)
    can close over runtime_cfg / features_cfg / scheduler_ctx and tests can
    stub it. The same channel adapter instance is reused across rebuilds —
    we do NOT re-load_channel, which would open a second lark-cli
    subscription on top of the first.
    """
    for ch in channels:
        old = holder.get(ch.name)
        if old is not None and not old.done():
            old.cancel()
            try:
                await old
            except asyncio.CancelledError:
                pass
        holder[ch.name] = make_task(ch, projects)


async def consume(
    channel: ChannelAdapter,
    projects: dict,
    runtime_cfg: dict,
    features_cfg: dict,
    bot_mention_key: str | None,
    alert_cfg: dict | None = None,
    *,
    scheduler_ctx: "SchedulerContext | None" = None,
) -> None:
    """Consume events from one channel with reconnect on failure.

    On subscribe() natural end or exception: log warning, close channel,
    sleep with exponential backoff (2s → 60s cap), then re-enter.

    ``features_cfg`` is the top-level ``cfg["features"]`` dict (per spec L285)
    forwarded to handle_message → _handle_message_inner → _maybe_verify.

    Dispatch is non-blocking (Bug P fix, see § 14.7): each message becomes
    an asyncio task tracked in ``_in_flight``. The actual concurrency cap
    lives in ``runtime.concurrency`` (global_sem + chat_sem); consume just
    stops being the bottleneck. ``run_forever`` calls ``drain_in_flight``
    after SIGTERM so in-flight handlers (especially alerts) finish cleanly.

    TODO(M2-T14 health): bounded retries + alerting for production use.
    """
    backoff = 2  # seconds; doubles each reconnect, capped at 60s
    while True:
        try:
            async for event in channel.subscribe():
                try:
                    parsed = await channel.parse(event)
                except Exception:
                    log.exception("channel.parse error")
                    continue
                if parsed is None:
                    continue
                match = routing.route(
                    parsed, projects, bot_mention_key=bot_mention_key
                )
                if match is None:
                    log.info(
                        "no project match: msg=%s chat=%s",
                        parsed.message_id,
                        parsed.chat_id,
                    )
                    continue
                project_name, project_cfg = match
                log.info(
                    "dispatched: project=%s chat=%s msg=%s "
                    "chat_type=%s mentioned=%s",
                    project_name,
                    parsed.chat_id,
                    parsed.message_id,
                    parsed.chat_type,
                    bool(
                        bot_mention_key
                        and bot_mention_key in parsed.mentions
                    ),
                )
                task = asyncio.create_task(
                    handle_message(
                        channel, parsed, project_name, project_cfg, runtime_cfg,
                        features_cfg, alert_cfg=alert_cfg,
                        scheduler_ctx=scheduler_ctx,
                    ),
                    name=f"handle-{parsed.message_id}",
                )
                _in_flight.add(task)
                task.add_done_callback(_on_handler_done)
            # subscribe() returned without raising — for the current
            # lark-cli SSE this is the *expected* shape (the cli closes
            # after each event batch and we re-subscribe). Log at INFO so
            # this routine path doesn't drown runtime.log in WARNING.
            # Exceptional exits still raise out and hit the WARNING/ERROR
            # branches below.
            log.info(
                "channel.subscribe completed (resubscribing in %ds)", backoff
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "consume loop crashed, reconnecting in %ds", backoff
            )

        try:
            await channel.close()
        except Exception:
            log.warning("error closing channel for reconnect", exc_info=True)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# ---------------------------------------------------------------------------
# run_forever — gather with return_exceptions + SIGTERM handling
# ---------------------------------------------------------------------------


async def _session_gc_loop(max_age: int) -> None:
    """Background task: clean up expired sessions every hour."""
    while True:
        try:
            session.cleanup_expired(max_age=max_age)
        except Exception as e:
            log.warning("session GC error: %s", e)
        await asyncio.sleep(3600)


# ---------------------------------------------------------------------------
# Alert polling loop (US-poll-005) — incoming-webhook fallback for alert
# chats whose senders bypass the lark-cli event subscription.
# ---------------------------------------------------------------------------


async def run_alert_polling_loop(
    *,
    alert_cfg: dict,
    projects: dict,
    runtime_cfg: dict,
    features_cfg: dict,
    cursor: PollerCursor,
    channel: ChannelAdapter | None = None,
    poll_chat_fn=_poll_chat_default,
    handle_message_fn=None,
    now_ms_fn=None,
    sleep_fn=asyncio.sleep,
    scheduler_ctx: "SchedulerContext | None" = None,
) -> None:
    """Poll each alert_chats entry on a fixed cadence; dispatch new messages
    through the existing scheduler entry point.

    Cold-start handling per chat (cursor unseen):
      - skip_history (default): seed cursor at current time so this iteration
        does NOT poll; future ticks pick up only new alerts
      - last_24h: seed cursor at now-24h, poll immediately, but cap ingest
        at ``max_initial_ingest`` to avoid burst-burning Claude quota

    Existing cursor (process restart):
      - cursor preserved as-is; resume from where the last run left off

    All errors are logged + swallowed so a single bad iteration cannot
    take down the loop.
    """
    polling_cfg = (alert_cfg or {}).get("polling") or {}
    interval_s = int(polling_cfg.get("interval_seconds", 30))
    page_size = int(polling_cfg.get("page_size", 20))
    cold_start = polling_cfg.get("cold_start", "skip_history")
    max_initial = int(polling_cfg.get("max_initial_ingest", 30))
    if handle_message_fn is None:
        handle_message_fn = handle_message
    if now_ms_fn is None:
        now_ms_fn = lambda: int(time.time() * 1000)  # noqa: E731

    # routes: list[(chat_id, project_name, project_cfg)]
    routes: list[tuple[str, str, dict]] = []
    for entry in alert_cfg.get("alert_chats") or []:
        chat_id = entry.get("chat_id")
        proj_name = entry.get("project")
        proj_cfg = projects.get(proj_name)
        if chat_id and proj_cfg:
            routes.append((chat_id, proj_name, proj_cfg))

    if not routes:
        log.warning("alert_polling_loop: no alert_chats routes; loop idle")

    # Track cold-start state per chat in-memory so we apply it once even
    # if the cursor file gets re-read multiple times.
    cold_started: set[str] = set()
    # Pre-fill from existing cursor — those chats are NOT in cold-start.
    for chat_id, _, _ in routes:
        if cursor.get(chat_id) is not None:
            cold_started.add(chat_id)

    while True:
        for chat_id, project_name, project_cfg in routes:
            try:
                # First touch for this chat in this process: apply cold_start.
                if chat_id not in cold_started:
                    now_ms = now_ms_fn()
                    if cold_start == "skip_history":
                        cursor.set(chat_id, now_ms)
                        cold_started.add(chat_id)
                        # No poll this tick — wait for the next one to
                        # surface only new arrivals.
                        continue
                    if cold_start == "last_24h":
                        cursor.set(chat_id, now_ms - 24 * 60 * 60 * 1000)
                        cold_started.add(chat_id)
                        # Fall through to poll immediately, with the
                        # max_initial_ingest cap applied below.
                    else:
                        log.warning(
                            "alert_polling_loop: unknown cold_start=%r; "
                            "falling back to skip_history", cold_start,
                        )
                        cursor.set(chat_id, now_ms)
                        cold_started.add(chat_id)
                        continue
                    is_first_poll = True
                else:
                    is_first_poll = False

                since_ms = cursor.get(chat_id) or 0
                msgs = await poll_chat_fn(
                    chat_id=chat_id,
                    since_ms=since_ms,
                    page_size=page_size,
                )
                if not msgs:
                    continue

                # Cold-start cap: drop oldest, keep newest N — stale alerts
                # would just burn Claude quota for no operational value.
                if is_first_poll and len(msgs) > max_initial:
                    log.info(
                        "alert_polling_loop: cold-start cap chat=%s "
                        "msgs=%d -> %d (dropping oldest)",
                        chat_id, len(msgs), max_initial,
                    )
                    msgs = msgs[-max_initial:]

                # Polling-path sender filter (2026-05-19 fix): only dispatch
                # explicit bot-like messages. Without this, thread/topic
                # replies from humans in alert chats get the full deep
                # branch (claude investigation + reply), bypassing the
                # routing.route() Strategy 2 sender filter entirely.
                _BOT_SENDERS = {"app", "bot", "webhook"}
                for msg in msgs:
                    if getattr(msg, "sender_type", None) not in _BOT_SENDERS:
                        log.info(
                            "alert_polling_loop: skip non-bot msg "
                            "chat=%s msg=%s sender_type=%r",
                            chat_id, msg.message_id, getattr(msg, "sender_type", None),
                        )
                        # Still advance cursor so we don't replay this msg
                        # every 30s tick forever.
                        ts = getattr(msg, "_poll_create_time_ms", 0)
                        if ts:
                            cursor.set(chat_id, ts)
                        continue
                    try:
                        await handle_message_fn(
                            channel, msg, project_name, project_cfg,
                            runtime_cfg, features_cfg, alert_cfg=alert_cfg,
                            scheduler_ctx=scheduler_ctx,
                        )
                    except Exception:
                        log.exception(
                            "alert_polling_loop: handle_message failed "
                            "(continuing): chat=%s msg=%s",
                            chat_id, msg.message_id,
                        )
                    # Advance cursor regardless of handle_message outcome
                    # — otherwise a poison message would re-dispatch
                    # forever every tick.
                    ts = getattr(msg, "_poll_create_time_ms", 0)
                    if ts:
                        cursor.set(chat_id, ts)
            except Exception:
                log.exception(
                    "alert_polling_loop: iteration failed for chat=%s",
                    chat_id,
                )

        await sleep_fn(interval_s)


async def run_forever(cfg: dict) -> None:
    """Spawn per-channel consume tasks + session GC; handle SIGTERM gracefully."""
    # Wire trace emission once before any consume task can fire.
    _apply_observability_config(cfg)

    # Build SchedulerContext early so consume + polling tasks can thread it
    # into every handle_message call. config_path comes from main() via the
    # module-level _LOADED_CONFIG_PATH (set right before asyncio.run); when
    # absent (legacy callers / tests calling run_forever directly), fall
    # back to "config.yaml" — /agent write commands won't work but read
    # commands and the rest of the runtime are unaffected.
    config_path = Path(_LOADED_CONFIG_PATH or "config.yaml")
    backup_dir = Path(
        (cfg.get("runtime") or {}).get(
            "config_backup_dir", "./.state/config_baks",
        ),
    )
    polling_task_holder: dict[str, asyncio.Task | None] = {"task": None}

    enabled_channels = []
    for ch_name, ch_cfg in cfg["channels"].items():
        if not ch_cfg.get("enabled", False):
            continue
        try:
            ch = load_channel(ch_name, ch_cfg)
        except (ValueError, ImportError) as e:
            log.error("failed to load channel %s: %s", ch_name, e)
            continue
        enabled_channels.append(ch)

    if not enabled_channels:
        log.error("no channels enabled; nothing to do")
        return

    bot_mention_key = cfg["channels"].get("feishu", {}).get("bot_mention_key")

    tasks = []
    features_cfg = cfg.get("features", {})
    alert_cfg = cfg.get("alert_resolver") or None
    # `_stream_card_enabled` reads `runtime_cfg["channels"]["feishu"]
    # ["stream_card"]`, but historical callers passed `cfg["runtime"]`
    # which has no `channels` field — silently routing every read through
    # the buffered (no-partial-on-timeout) path. Merge the top-level
    # channels block into the runtime cfg once so the stream path can
    # actually engage and surface partial output when claude exceeds
    # `reply_timeout`.
    runtime_cfg = {**cfg["runtime"], "channels": cfg["channels"]}

    # Wire restart_alert_polling closure — captured here so it can see
    # polling_task_holder + the in-scope feishu_channel/cursor (set when
    # polling is enabled below). On call: cancel current task, rebuild
    # from the (potentially mutated) cfg's alert_resolver section.
    feishu_channel_holder: dict[str, ChannelAdapter | None] = {"ch": None}
    cursor_holder: dict[str, PollerCursor | None] = {"cursor": None}

    async def _restart_alert_polling() -> None:
        t = polling_task_holder["task"]
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        alert_cfg_now = cfg.get("alert_resolver") or {}
        if not (alert_cfg_now.get("enabled")
                and (alert_cfg_now.get("polling") or {}).get("enabled")):
            polling_task_holder["task"] = None
            return
        ch_now = feishu_channel_holder["ch"]
        cur_now = cursor_holder["cursor"]
        if ch_now is None or cur_now is None:
            log.warning(
                "restart_alert_polling: feishu channel / cursor unavailable; "
                "polling not restarted",
            )
            return
        polling_task_holder["task"] = asyncio.create_task(
            run_alert_polling_loop(
                alert_cfg=alert_cfg_now,
                projects=cfg["projects"],
                runtime_cfg=runtime_cfg,
                features_cfg=features_cfg,
                cursor=cur_now,
                channel=ch_now,
                scheduler_ctx=scheduler_ctx,
            ),
            name="alert-polling",
        )

    # Holder for the per-channel consume tasks so _restart_consume can
    # cancel + recreate them after a /agent project add|rm reloads cfg.
    consume_task_holder: dict[str, asyncio.Task | None] = {}

    def _make_consume_task(ch, projects) -> asyncio.Task:
        # NOTE: runtime_cfg / features_cfg / bot_mention_key are captured once
        # at run_forever start and are NOT refreshed on reload — _restart_consume
        # only rehydrates `projects`. This is intentional: /agent project add|rm
        # only mutates cfg["projects"]. If a future reload path needs live
        # runtime/features/mention, read them from cfg here (alert_cfg already does).
        return asyncio.create_task(
            consume(
                ch, projects, runtime_cfg, features_cfg,
                bot_mention_key, alert_cfg=cfg.get("alert_resolver") or None,
                scheduler_ctx=scheduler_ctx,
            ),
            name=f"consume-{ch.name}",
        )

    async def _restart_consume() -> None:
        # Rebuild against the CURRENT cfg["projects"] — _reload_cfg_in_place
        # swapped it for a new dict, so the old consume tasks point at stale
        # projects. Reuse the same channel adapters (no re-load_channel) to
        # avoid a duplicate lark-cli subscription.
        # Each rebuild cancels + re-subscribes every channel, so there is a
        # sub-second window where ALL chats (not just the changed project's)
        # are not listening; lark-cli event has no offset replay, so messages
        # arriving in that window are dropped. Surfaced here for ops visibility.
        log.warning(
            "restart_consume: re-subscribing all channels (brief intake gap); "
            "messages arriving during resubscribe may be missed"
        )
        await _rebuild_consume_tasks(
            channels=enabled_channels,
            projects=cfg["projects"],
            holder=consume_task_holder,
            make_task=_make_consume_task,
        )
        # Newly created tasks must join the shutdown set + crash callback.
        for ch in enabled_channels:
            t = consume_task_holder.get(ch.name)
            if t is not None and t not in tasks:
                tasks.append(t)
                t.add_done_callback(_on_task_done)

    scheduler_ctx = SchedulerContext(
        cfg=cfg,
        config_path=config_path,
        backup_dir=backup_dir,
        restart_alert_polling_fn=_restart_alert_polling,
        restart_consume_fn=_restart_consume,
    )

    for ch in enabled_channels:
        consume_task = asyncio.create_task(
            consume(
                ch, cfg["projects"], runtime_cfg, features_cfg,
                bot_mention_key, alert_cfg=alert_cfg,
                scheduler_ctx=scheduler_ctx,
            ),
            name=f"consume-{ch.name}",
        )
        consume_task_holder[ch.name] = consume_task
        tasks.append(consume_task)

    # Alert kb daily sweep loop (US-007). Build a unique kb per project that
    # appears in alert_chats; skip the loop entirely when sweep is disabled.
    if (
        alert_cfg
        and alert_cfg.get("enabled")
        and ((alert_cfg.get("sweep") or {}).get("enabled", True))
    ):
        sweep_kbs = []
        seen_roots: set[str] = set()
        for chat_route in alert_cfg.get("alert_chats") or []:
            proj = cfg["projects"].get(chat_route.get("project"))
            if not proj or not proj.get("work_dir"):
                continue
            kb = alert_resolver.make_kb(proj["work_dir"])
            root_key = str(kb.root)
            if root_key in seen_roots:
                continue
            seen_roots.add(root_key)
            sweep_kbs.append(kb)
        if sweep_kbs:
            tasks.append(
                asyncio.create_task(
                    alert_resolver.run_sweep_loop(sweep_kbs, alert_cfg),
                    name="alert-sweep",
                )
            )

    # Alert polling loop (US-poll-005): incoming-webhook fallback. Only
    # starts when alert_resolver is enabled and polling.enabled is true.
    # Reuses the feishu channel adapter so alert hits can post replies on
    # the same connection — without a channel, channel.reply() crashes.
    if (
        alert_cfg
        and alert_cfg.get("enabled")
        and ((alert_cfg.get("polling") or {}).get("enabled", False))
    ):
        feishu_channel = next(
            (ch for ch in enabled_channels if getattr(ch, "name", None) == "feishu"),
            None,
        )
        if feishu_channel is None:
            log.warning(
                "alert_polling: no feishu channel enabled; skipping polling loop"
            )
        else:
            cursor_file = (
                (alert_cfg.get("polling") or {}).get(
                    "cursor_file", "./.state/alert_polling_cursor.json",
                )
            )
            cursor = PollerCursor(path=Path(cursor_file))
            # Stash channel + cursor so restart_alert_polling closure can
            # re-create the task after cfg mutation.
            feishu_channel_holder["ch"] = feishu_channel
            cursor_holder["cursor"] = cursor
            polling_task = asyncio.create_task(
                run_alert_polling_loop(
                    alert_cfg=alert_cfg,
                    projects=cfg["projects"],
                    runtime_cfg=runtime_cfg,
                    features_cfg=features_cfg,
                    cursor=cursor,
                    channel=feishu_channel,
                    scheduler_ctx=scheduler_ctx,
                ),
                name="alert-polling",
            )
            polling_task_holder["task"] = polling_task
            # NOTE: polling_task is intentionally NOT added to `tasks`.
            # The wait-for-stop loop below uses FIRST_COMPLETED semantics;
            # if polling_task were in `tasks`, then every call to
            # _restart_alert_polling (cancel + recreate) would trigger
            # "first complete" and tear the whole scheduler down. Polling
            # lifecycle is owned by polling_task_holder + the shutdown
            # finally block.

    tasks.append(
        asyncio.create_task(
            _session_gc_loop(cfg["runtime"].get("session_max_age", 86400)),
            name="session-gc",
        )
    )
    tasks.append(
        asyncio.create_task(
            health.heartbeat_loop(interval=30),
            name="health-heartbeat",
        )
    )

    from agent_runtime import repo_sync
    repo_sync_cfg = cfg["runtime"].get("repo_sync") or {}
    if repo_sync_cfg.get("enabled", False):
        tasks.append(asyncio.create_task(
            repo_sync.sync_loop(
                cfg["projects"],
                interval_seconds=repo_sync_cfg.get("interval_seconds", 3600),
            ),
            name="repo-sync",
        ))

    # Periodic lessons.md → SOUL.md distillation: fold accumulated /lesson
    # corrections into the curated persona so lessons.md stays short and the
    # persona stays the single source. Off by default; opt-in per config.
    from agent_runtime import lesson_distill
    distill_cfg = cfg["runtime"].get("lesson_distill") or {}
    if distill_cfg.get("enabled", False):
        tasks.append(asyncio.create_task(
            lesson_distill.distill_loop(
                cfg["projects"],
                (cfg.get("paths") or {}).get("meta_work_dir"),
                interval_seconds=distill_cfg.get("interval_seconds", 86400),
                min_lessons=distill_cfg.get("min_lessons", 3),
                model=distill_cfg.get("model"),
            ),
            name="lesson-distill",
        ))

    # Graceful shutdown on SIGTERM / SIGINT
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _graceful_stop() -> None:
        log.info("scheduler: received shutdown signal, stopping...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _graceful_stop)
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler; MVP targets macOS/Linux

    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")

    # Crash-logging done-callback on every long-lived task: if a task ends
    # unexpectedly (not via stop signal), we log the cause but we do NOT
    # tear down the scheduler. Tearing down on first-task-done made
    # _restart_alert_polling kill the daemon (cancelling old polling task
    # tripped FIRST_COMPLETED).
    def _on_task_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("task %s crashed: %s", t.get_name(), exc, exc_info=exc)
        else:
            log.info("task %s finished cleanly (no error)", t.get_name())

    for _t in tasks:
        _t.add_done_callback(_on_task_done)

    try:
        await stop_event.wait()
        # Cancel every long-lived task. Polling lives in polling_task_holder
        # (not in `tasks`) — cancel it explicitly here.
        polling_t = polling_task_holder["task"]
        cancel_targets = list(tasks)
        if polling_t is not None and not polling_t.done():
            cancel_targets.append(polling_t)
        log.info("scheduler: stopping, cancelling %d pending task(s)",
                 len(cancel_targets))
        for t in cancel_targets:
            t.cancel()
        await asyncio.gather(*cancel_targets, return_exceptions=True)
        # Bug P fix: drain in-flight handler tasks so SIGTERM doesn't kill
        # an alert mid-dispatch. Done after consume tasks are cancelled so
        # no new handlers can be queued while we wait.
        if _in_flight:
            log.info(
                "scheduler: draining %d in-flight handler(s)", len(_in_flight)
            )
            await drain_in_flight()
    except asyncio.CancelledError:
        log.info("scheduler: gather cancelled")
        for t in tasks + [stop_task]:
            t.cancel()
        await asyncio.gather(*tasks, stop_task, return_exceptions=True)
        if _in_flight:
            await drain_in_flight()
    finally:
        for ch in enabled_channels:
            try:
                await ch.close()
            except Exception:
                log.warning("error closing channel %s", ch.name, exc_info=True)


# ---------------------------------------------------------------------------
# main entry point
# ---------------------------------------------------------------------------


def _setup_health(cfg: dict, resolve) -> None:
    """Configure health.heartbeat + watchdog from config.

    Activates M9-T05 watchdog (ingest/backup mtime checks) when
    `paths.meta_work_dir` is set; stays no-op when not.
    """
    status_file = resolve(cfg["runtime"].get("status_file"))
    if not status_file:
        return
    meta_work_dir = resolve((cfg.get("paths") or {}).get("meta_work_dir"))
    history_file = resolve(cfg["runtime"].get("status_history_file"))
    health.configure(status_file, history_file=history_file, meta_dir=meta_work_dir)


def main() -> int:
    """Entry point: argparse + load_config + configure + asyncio.run."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        prog="agent-runtime",
        description="Feishu digital agent framework runtime",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="agent-runtime 0.1.0",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="path to master config.yaml",
    )
    args = parser.parse_args()

    try:
        cfg = config_mod.load_config(args.config)
    except (config_mod.ConfigError, FileNotFoundError) as e:
        print(f"❌ config error: {e}", file=sys.stderr)
        return 1

    # Resolve state/log paths relative to config.yaml's directory
    config_dir = Path(args.config).resolve().parent

    def _resolve(p: str | None) -> Path | None:
        if not p:
            return None
        path = Path(p)
        return path if path.is_absolute() else (config_dir / path).resolve()

    session_file = _resolve(cfg["runtime"]["session_file"])
    log.info("scheduler: config_dir=%s session_file=%s", config_dir, session_file)

    # 1) session persistence
    session.configure(session_file)

    # 2) concurrency
    concurrency.init_global(cfg["runtime"].get("max_concurrent", 5))

    # 3) file logging
    log_file_path = _resolve(cfg["runtime"].get("log_file"))
    if log_file_path:
        setup_file_logging(log_file_path)

    # 4) health heartbeat (M9-T05 watchdog wired via meta_work_dir if configured)
    _setup_health(cfg, _resolve)

    # Surface the config path to run_forever via module-level so the
    # SchedulerContext built inside knows which file /agent commands
    # should edit. See _LOADED_CONFIG_PATH docstring.
    global _LOADED_CONFIG_PATH
    _LOADED_CONFIG_PATH = str(Path(args.config).resolve())

    try:
        asyncio.run(run_forever(cfg))
    except KeyboardInterrupt:
        log.info("scheduler: KeyboardInterrupt, shutting down")
        return 0
    return 0
