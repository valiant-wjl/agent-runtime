"""Tests for scheduler image-cache GC (R2-S4).

Stale `<work_dir>/.cache/images/<msg_id>/` subdirs (mtime > 24h) get removed
on first handle_message call per project. Idempotent: re-running is a no-op.
"""

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import concurrency, scheduler, session
from agent_runtime.claude_proc import RunResult


def _project_cfg(work_dir: Path) -> dict:
    return {
        "work_dir": str(work_dir),
        "display_name": "Bot",
        "model": "opus",
        "admin_users": [],
        "approval_timeout": 1800,
        "read_phase": {"disallowed_tools": [], "disallowed_bash_patterns": []},
        "write_phase": {"timeout": 600},
    }


_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


def _parsed():
    return ParsedMsg(
        channel="feishu", message_id="m-gc",
        thread_root_id="t-gc", chat_id="c-1",
        sender_id="ou-x", sender_name="x",
        text="hi", mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


def _make_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.reply = AsyncMock()
    ch.send_card = AsyncMock(return_value="card-1")
    ch.fetch_topic_history = AsyncMock(return_value=[])
    ch.download_image = AsyncMock()
    return ch


@pytest.fixture(autouse=True)
def _init_concurrency_and_reset_gc():
    concurrency.init_global(10)
    # Reset module-level GC state between tests to keep them independent.
    scheduler._gc_done_work_dirs.clear()
    yield


@pytest.mark.asyncio
async def test_cache_gc_removes_stale_dir(tmp_path):
    """A subdir whose mtime is > 24h old gets removed on first handle_message."""
    session.configure(tmp_path / "sess.json")
    cache = tmp_path / ".cache" / "images" / "om_old_msg"
    cache.mkdir(parents=True)
    (cache / "img_a.png").write_bytes(b"x")
    # Backdate mtime to 2 days ago
    old = time.time() - 2 * 86400
    os.utime(cache, (old, old))

    ch = _make_channel()
    fake = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        await scheduler.handle_message(
            ch, _parsed(), "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    assert not cache.exists()


@pytest.mark.asyncio
async def test_cache_gc_preserves_fresh_dir(tmp_path):
    """A subdir whose mtime is < 24h old is kept (might be in-flight)."""
    session.configure(tmp_path / "sess.json")
    cache = tmp_path / ".cache" / "images" / "om_recent"
    cache.mkdir(parents=True)
    (cache / "img_b.png").write_bytes(b"y")
    # Default mtime = now; do nothing

    ch = _make_channel()
    fake = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        await scheduler.handle_message(
            ch, _parsed(), "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    assert cache.exists()


@pytest.mark.asyncio
async def test_cache_gc_skips_missing_parent(tmp_path):
    """No `.cache/images/` dir at all → GC is a no-op (no crash)."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    fake = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        # Should not raise
        await scheduler.handle_message(
            ch, _parsed(), "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )


@pytest.mark.asyncio
async def test_cache_gc_runs_once_per_work_dir(tmp_path):
    """Second handle_message for same work_dir is a no-op — proven by:
    after the first call, planting a fresh stale dir and confirming the
    second call leaves it intact (the GC won't re-scan)."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    fake = RunResult(text="ok", session_id="s1", exit_code=0)
    cfg = _project_cfg(tmp_path)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        # First call → GC runs, set marks work_dir done.
        await scheduler.handle_message(ch, _parsed(), "p", cfg, _RUNTIME_CFG)
        assert str(tmp_path) in scheduler._gc_done_work_dirs

        # Plant a stale dir that the second call WOULD delete if GC ran again.
        stale = tmp_path / ".cache" / "images" / "om_planted"
        stale.mkdir(parents=True)
        old = time.time() - 2 * 86400
        os.utime(stale, (old, old))

        # Second call → GC must NOT re-scan; planted stale dir survives.
        await scheduler.handle_message(ch, _parsed(), "p", cfg, _RUNTIME_CFG)
    assert stale.exists(), "GC re-ran on second call; idempotency violated"
