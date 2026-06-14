"""Tests for runtime.decay (M9-T05B)."""

import asyncio
import os
import time
from datetime import date
from pathlib import Path

import pytest

from agent_runtime import decay


def _write_evergreen(meta_dir: Path, body: str) -> Path:
    meta_dir.mkdir(parents=True, exist_ok=True)
    p = meta_dir / "EVERGREEN.md"
    p.write_text(body)
    return p


def test_rotate_moves_expired_events(tmp_path):
    """Expired bullets move to changelog; fresh ones remain."""
    body = (
        "# meta\n"
        "\n"
        "## 近 14 天重要事件\n"
        "- 2026-04-07: old event\n"
        "- 2026-04-17: fresh event\n"
        "\n"
        "## 其他\n"
        "- keep me\n"
    )
    _write_evergreen(tmp_path, body)
    n = decay.rotate_evergreen_events(
        tmp_path, keep_days=14, today=date(2026, 4, 27)
    )
    assert n == 1
    new_text = (tmp_path / "EVERGREEN.md").read_text()
    assert "old event" not in new_text
    assert "fresh event" in new_text
    # Other section preserved
    assert "## 其他" in new_text
    assert "- keep me" in new_text
    changelog = tmp_path / "wiki" / "changelog.md"
    assert changelog.exists()
    cl = changelog.read_text()
    assert "- 2026-04-07: old event" in cl
    assert "rotated from EVERGREEN" in cl


def test_rotate_noop_when_all_fresh(tmp_path):
    """All bullets within window → return 0; EVERGREEN byte-identical; no changelog."""
    body = (
        "# meta\n"
        "\n"
        "## 近 14 天重要事件\n"
        "- 2026-04-20: recent A\n"
        "- 2026-04-25: recent B\n"
    )
    p = _write_evergreen(tmp_path, body)
    original_bytes = p.read_bytes()
    n = decay.rotate_evergreen_events(
        tmp_path, keep_days=14, today=date(2026, 4, 27)
    )
    assert n == 0
    assert p.read_bytes() == original_bytes
    assert not (tmp_path / "wiki" / "changelog.md").exists()


def test_find_stale_inbox(tmp_path):
    """Threshold filters by mtime; only old files returned."""
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    old = inbox / "old.md"
    fresh = inbox / "fresh.md"
    old.write_text("old\n")
    fresh.write_text("fresh\n")
    now = time.time()
    os.utime(old, (now, now - 35 * 86400))
    os.utime(fresh, (now, now - 5 * 86400))
    result = decay.find_stale_inbox(tmp_path, threshold_days=30, now=now)
    assert result == [old]


def test_rotate_writes_changelog_before_evergreen(tmp_path, monkeypatch):
    """Cross-file atomicity: if EVERGREEN write fails, changelog is already
    written → duplicate-on-retry instead of silent data loss."""
    body = (
        "# meta\n"
        "\n"
        "## 近 14 天重要事件\n"
        "- 2026-04-01: very old event\n"
    )
    p = _write_evergreen(tmp_path, body)
    original_bytes = p.read_bytes()
    evergreen_path = tmp_path / "EVERGREEN.md"

    real_atomic_write = decay._atomic_write

    def failing_atomic_write(path, text):
        if Path(path) == evergreen_path:
            raise OSError("simulated EVERGREEN write failure")
        return real_atomic_write(path, text)

    monkeypatch.setattr(decay, "_atomic_write", failing_atomic_write)

    with pytest.raises(OSError, match="simulated EVERGREEN write failure"):
        decay.rotate_evergreen_events(
            tmp_path, keep_days=14, today=date(2026, 4, 27)
        )

    # changelog should exist with rotated entry (proves changelog written first)
    changelog = tmp_path / "wiki" / "changelog.md"
    assert changelog.exists(), "changelog must be written BEFORE EVERGREEN"
    cl = changelog.read_text()
    assert "- 2026-04-01: very old event" in cl
    # EVERGREEN must be byte-identical (proves the second write was the one that failed)
    assert p.read_bytes() == original_bytes


@pytest.mark.asyncio
async def test_decay_loop_pushes_on_stale(tmp_path, monkeypatch):
    """Loop calls push_to_self exactly once with text mentioning 'stale'/'inbox'."""
    # Empty EVERGREEN
    _write_evergreen(tmp_path, "# empty\n")
    # Stale inbox file
    inbox = tmp_path / "raw" / "inbox"
    inbox.mkdir(parents=True)
    stale = inbox / "old.md"
    stale.write_text("old\n")
    now = time.time()
    os.utime(stale, (now, now - 60 * 86400))

    calls: list[str] = []

    async def spy(text, **kwargs):
        calls.append(text)
        return True

    monkeypatch.setattr(decay, "push_to_self", spy)

    task = asyncio.create_task(decay.decay_loop(tmp_path, interval_s=3600))
    # Give loop time to run one iteration
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) == 1, f"expected 1 push, got {len(calls)}: {calls}"
    msg = calls[0].lower()
    assert "stale" in msg or "inbox" in msg
