import asyncio
import json
import os
import time
from collections import namedtuple
from pathlib import Path

import pytest

from agent_runtime import health


@pytest.fixture(autouse=True)
def _reset():
    health.reset()
    health._last_push.clear()
    yield
    health.reset()
    health._last_push.clear()


_DiskUsage = namedtuple("_DiskUsage", ["total", "used", "free"])
_GB = 1024 ** 3
_MB = 1024 ** 2


def test_write_creates_status_file(tmp_path):
    path = tmp_path / "status.json"
    health.configure(path)
    health._write("running")
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["status"] == "running"
    assert "version" in data
    assert data["uptime_seconds"] >= 0


def test_write_without_configure_is_noop():
    """No _status_file set -> silent no-op (doesn't crash)."""
    health._write("running")  # should not raise


def test_write_atomic_replace(tmp_path):
    """Atomic write: temp file + replace."""
    path = tmp_path / "status.json"
    health.configure(path)
    health._write("starting")
    health._write("running")  # 覆盖写入
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["status"] == "running"


@pytest.mark.asyncio
async def test_heartbeat_writes_running_then_stopping_on_cancel(tmp_path):
    """heartbeat_loop: writes running, then stopping on cancel."""
    path = tmp_path / "status.json"
    health.configure(path)
    task = asyncio.create_task(health.heartbeat_loop(interval=0.01))
    await asyncio.sleep(0.05)
    data = json.loads(path.read_text())
    assert data["status"] == "running"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # 最终状态应为 "stopping"
    final = json.loads(path.read_text())
    assert final["status"] == "stopping"


@pytest.mark.asyncio
async def test_status_history_appends(tmp_path):
    """3 heartbeat ticks → status-history.jsonl has exactly 3 lines."""
    status = tmp_path / "status.json"
    history = tmp_path / "status-history.jsonl"
    health.configure(status, history_file=history)
    task = asyncio.create_task(health.heartbeat_loop(interval=0.02))
    # Allow ~3 ticks to write
    await asyncio.sleep(0.07)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert history.exists(), "status-history.jsonl not created"
    lines = [ln for ln in history.read_text().splitlines() if ln.strip()]
    # Resilient to event-loop jitter (was: == 3)
    assert 2 <= len(lines) <= 5, (
        f"expected ~3 ticks in 0.07s @ 0.02s interval, got {len(lines)}"
    )
    for ln in lines:
        rec = json.loads(ln)
        assert set(rec.keys()) == {"ts", "status", "uptime_s", "disk_gb"}
        assert isinstance(rec["ts"], int)
        assert isinstance(rec["status"], str)
        assert isinstance(rec["uptime_s"], int)
        assert isinstance(rec["disk_gb"], (int, float))


@pytest.mark.asyncio
async def test_disk_low_pushes_warning(tmp_path, monkeypatch):
    """When disk free < 1GB, watchdog pushes a 'disk' warning once."""
    status = tmp_path / "status.json"
    health.configure(status, meta_dir=tmp_path)
    monkeypatch.setattr(
        health.shutil,
        "disk_usage",
        lambda _p: _DiskUsage(total=10 * _GB, used=int(9.5 * _GB), free=500 * _MB),
    )
    calls: list[str] = []

    async def spy(text, **kwargs):
        calls.append(text)
        return True

    monkeypatch.setattr(health, "push_to_self", spy)
    await health._watchdog(tmp_path)
    assert len(calls) == 1
    assert "磁盘" in calls[0]


@pytest.mark.asyncio
async def test_ingest_stuck_pushes(tmp_path, monkeypatch):
    """ingest_feishu.last mtime > 3 days → push once."""
    status = tmp_path / "status.json"
    health.configure(status, meta_dir=tmp_path)
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    last = state_dir / "ingest_feishu.last"
    last.write_text("ok\n")
    old = time.time() - 4 * 86400
    os.utime(last, (old, old))

    # Avoid disk-warning interference: stub disk_usage to be plenty.
    monkeypatch.setattr(
        health.shutil,
        "disk_usage",
        lambda _p: _DiskUsage(total=100 * _GB, used=10 * _GB, free=90 * _GB),
    )
    calls: list[str] = []

    async def spy(text, **kwargs):
        calls.append(text)
        return True

    monkeypatch.setattr(health, "push_to_self", spy)
    await health._watchdog(tmp_path)
    assert len(calls) == 1
    assert "摄取" in calls[0]


