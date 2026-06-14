"""P1 concurrency regression: consume() must dispatch messages concurrently.

Bug P (docs/specs/2026-05-12-digital-agent-observer-design.md § 14.7):
consume() previously `await`-ed handle_message inline, which serialized
every message in a single channel — defeating the global / per-chat
semaphores in `runtime.concurrency`. Alert chats that bursted 10 cards
had to wait for the 9th to finish before the 10th was even dispatched,
breaking alert SLA.

These tests pin the post-fix contract:
  - consume() dispatches each message via asyncio.create_task so 3
    in-flight handlers complete in ~max(handler_latency), not
    sum(handler_latency).
  - In-flight tasks are tracked in a module-level set and exposed for a
    graceful drain (so SIGTERM doesn't kill alerts half-way).
  - Handler exceptions are caught via done_callback; one bad message
    must not crash the consume loop.
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import concurrency, scheduler


_PROJECT_CFG = {
    "work_dir": "/tmp/billing",
    "display_name": "BillingBot",
    "model": "opus",
    "admin_users": [],
    "approval_timeout": 1800,
    "read_phase": {"disallowed_tools": [], "disallowed_bash_patterns": []},
    "write_phase": {"timeout": 600},
    "supported_msg_types": ["text"],
    "unsupported_msg_reply": "no",
}

_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


def _parsed(i: int) -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id=f"m-{i}",
        thread_root_id=f"t-{i}",
        chat_id="c-burst",
        sender_id="ou-x",
        sender_name="x",
        text=f"alert-{i}",
        mentions=[],
        chat_type="p2p",
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


@pytest.fixture(autouse=True)
def _init_sem():
    # Global=5, per_chat=2 keep the test under semaphore caps; with 3
    # bursted messages there is enough headroom for true concurrency.
    concurrency.init_global(5)
    yield


@pytest.mark.asyncio
async def test_consume_dispatches_concurrently_within_budget():
    """3 messages, each handler sleeps 500ms → all done within 1.0s wall time.

    Serial (pre-fix) wall time would be ~1.5s. The 1.0s budget proves the
    handlers actually overlap; we don't tighten to 600ms to keep CI noise
    out (slow runners, GC pauses).
    """
    parsed_msgs = [_parsed(i) for i in range(3)]

    async def _events():
        for _ in parsed_msgs:
            yield {"raw": "ev"}

    ch = AsyncMock()
    ch.name = "feishu"
    ch.subscribe = lambda: _events()

    parse_iter = iter(parsed_msgs)
    ch.parse = AsyncMock(side_effect=lambda _e: next(parse_iter))
    # Exit consume's while-True loop after the iterator drains.
    ch.close = AsyncMock(side_effect=asyncio.CancelledError())

    handler_calls: list[str] = []

    async def _slow_handler(_ch, parsed, *args, **kwargs):
        handler_calls.append(parsed.message_id)
        await asyncio.sleep(0.5)

    start = time.monotonic()
    with patch(
        "agent_runtime.scheduler.handle_message",
        AsyncMock(side_effect=_slow_handler),
    ):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.consume(
                ch,
                {"billing": _PROJECT_CFG},
                _RUNTIME_CFG,
                {},
                bot_mention_key=None,
            )
        # consume() exits as soon as ch.close() raises — at that point
        # tasks are still in-flight. Drain them so we can assert all 3
        # ran and the wall time was sub-sum.
        await scheduler.drain_in_flight()
    elapsed = time.monotonic() - start

    assert sorted(handler_calls) == ["m-0", "m-1", "m-2"], handler_calls
    assert elapsed < 1.0, (
        f"expected concurrent dispatch (<1.0s); got {elapsed:.3f}s "
        f"— probably still serial (3 * 500ms ≈ 1.5s)"
    )


@pytest.mark.asyncio
async def test_consume_handler_exception_does_not_crash_loop(caplog):
    """One handler raising must be logged and not block subsequent dispatch."""
    parsed_msgs = [_parsed(0), _parsed(1)]

    async def _events():
        for _ in parsed_msgs:
            yield {"raw": "ev"}

    ch = AsyncMock()
    ch.name = "feishu"
    ch.subscribe = lambda: _events()
    parse_iter = iter(parsed_msgs)
    ch.parse = AsyncMock(side_effect=lambda _e: next(parse_iter))
    ch.close = AsyncMock(side_effect=asyncio.CancelledError())

    seen: list[str] = []

    async def _handler(_ch, parsed, *args, **kwargs):
        seen.append(parsed.message_id)
        if parsed.message_id == "m-0":
            raise RuntimeError("boom")

    caplog.set_level(logging.ERROR, logger="agent_runtime.scheduler")

    with patch(
        "agent_runtime.scheduler.handle_message",
        AsyncMock(side_effect=_handler),
    ):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.consume(
                ch,
                {"billing": _PROJECT_CFG},
                _RUNTIME_CFG,
                {},
                bot_mention_key=None,
            )
        await scheduler.drain_in_flight()

    assert sorted(seen) == ["m-0", "m-1"], seen
    crash_logs = [
        r for r in caplog.records if "boom" in (r.message + str(r.exc_info))
    ]
    assert crash_logs, (
        f"expected handler exception to be logged; got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_drain_in_flight_waits_for_pending_tasks():
    """drain_in_flight() must await all queued handlers before returning.

    Simulates SIGTERM hitting just after a burst dispatch: consume's
    while-True exits, but in-flight handlers must still finish.
    """
    parsed_msgs = [_parsed(i) for i in range(3)]

    async def _events():
        for _ in parsed_msgs:
            yield {"raw": "ev"}

    ch = AsyncMock()
    ch.name = "feishu"
    ch.subscribe = lambda: _events()
    parse_iter = iter(parsed_msgs)
    ch.parse = AsyncMock(side_effect=lambda _e: next(parse_iter))
    ch.close = AsyncMock(side_effect=asyncio.CancelledError())

    completed: list[str] = []

    async def _handler(_ch, parsed, *args, **kwargs):
        await asyncio.sleep(0.2)
        completed.append(parsed.message_id)

    with patch(
        "agent_runtime.scheduler.handle_message",
        AsyncMock(side_effect=_handler),
    ):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.consume(
                ch,
                {"billing": _PROJECT_CFG},
                _RUNTIME_CFG,
                {},
                bot_mention_key=None,
            )
        # Before draining, handlers may not be done yet.
        await scheduler.drain_in_flight()

    assert sorted(completed) == ["m-0", "m-1", "m-2"], completed
    # The set must be empty after drain so a re-entered scheduler starts clean.
    assert not scheduler._in_flight, (
        f"_in_flight not cleared after drain: {scheduler._in_flight}"
    )
