"""status.json heartbeat + watchdog (M2 basic; M9 扩展)."""

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from agent_runtime import __version__
from agent_runtime.push import push_to_self

log = logging.getLogger(__name__)

_status_file: Path | None = None
_history_file: Path | None = None
_meta_dir: Path | None = None
_start_time = time.time()
_global_status: str = "running"  # may be set to "full_halt" by watchdog

# rate-limit dicts: check_name -> last push timestamp (epoch seconds)
_last_push: dict[str, float] = {}

# Thresholds
_GB = 1024 ** 3
_MB = 1024 ** 2
_DISK_WARN_THRESHOLD = 1 * _GB     # < 1GB → warn
_DISK_HALT_THRESHOLD = 100 * _MB   # < 100MB → halt
_PUSH_HOUR = 3600
_PUSH_DAY = 86400
_STUCK_THRESHOLD_SEC = 3 * 86400   # 3 days


def configure(
    path: Path,
    *,
    history_file: Path | None = None,
    meta_dir: Path | None = None,
) -> None:
    """Set the status.json output path. Call from main() once.

    `history_file` defaults to sibling `status-history.jsonl` of `path`.
    `meta_dir` enables watchdog checks (ingest/backup stuck files); None
    means watchdog is no-op (back-compat).
    """
    global _status_file, _history_file, _meta_dir
    _status_file = Path(path)
    _status_file.parent.mkdir(parents=True, exist_ok=True)
    if history_file is None:
        _history_file = _status_file.parent / "status-history.jsonl"
    else:
        _history_file = Path(history_file)
    _meta_dir = Path(meta_dir) if meta_dir is not None else None


async def heartbeat_loop(interval: int = 30) -> None:
    """Run forever; write status.json every `interval` seconds.

    Scheduler should asyncio.create_task(health.heartbeat_loop()). It's
    cancelled when the scheduler stops (asyncio.CancelledError).
    """
    _write("starting")
    while True:
        _write(_global_status)
        _append_history(_global_status)
        try:
            await _watchdog(_meta_dir)
        except Exception as e:
            log.warning("watchdog error: %r", e)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            _write("stopping")
            raise


def _write(status: str) -> None:
    if _status_file is None:
        return
    data = {
        "status": status,
        "version": __version__,
        "uptime_seconds": int(time.time() - _start_time),
        "timestamp": int(time.time()),
    }
    try:
        tmp = _status_file.with_suffix(_status_file.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_status_file)
    except OSError as e:
        log.warning("failed to write status.json: %s", e)
        _second_channel_alert("digital-agent offline")


def _append_history(status: str) -> None:
    """Append one JSON line per heartbeat to status-history.jsonl.

    Monthly rotation: on day-of-month==1, if the existing history file's
    mtime is > 7 days old, rename to status-history-YYYY-MM.jsonl
    (last month's stamp) and start fresh.
    """
    if _history_file is None:
        return
    try:
        # Monthly rotation check
        if _history_file.exists():
            now = datetime.now()
            mtime = datetime.fromtimestamp(_history_file.stat().st_mtime)
            if now.day == 1 and (now - mtime) > timedelta(days=7):
                # Use last month's stamp from current mtime
                stamp = mtime.strftime("%Y-%m")
                rotated = _history_file.with_name(
                    f"status-history-{stamp}.jsonl"
                )
                _history_file.rename(rotated)

        try:
            disk = shutil.disk_usage(".")
            disk_gb = round(disk.free / _GB, 2)
        except Exception:
            disk_gb = 0.0
        line = {
            "ts": int(time.time()),
            "status": status,
            "uptime_s": int(time.time() - _start_time),
            "disk_gb": disk_gb,
        }
        with _history_file.open("a") as f:
            f.write(json.dumps(line) + "\n")
    except OSError as e:
        log.warning("failed to append status-history: %s", e)


async def _watchdog(meta_dir: Path | None) -> None:
    """Run all watchdog checks. No-op if meta_dir is None."""
    # Disk check (always runs; doesn't depend on meta_dir)
    await _check_disk()
    if meta_dir is None:
        return
    await _check_stuck(
        meta_dir / ".state" / "ingest_feishu.last",
        check_name="ingest",
        text="⚠️ 飞书数据摄取已停滞：超过 3 天没有更新（ingest_feishu）",
        cooldown=_PUSH_DAY,
    )
    await _check_stuck(
        meta_dir / ".state" / "backup.log",
        check_name="backup",
        text="⚠️ 备份已停滞：超过 3 天没有更新（backup）",
        cooldown=_PUSH_DAY,
    )


async def _check_disk() -> None:
    global _global_status
    try:
        usage = shutil.disk_usage(".")
    except Exception as e:
        log.warning("disk_usage failed: %r", e)
        return
    if usage.free < _DISK_HALT_THRESHOLD:
        _global_status = "full_halt"
        await _maybe_push(
            "disk",
            f"🛑 磁盘空间告急：仅剩 {usage.free // _MB}MB，已进入全面停机（full_halt）",
            cooldown=_PUSH_HOUR,
        )
    elif usage.free < _DISK_WARN_THRESHOLD:
        await _maybe_push(
            "disk",
            f"⚠️ 磁盘空间不足：仅剩 {usage.free // _MB}MB",
            cooldown=_PUSH_HOUR,
        )


async def _check_stuck(
    path: Path, *, check_name: str, text: str, cooldown: float
) -> None:
    if not path.exists():
        return
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return
    if age > _STUCK_THRESHOLD_SEC:
        await _maybe_push(check_name, text, cooldown=cooldown)


async def _maybe_push(check_name: str, text: str, *, cooldown: float) -> None:
    """Rate-limited self-push: skip if last push for this check_name was
    within `cooldown` seconds."""
    now = time.time()
    last = _last_push.get(check_name, 0.0)
    if now - last < cooldown:
        return
    _last_push[check_name] = now
    try:
        await push_to_self(text)
    except Exception as e:
        log.warning("push_to_self raised: %r", e)


def _second_channel_alert(message: str) -> None:
    """Best-effort macOS notification + spoken alert. Skip on non-darwin."""
    if sys.platform != "darwin":
        return
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{message}" with title "digital-agent"',
            ],
            check=False,
            timeout=5,
        )
    except Exception as e:
        log.warning("osascript alert failed: %r", e)
    try:
        subprocess.run(
            ["say", "digital agent offline"], check=False, timeout=5
        )
    except Exception as e:
        log.warning("say alert failed: %r", e)


def reset() -> None:
    """For tests."""
    global _status_file, _history_file, _meta_dir, _start_time
    global _global_status, _last_push
    _status_file = None
    _history_file = None
    _meta_dir = None
    _start_time = time.time()
    _global_status = "running"
    _last_push = {}
