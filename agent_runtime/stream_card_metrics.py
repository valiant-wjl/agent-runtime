"""Tiny counter for stream_card_throttled events (M6-T04).

Persists to ``.state/stream_card_throttled_count`` (single-line int) for the
M9 health watchdog to read. Atomic-ish via tmp+rename.

The path is resolved relative to the process cwd (matching the convention
used by ``runtime.health`` and ``runtime.session`` before configure()).
This keeps the helper trivially testable via ``monkeypatch.chdir(tmp_path)``.
"""

from __future__ import annotations

import pathlib

_STATE_FILE_NAME = pathlib.Path(".state/stream_card_throttled_count")


def _state_file() -> pathlib.Path:
    """Resolve relative to current cwd on each call (test-friendly)."""
    return _STATE_FILE_NAME


def bump_throttled() -> int:
    """Increment counter, return new value. Best-effort, never raises."""
    sf = _state_file()
    try:
        sf.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    current = 0
    if sf.exists():
        try:
            current = int(sf.read_text().strip() or "0")
        except (ValueError, OSError):
            current = 0
    new = current + 1
    try:
        tmp = sf.with_suffix(sf.suffix + ".tmp")
        tmp.write_text(str(new))
        tmp.replace(sf)
    except OSError:
        return current
    return new


def get_throttled() -> int:
    sf = _state_file()
    if not sf.exists():
        return 0
    try:
        return int(sf.read_text().strip() or "0")
    except (ValueError, OSError):
        return 0
