"""Thread root_id -> Claude session_id persistence.

MUST call configure() before any put/get/cleanup_expired.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

_sessions: dict = {}
_path: Path | None = None


def configure(path: Path) -> None:
    """Set persistence path and load existing sessions from disk.

    If the file is corrupted, rename it to {path}.corrupted.{ts} and start
    with empty sessions to avoid silently overwriting historical data.

    MUST be called before any put/get/cleanup_expired.
    """
    global _path
    _path = Path(path)
    _sessions.clear()
    if not _path.exists():
        return
    try:
        _sessions.update(json.loads(_path.read_text()))
    except (json.JSONDecodeError, OSError) as e:
        import time as _time
        backup = _path.with_suffix(f"{_path.suffix}.corrupted.{int(_time.time())}")
        log.error(
            "failed to load %s (%s); renamed to %s; starting with empty sessions",
            _path, e, backup,
        )
        try:
            _path.rename(backup)
        except OSError:
            log.exception("also failed to backup corrupted file")


def put(thread_root_id: str, session_id: str, *, agent: str, created_at: float | None = None) -> None:
    """Persist a session mapping.

    `created_at` override exists for testing expired-cleanup; production
    callers should omit it (uses time.time()).
    """
    _sessions[thread_root_id] = {
        "session_id": session_id,
        "agent": agent,
        "created_at": created_at if created_at is not None else time.time(),
    }
    _save()


def get(thread_root_id: str) -> dict | None:
    return _sessions.get(thread_root_id)


def cleanup_expired(max_age: int = 86400) -> None:
    """Remove sessions older than max_age seconds."""
    now = time.time()
    expired = [k for k, v in _sessions.items() if now - v.get("created_at", 0) > max_age]
    for k in expired:
        del _sessions[k]
    if expired:
        log.info("cleaned up %d expired session(s)", len(expired))
        _save()


def _save() -> None:
    """Atomic write to _path."""
    if _path is None:
        raise RuntimeError("session.configure() must be called before put/cleanup")
    try:
        tmp = _path.with_suffix(_path.suffix + ".tmp")
        tmp.write_text(json.dumps(_sessions, ensure_ascii=False, indent=2))
        tmp.replace(_path)
    except OSError:
        log.exception("failed to save %s; session data NOT persisted", _path)
        raise  # fail-fast: caller (scheduler) decides how to handle


def reset() -> None:
    """Clear in-memory sessions only. _path survives so test fixtures stay valid."""
    _sessions.clear()
