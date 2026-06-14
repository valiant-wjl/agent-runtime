"""US-poll-005: scheduler.run_alert_polling_loop — periodic poll → dispatch.

The loop must:
  - cold-start each chat exactly once (skip_history vs last_24h)
  - serially dispatch returned messages via handle_message
  - advance the cursor based on the highest create_time it dispatched
  - swallow per-iteration errors (handle_message crash, poll crash)
  - respect interval_seconds via injectable sleep_fn / now_fn
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import asyncio
import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu.poller import PollerCursor
from agent_runtime import scheduler


_ALERT_CFG_BASE = {
    "enabled": True,
    "ttl_days": 14,
    "retriever": "keyword",
    "top_k": 3,
    "judge_timeout": 60,
    "judge_model": "haiku",
    "alert_chats": [{"chat_id": "oc_alert", "project": "spring_billing"}],
    "polling": {
        "enabled": True,
        "interval_seconds": 30,
        "page_size": 20,
        "cold_start": "skip_history",
        "max_initial_ingest": 30,
    },
}


def _project_cfg(work_dir: Path) -> dict:
    return {
        "work_dir": str(work_dir),
        "model": "opus",
        "admin_users": ["ou_admin"],
        "approval_timeout": 1800,
        "read_phase": {
            "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
            "disallowed_bash_patterns": [],
        },
        "write_phase": {"timeout": 600},
        "supported_msg_types": ["text", "post"],
    }


def _msg(create_time_ms: int, mid: str = "om_x") -> ParsedMsg:
    p = ParsedMsg(
        channel="feishu",
        message_id=mid,
        thread_root_id=mid,
        chat_id="oc_alert",
        sender_id="cli_aily",
        sender_name="cli_aily",
        text=f"alert text {mid}",
        mentions=[],
        chat_type="group",
        sender_type="app",
    )
    p._poll_create_time_ms = create_time_ms  # type: ignore[attr-defined]
    return p


# ---------------------------------------------------------------------------
# Cold-start: skip_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skip_history_seeds_cursor_to_now_on_first_iteration(tmp_path):
    """skip_history mode: chat unseen → cursor set to current time, NO
    history dispatched, then loop sleeps and exits."""
    cursor = PollerCursor(path=tmp_path / "cur.json")
    poll_fn = AsyncMock(return_value=[])
    handle_fn = AsyncMock()

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 1:
            raise asyncio.CancelledError()

    fixed_now_ms = 1700_000_000_000

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=_ALERT_CFG_BASE,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: fixed_now_ms,
            sleep_fn=sleep_fn,
        )

    assert cursor.get("oc_alert") == fixed_now_ms
    handle_fn.assert_not_called()
    # On the first iteration after seeding, poll_chat should NOT be invoked
    # — we just initialised the cursor, no work to do this tick.
    poll_fn.assert_not_called()


# ---------------------------------------------------------------------------
# Cold-start: last_24h
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_24h_seeds_cursor_to_24h_ago(tmp_path):
    """last_24h mode: chat unseen → cursor = now - 24h, then loop polls."""
    cfg = {**_ALERT_CFG_BASE,
           "polling": {**_ALERT_CFG_BASE["polling"], "cold_start": "last_24h"}}
    cursor = PollerCursor(path=tmp_path / "cur.json")
    poll_fn = AsyncMock(return_value=[])
    handle_fn = AsyncMock()
    now_ms = 1700_000_000_000

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 1:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=cfg,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: now_ms,
            sleep_fn=sleep_fn,
        )

    expected_seed = now_ms - 24 * 60 * 60 * 1000
    assert cursor.get("oc_alert") == expected_seed
    # In last_24h mode, the first iteration DOES poll right after seeding.
    poll_fn.assert_called_once()
    args, kwargs = poll_fn.call_args
    assert kwargs["chat_id"] == "oc_alert"
    assert kwargs["since_ms"] == expected_seed


# ---------------------------------------------------------------------------
# Existing cursor: don't reset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_existing_cursor_is_preserved_across_restarts(tmp_path):
    """A chat that already has a cursor (process restart) must NOT be
    re-seeded by cold_start logic."""
    cursor_path = tmp_path / "cur.json"
    seed = 1_500_000_000_000
    PollerCursor(path=cursor_path).set("oc_alert", seed)

    cursor = PollerCursor(path=cursor_path)
    poll_fn = AsyncMock(return_value=[])
    handle_fn = AsyncMock()

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 1:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=_ALERT_CFG_BASE,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: 9_999_999_999_999,
            sleep_fn=sleep_fn,
        )

    # Cursor unchanged (no new messages from poll, but nothing should reset it)
    assert cursor.get("oc_alert") == seed
    poll_fn.assert_called_once()
    args, kwargs = poll_fn.call_args
    assert kwargs["since_ms"] == seed


# ---------------------------------------------------------------------------
# Dispatch path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_polling_loop_dispatches_messages_serially(tmp_path):
    """Two messages → handle_message called twice, in chronological order;
    cursor advances to the latest dispatched create_time."""
    cursor_path = tmp_path / "cur.json"
    PollerCursor(path=cursor_path).set("oc_alert", 1000)

    cursor = PollerCursor(path=cursor_path)
    msgs = [_msg(2000, "om_a"), _msg(2500, "om_b")]
    poll_fn = AsyncMock(return_value=msgs)
    handle_fn = AsyncMock()

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 1:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=_ALERT_CFG_BASE,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: 3000,
            sleep_fn=sleep_fn,
        )

    assert handle_fn.call_count == 2
    dispatched_ids = [c.kwargs.get("parsed").message_id if c.kwargs.get("parsed") is not None
                      else c.args[1].message_id for c in handle_fn.call_args_list]
    assert dispatched_ids == ["om_a", "om_b"]
    # Cursor advanced to latest seen create_time
    assert cursor.get("oc_alert") == 2500


@pytest.mark.asyncio
async def test_polling_loop_advances_cursor_even_when_handle_crashes(tmp_path):
    """One bad message must not pin the cursor — otherwise the loop would
    re-dispatch the same crash forever."""
    cursor_path = tmp_path / "cur.json"
    PollerCursor(path=cursor_path).set("oc_alert", 1000)

    cursor = PollerCursor(path=cursor_path)
    msgs = [_msg(2000, "om_a"), _msg(2500, "om_b")]
    poll_fn = AsyncMock(return_value=msgs)
    handle_fn = AsyncMock(side_effect=[RuntimeError("boom"), None])

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 1:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=_ALERT_CFG_BASE,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: 3000,
            sleep_fn=sleep_fn,
        )

    assert handle_fn.call_count == 2
    # Both messages "consumed" — cursor at the latest one
    assert cursor.get("oc_alert") == 2500


@pytest.mark.asyncio
async def test_polling_loop_swallows_poll_chat_failure(tmp_path):
    """poll_chat raising must not kill the loop."""
    cursor_path = tmp_path / "cur.json"
    PollerCursor(path=cursor_path).set("oc_alert", 1000)

    cursor = PollerCursor(path=cursor_path)
    poll_fn = AsyncMock(side_effect=RuntimeError("network down"))
    handle_fn = AsyncMock()

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 2:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=_ALERT_CFG_BASE,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: 3000,
            sleep_fn=sleep_fn,
        )

    # Loop kept going across two iterations despite each poll crashing
    assert poll_fn.call_count == 2
    handle_fn.assert_not_called()


# ---------------------------------------------------------------------------
# max_initial_ingest cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_24h_cap_drops_oldest_when_over_limit(tmp_path):
    """In last_24h cold-start mode, if >max_initial_ingest messages come
    back in the FIRST poll, dispatch only the newest N — older ones are
    skipped (cursor still advances past them)."""
    cfg = {**_ALERT_CFG_BASE,
           "polling": {
               **_ALERT_CFG_BASE["polling"],
               "cold_start": "last_24h",
               "max_initial_ingest": 3,
           }}
    cursor = PollerCursor(path=tmp_path / "cur.json")
    # 5 messages — only newest 3 should dispatch
    msgs = [_msg(1000 + i, f"om_{i}") for i in range(5)]
    poll_fn = AsyncMock(return_value=msgs)
    handle_fn = AsyncMock()

    iter_count = {"n": 0}
    async def sleep_fn(s):
        iter_count["n"] += 1
        if iter_count["n"] >= 1:
            raise asyncio.CancelledError()

    now_ms = 1700_000_000_000
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_alert_polling_loop(
            alert_cfg=cfg,
            projects={"spring_billing": _project_cfg(tmp_path)},
            runtime_cfg={"reply_timeout": 300, "session_max_age": 86400},
            features_cfg={},
            cursor=cursor,
            poll_chat_fn=poll_fn,
            handle_message_fn=handle_fn,
            now_ms_fn=lambda: now_ms,
            sleep_fn=sleep_fn,
        )

    assert handle_fn.call_count == 3
    # Newest 3 = om_2, om_3, om_4 (sorted ascending in dispatch order)
    dispatched = [c.args[1].message_id if len(c.args) > 1 else c.kwargs["parsed"].message_id
                  for c in handle_fn.call_args_list]
    assert dispatched == ["om_2", "om_3", "om_4"]
    # Cursor at the newest message's create_time (1004), NOT the now_ms
    # — so a follow-up tick won't dispatch the same window again.
    assert cursor.get("oc_alert") == 1004
