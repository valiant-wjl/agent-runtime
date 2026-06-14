"""EVERGREEN.md rotation + stale inbox detection (M9 spec §7.3)."""

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from agent_runtime.push import push_to_self

log = logging.getLogger(__name__)

# Section header recognition: "## 近 14 天重要事件" or "## Recent Events"
# Case-insensitive, allow trailing whitespace.
_SECTION_PATTERNS = [
    re.compile(r"^##\s*近\s*\d+\s*天重要事件\s*$", re.IGNORECASE),
    re.compile(r"^##\s*Recent\s+Events\s*$", re.IGNORECASE),
]
_BULLET_DATE_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2})\b")
_NEXT_SECTION_RE = re.compile(r"^##\s+")


def _is_target_section(line: str) -> bool:
    return any(p.match(line) for p in _SECTION_PATTERNS)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def rotate_evergreen_events(
    meta_dir: Path,
    keep_days: int = 14,
    *,
    today: date | None = None,
) -> int:
    """Scan meta/EVERGREEN.md for the recent-events section.

    Bullets older than `keep_days` (date format YYYY-MM-DD at start) are
    removed and appended to meta/wiki/changelog.md. Returns number rotated.
    """
    if today is None:
        today = date.today()
    evergreen = Path(meta_dir) / "EVERGREEN.md"
    if not evergreen.exists():
        log.debug("rotate_evergreen_events: EVERGREEN.md missing at %s", evergreen)
        return 0
    original_text = evergreen.read_text()
    lines = original_text.splitlines(keepends=False)
    # Detect trailing newline so we can preserve it on rewrite
    had_trailing_newline = original_text.endswith("\n")

    # Find target section header
    section_idx = None
    for i, line in enumerate(lines):
        if _is_target_section(line):
            section_idx = i
            break
    if section_idx is None:
        log.debug("rotate_evergreen_events: section not found")
        return 0

    # Find end of section (next ^## or EOF)
    end_idx = len(lines)
    for j in range(section_idx + 1, len(lines)):
        if _NEXT_SECTION_RE.match(lines[j]):
            end_idx = j
            break

    section_body = lines[section_idx + 1:end_idx]
    kept_body: list[str] = []
    rotated_lines: list[str] = []
    for line in section_body:
        m = _BULLET_DATE_RE.match(line)
        if not m:
            # Non-dated line (blank, sub-bullet, etc.) → keep in place
            kept_body.append(line)
            continue
        try:
            bullet_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            kept_body.append(line)
            continue
        if (today - bullet_date) > timedelta(days=keep_days):
            rotated_lines.append(line)
        else:
            kept_body.append(line)

    if not rotated_lines:
        return 0

    # Cross-file atomicity: write changelog FIRST so a crash between the two
    # writes results in (recoverable) duplicate entries rather than silent
    # data loss. Worst case on retry: same header appended twice → easy
    # dedup; never bullets-vanished.
    changelog = Path(meta_dir) / "wiki" / "changelog.md"
    header = f"## {today.isoformat()} rotated from EVERGREEN"
    block = header + "\n" + "\n".join(rotated_lines) + "\n"
    if changelog.exists():
        existing = changelog.read_text()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_changelog = existing + "\n" + block
    else:
        new_changelog = block
    _atomic_write(changelog, new_changelog)

    # Then rewrite EVERGREEN without the rotated bullets
    new_lines = (
        lines[:section_idx + 1]
        + kept_body
        + lines[end_idx:]
    )
    new_text = "\n".join(new_lines)
    if had_trailing_newline:
        new_text += "\n"
    _atomic_write(evergreen, new_text)

    return len(rotated_lines)


def find_stale_inbox(
    meta_dir: Path,
    threshold_days: int = 30,
    *,
    now: float | None = None,
) -> list[Path]:
    """Return *.md files in meta/raw/inbox/ with mtime older than threshold."""
    if now is None:
        now = time.time()
    inbox = Path(meta_dir) / "raw" / "inbox"
    if not inbox.exists() or not inbox.is_dir():
        return []
    threshold_seconds = threshold_days * 86400
    stale: list[Path] = []
    for entry in inbox.iterdir():
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        if entry.suffix != ".md":
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if (now - mtime) > threshold_seconds:
            stale.append(entry)
    return sorted(stale)


async def decay_loop(meta_dir: Path, interval_s: int = 86400) -> None:
    """Run forever: every interval_s, rotate evergreen + check stale inbox.

    On stale inbox non-empty → push self-notification.
    Cancellable via asyncio.CancelledError.
    """
    while True:
        try:
            try:
                rotate_evergreen_events(meta_dir)
            except Exception as e:
                log.warning("decay_loop: rotate failed: %r", e)
            try:
                stale = find_stale_inbox(meta_dir)
            except Exception as e:
                log.warning("decay_loop: find_stale_inbox failed: %r", e)
                stale = []
            if stale:
                names = [p.name for p in stale[:5]]
                suffix = "..." if len(stale) > 5 else ""
                text = f"{len(stale)} stale inbox files: {names}{suffix}"
                try:
                    await push_to_self(text)
                except Exception as e:
                    log.warning("decay_loop: push_to_self failed: %r", e)
        except Exception as e:
            log.warning("decay_loop: iteration error: %r", e)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