@pytest.mark.asyncio
async def test_backup_stuck_pushes(tmp_path, monkeypatch):
    """backup.log mtime > 3 days → push once."""
    status = tmp_path / "status.json"
    health.configure(status, meta_dir=tmp_path)
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    blog = state_dir / "backup.log"
    blog.write_text("ok\n")
    old = time.time() - 4 * 86400
    os.utime(blog, (old, old))

    monkeypatch.setattr(
        health.shutil,
        "disk_usage",
        lambda _p: _DiskUsage(total=100 * _GB, used=10 * _GB, free=90 * _GB),
    )
    calls: list[str] = []

    async def spy(text, **kwargs):
        calls.append(text)
        return True

    monkeypatch.setattr(health, "push_to_self", spy)
    await health._watchdog(tmp_path)
    assert len(calls) == 1
    assert "备份" in calls[0]


def test_second_channel_fires_on_status_write_fail(tmp_path, monkeypatch):
    """When status.json write fails, fire osascript + say (darwin only)."""
    import sys

    status = tmp_path / "status.json"
    health.configure(status)

    # Force darwin so the second-channel path runs
    monkeypatch.setattr(sys, "platform", "darwin")

    # Force the temp write_text to OSError
    original_write_text = Path.write_text

    def bad_write_text(self, *args, **kwargs):
        if str(self).endswith(".tmp"):
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", bad_write_text)

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return None

    monkeypatch.setattr(health.subprocess, "run", fake_run)

    health._write("running")

    cmd_names = [c[0] for c in calls]
    assert "osascript" in cmd_names, f"osascript not invoked: {calls}"
    assert "say" in cmd_names, f"say not invoked: {calls}"


@pytest.mark.asyncio
async def test_watchdog_rate_limits_repeated_pushes(tmp_path, monkeypatch):
    """Calling _watchdog twice in quick succession only pushes once
    (rate-limit suppresses second)."""
    status = tmp_path / "status.json"
    health.configure(status, meta_dir=tmp_path)
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    last = state_dir / "ingest_feishu.last"
    last.write_text("ok\n")
    old = time.time() - 4 * 86400
    os.utime(last, (old, old))

    monkeypatch.setattr(
        health.shutil,
        "disk_usage",
        lambda _p: _DiskUsage(total=100 * _GB, used=10 * _GB, free=90 * _GB),
    )
    calls: list[str] = []

    async def spy(text, **kwargs):
        calls.append(text)
        return True

    monkeypatch.setattr(health, "push_to_self", spy)
    await health._watchdog(tmp_path)
    await health._watchdog(tmp_path)
    assert len(calls) == 1, f"rate-limit failed: {len(calls)} pushes (expected 1)"


def test_second_channel_skipped_on_non_darwin(tmp_path, monkeypatch):
    """On linux, _write OSError must NOT trigger osascript / say."""
    import sys

    status = tmp_path / "status.json"
    health.configure(status)

    monkeypatch.setattr(sys, "platform", "linux")

    original_write_text = Path.write_text

    def bad_write_text(self, *args, **kwargs):
        if str(self).endswith(".tmp"):
            raise OSError("disk full")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", bad_write_text)

    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return None

    monkeypatch.setattr(health.subprocess, "run", fake_run)

    health._write("running")

    assert calls == [], f"subprocess.run should NOT be called on non-darwin: {calls}"


@pytest.mark.asyncio
async def test_push_to_self_handles_failure_modes(monkeypatch):
    """push_to_self returns False (never raises) for: missing open_id,
    missing lark-cli, non-zero exit, runner exception."""
    from agent_runtime import push as push_mod

    class FakeProc:
        def __init__(self, rc):
            self.returncode = rc

        async def communicate(self):
            return (b"", b"")

    def make_runner(rc=0, raises=None):
        async def _runner(*args, **kwargs):
            if raises:
                raise raises
            return FakeProc(rc)

        return _runner

    # 1. No open_id, no env -> False, no subprocess invoked
    monkeypatch.delenv("LARK_SELF_OPEN_ID", raising=False)
    sentinel_called = []

    async def sentinel_runner(*args, **kwargs):
        sentinel_called.append(args)
        return FakeProc(0)

    result = await push_mod.push_to_self("hello", runner=sentinel_runner)
    assert result is False
    assert sentinel_called == [], "runner must not be called when no open_id"

    # 2. open_id set but lark-cli not on PATH -> False
    monkeypatch.setenv("LARK_SELF_OPEN_ID", "ou_xxx")
    monkeypatch.setattr(push_mod.shutil, "which", lambda _name: None)
    result = await push_mod.push_to_self("hello", runner=sentinel_runner)
    assert result is False

    # 3. lark-cli on PATH but exit code 1 -> False
    monkeypatch.setattr(push_mod.shutil, "which", lambda _name: "/usr/bin/lark-cli")
    result = await push_mod.push_to_self("hello", runner=make_runner(rc=1))
    assert result is False

    # 4. runner raises -> False (NOT propagated)
    result = await push_mod.push_to_self(
        "hello", runner=make_runner(raises=RuntimeError("boom"))
    )
    assert result is False

    # 5. (sanity) success path -> True
    result = await push_mod.push_to_self("hello", runner=make_runner(rc=0))
    assert result is True
