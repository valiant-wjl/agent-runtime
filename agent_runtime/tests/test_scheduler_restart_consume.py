"""US-sched-consume-001: restart_consume cancels old consume tasks and
rebuilds them against the (reloaded) cfg["projects"]."""

from __future__ import annotations

import asyncio

import pytest

from agent_runtime import scheduler


@pytest.mark.asyncio
async def test_rebuild_consume_cancels_old_and_uses_new_projects():
    # A long-lived fake consume that records which projects dict it saw and
    # blocks until cancelled (mirrors the real consume reconnect loop).
    seen_projects: list[dict] = []
    started = asyncio.Event()

    async def fake_consume(channel, projects, *a, **kw):
        seen_projects.append(projects)
        started.set()
        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            raise

    class _Ch:
        name = "feishu"

    ch = _Ch()
    holder: dict[str, asyncio.Task | None] = {"feishu": None}

    # Initial build with old projects.
    old_projects = {"p_old": {"work_dir": "/tmp/old"}}
    await scheduler._rebuild_consume_tasks(
        channels=[ch],
        projects=old_projects,
        holder=holder,
        make_task=lambda c, p: asyncio.create_task(fake_consume(c, p)),
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    old_task = holder["feishu"]
    assert old_task is not None and not old_task.done()
    assert seen_projects[0] is old_projects

    # Rebuild with NEW projects — old task must be cancelled, new created.
    started.clear()
    new_projects = {"p_new": {"work_dir": "/tmp/new"}}
    await scheduler._rebuild_consume_tasks(
        channels=[ch],
        projects=new_projects,
        holder=holder,
        make_task=lambda c, p: asyncio.create_task(fake_consume(c, p)),
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert old_task.cancelled()
    new_task = holder["feishu"]
    assert new_task is not None and new_task is not old_task
    assert seen_projects[1] is new_projects

    # cleanup
    new_task.cancel()
    try:
        await new_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_scheduler_context_restart_consume_invokes_fn():
    from unittest.mock import AsyncMock
    from pathlib import Path

    fn = AsyncMock()
    ctx = scheduler.SchedulerContext(
        cfg={}, config_path=Path("config.yaml"),
        backup_dir=Path("bak"),
        restart_consume_fn=fn,
    )
    await ctx.restart_consume()
    fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduler_context_restart_consume_noop_when_unset():
    from pathlib import Path

    ctx = scheduler.SchedulerContext(
        cfg={}, config_path=Path("config.yaml"), backup_dir=Path("bak"),
    )
    # Should not raise when restart_consume_fn is None.
    await ctx.restart_consume()
