"""Alert resolver — orchestrates retrieve → judge → reply / sink / sweep.

Public API used by scheduler:
  - is_alert_message(parsed, alert_cfg) -> (bool, route_dict | None)
  - try_handle_alert_hit(parsed, project_cfg, alert_cfg, channel, *,
                          kb, retriever, judge_fn=judge) -> bool
  - sink_after_deep(parsed, conclusion, *, kb) -> AlertEntry | None
  - run_sweep_loop(kbs, alert_cfg, *, now_fn=time.time,
                    sleep_fn=asyncio.sleep) -> None
  - make_kb(work_dir) -> AlertKB

Design discipline:
  - Every error path is fail-open: log + degrade to "miss" / drop the
    entry, never raise into the scheduler hot path.
  - Caller (scheduler) constructs kb / retriever once and threads them
    through, keeping this module dependency-injectable for tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_runtime.channels import ParsedMsg
from agent_runtime.alert_judge import JudgeResult, judge as default_judge
from agent_runtime.alert_kb import AlertEntry, AlertKB
from agent_runtime.alert_retriever import Retriever

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def make_kb(work_dir: str | Path) -> AlertKB:
    return AlertKB(root=Path(work_dir) / "knowledge" / "alerts")


def is_alert_message(
    parsed: ParsedMsg, alert_cfg: dict | None
) -> tuple[bool, dict | None]:
    """Return (True, route) iff the message should be handled by the alert
    resolver. ``route`` is the matching alert_chats entry (with chat_id and
    project keys); None when not an alert.
    """
    if not alert_cfg or not alert_cfg.get("enabled"):
        return False, None
    if parsed.sender_type != "app":
        return False, None
    for chat in alert_cfg.get("alert_chats") or []:
        if chat.get("chat_id") == parsed.chat_id:
            return True, chat
    return False, None


# ---------------------------------------------------------------------------
# Hit path
# ---------------------------------------------------------------------------


JudgeFn = Callable[..., Awaitable[JudgeResult | None]]


async def try_handle_alert_hit(
    parsed: ParsedMsg,
    project_cfg: dict,
    alert_cfg: dict,
    channel: Any,
    *,
    kb: AlertKB,
    retriever: Retriever,
    judge_fn: JudgeFn = default_judge,
) -> bool:
    """Return True iff we replied with a cached/rewritten conclusion.
    Caller should short-circuit the rest of the pipeline on True; on
    False, continue to deep investigation."""
    top_k = int(alert_cfg.get("top_k", 3))
    _r_t0 = time.monotonic()
    candidates = retriever.search(
        chat_id=parsed.chat_id, alert_text=parsed.text, top_k=top_k,
    )
    _retriever_ms = int((time.monotonic() - _r_t0) * 1000)
    if not candidates:
        log.info(
            "alert_retriever chat_id=%s elapsed_ms=%d candidate_count=0 outcome=no_candidates",
            parsed.chat_id, _retriever_ms,
        )
        return False
    log.info(
        "alert_retriever chat_id=%s elapsed_ms=%d candidate_count=%d outcome=ok",
        parsed.chat_id, _retriever_ms, len(candidates),
    )

    judge_model = alert_cfg.get("judge_model") or project_cfg.get("model")
    judge_timeout = int(alert_cfg.get("judge_timeout", 60))

    _j_t0 = time.monotonic()
    _judge_outcome = "miss"
    try:
        try:
            result = await judge_fn(
                alert_text=parsed.text,
                candidates=candidates,
                model=judge_model,
                timeout=judge_timeout,
                # config validation guarantees work_dir is set per project — let
                # KeyError surface loudly if a misconfig somehow reaches us.
                work_dir=project_cfg["work_dir"],
            )
        except Exception as e:
            log.warning("alert_resolver: judge crashed: %s", e)
            _judge_outcome = "crash"
            return False

        if result is None or not result.is_match:
            _judge_outcome = "miss"
            return False

        matched = next(
            (c.entry for c in candidates if c.entry.id == result.matched_id), None,
        )
        if matched is None:
            # Defensive: judge already validates this, but belt-and-braces.
            log.warning("alert_resolver: matched_id %r not in candidates", result.matched_id)
            _judge_outcome = "matched_id_invalid"
            return False
        _judge_outcome = "hit"
    finally:
        log.info(
            "alert_judge_phase chat_id=%s elapsed_ms=%d outcome=%s",
            parsed.chat_id, int((time.monotonic() - _j_t0) * 1000), _judge_outcome,
        )

    new_hit_count = matched.hit_count + 1
    reply_text = (
        f"⚡ 与 {matched.created_at} 的告警同类（id={matched.id}，"
        f"第 {new_hit_count} 次复用）\n\n"
        f"{result.rewritten_conclusion}"
    )

    # Reply BEFORE mark_hit: if mark_hit fails (disk full, etc.) the user
    # has still received the conclusion. We log + swallow the mark error.
    try:
        await channel.reply(parsed, reply_text)
    except Exception as e:
        log.warning("alert_resolver: channel.reply failed: %s", e)
        # If reply itself failed, no point bumping a hit count.
        return True

    try:
        kb.mark_hit(chat_id=parsed.chat_id, entry_id=matched.id)
    except Exception as e:
        log.warning(
            "alert_resolver: mark_hit failed for %s/%s: %s",
            parsed.chat_id, matched.id, e,
        )

    return True


# ---------------------------------------------------------------------------
# Sink path
# ---------------------------------------------------------------------------


async def sink_after_deep(
    parsed: ParsedMsg, conclusion: str, *, kb: AlertKB,
) -> AlertEntry | None:
    """Append the (alert, conclusion) pair to kb. Never raises.

    The conclusion is what the bot just replied to the alert group —
    using it verbatim avoids a second Claude call for summarisation and
    matches the spec's "no per-source adaptation" rule.

    Conclusions that look like timeout/failure sentinels are NOT sinked:
    a junk conclusion (e.g. "⚠️ 分析超时") would otherwise be retrieved
    by future polls, fed to the judge, and either falsely matched or
    trigger another round of wasted analysis. Reject early.
    """
    if _is_failure_conclusion(conclusion):
        log.info(
            "alert_resolver: skipping sink for failure-shaped conclusion "
            "(len=%d, msg=%s)", len(conclusion or ""), parsed.message_id,
        )
        return None
    try:
        return kb.add(
            chat_id=parsed.chat_id,
            alert_text=parsed.text,
            conclusion=conclusion,
            source_message_id=parsed.message_id,
        )
    except Exception as e:
        log.warning("alert_resolver: sink_after_deep failed: %s", e)
        return None


# Sentinels that indicate the conclusion is not a real analysis result
# but a runtime fallback ("分析超时" / claude error / empty answer).
# Sinking these would poison the kb — retriever would surface them and
# judge would burn cycles dismissing them.
_FAILURE_PHRASES = (
    "分析超时",
    "claude stream timed out",
    "claude stream failed",
    "claude 错误",
    "no answer",
    "verifier 未跑通",
)
_MIN_CONCLUSION_LEN = 30


def _is_failure_conclusion(conclusion: str | None) -> bool:
    if not conclusion:
        return True
    text = conclusion.strip()
    if len(text) < _MIN_CONCLUSION_LEN:
        return True
    lowered = text.lower()
    return any(p.lower() in lowered for p in _FAILURE_PHRASES)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------


def _seconds_until_next_local_hour(
    hour: int, *, now_fn: Callable[[], float] = time.time
) -> float:
    now = datetime.fromtimestamp(now_fn())
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def run_sweep_loop(
    kbs: list[AlertKB],
    alert_cfg: dict,
    *,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Daily sweep at the configured local hour. Runs forever; expected to
    be wrapped in asyncio.create_task by the scheduler and cancelled on
    shutdown. Test injection via now_fn / sleep_fn."""
    sweep_cfg = (alert_cfg or {}).get("sweep") or {}
    hour = int(sweep_cfg.get("hour", 4))
    ttl_seconds = int(alert_cfg.get("ttl_days", 14)) * 86400

    while True:
        delay = _seconds_until_next_local_hour(hour, now_fn=now_fn)
        await sleep_fn(delay)
        for kb in kbs:
            try:
                purged = kb.sweep(ttl_seconds=ttl_seconds)
                log.info("alert kb sweep: root=%s purged=%d", kb.root, purged)
            except Exception as e:
                log.warning("alert kb sweep failed for %s: %s", kb.root, e)
