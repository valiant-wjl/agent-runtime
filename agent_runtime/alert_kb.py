"""Alert knowledge base — append-only jsonl per chat_id.

Single-process asyncio runtime; all calls are sync. File IO is fast for
the per-chat-id sized files we expect (hundreds of entries within the
14-day TTL window). The lock is kept for the rewrite path so an external
sweeper (cron) added later doesn't race with the in-process scheduler.

Layout:
    <root>/<chat_id>.jsonl      one entry per line, append-only
    <root>/<chat_id>.lock       fcntl LOCK_EX held during rewrite

Backwards-compatible read: corrupt JSON lines are warned + skipped, not
fatal — manual edits or partial-write crashes don't take down the kb.
"""

from __future__ import annotations

import fcntl
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# id format: alert-YYYY-MM-DD-NNN  (NNN ≥ 3 digits, allows growth past 999)
_ID_RE = re.compile(r"^alert-(\d{4}-\d{2}-\d{2})-(\d{3,})$")


@dataclass
class AlertEntry:
    id: str
    created_at: str
    alert_text: str
    conclusion: str
    source_message_id: str
    status: str
    hit_count: int
    last_hit_at: str | None


class AlertKB:
    def __init__(self, root: Path):
        self.root = Path(root)

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _path(self, chat_id: str) -> Path:
        return self.root / f"{chat_id}.jsonl"

    def _ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _read_all_raw(self, chat_id: str) -> list[dict]:
        p = self._path(chat_id)
        if not p.is_file():
            return []
        rows: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as e:
                log.warning(
                    "alert_kb: skipping corrupt JSON line in %s: %s", p, e
                )
        return rows

    def _next_id(self, chat_id: str, today: str) -> str:
        rows = self._read_all_raw(chat_id)
        max_seq = 0
        for r in rows:
            m = _ID_RE.match(r.get("id", ""))
            if m and m.group(1) == today:
                seq = int(m.group(2))
                if seq > max_seq:
                    max_seq = seq
        return f"alert-{today}-{max_seq + 1:03d}"

    def _rewrite(self, chat_id: str, rows: list[dict]) -> None:
        """Atomic rewrite of <chat_id>.jsonl. Holds LOCK_EX on <chat_id>.lock
        for the duration. Cron-based external sweepers are expected to use
        the same lock convention."""
        self._ensure_root()
        target = self._path(chat_id)
        lock = self.root / f"{chat_id}.lock"
        with lock.open("a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                tmp = target.with_suffix(target.suffix + ".tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    for r in rows:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                tmp.replace(target)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def add(
        self,
        *,
        chat_id: str,
        alert_text: str,
        conclusion: str,
        source_message_id: str,
    ) -> AlertEntry:
        self._ensure_root()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        entry = AlertEntry(
            id=self._next_id(chat_id, today),
            created_at=now.isoformat(),
            alert_text=alert_text,
            conclusion=conclusion,
            source_message_id=source_message_id,
            status="active",
            hit_count=0,
            last_hit_at=None,
        )
        line = json.dumps(asdict(entry), ensure_ascii=False)
        with self._path(chat_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return entry

    def list_active(self, *, chat_id: str, ttl_seconds: int) -> list[AlertEntry]:
        rows = self._read_all_raw(chat_id)
        now = datetime.now(timezone.utc)
        out: list[AlertEntry] = []
        for r in rows:
            if r.get("status") != "active":
                continue
            created = _parse_iso(r.get("created_at"))
            if created is None:
                log.warning("alert_kb: skipping entry with bad created_at: %r", r)
                continue
            # int truncation absorbs sub-second drift between the moment a
            # caller computed `now - ttl` and the moment list_active calls
            # datetime.now() — boundary ±1s should still count as "in window".
            age = int((now - created).total_seconds())
            if age > ttl_seconds:
                continue
            try:
                out.append(AlertEntry(**r))
            except TypeError as e:
                log.warning("alert_kb: schema mismatch in %r: %s", r, e)
        return out

    def mark_hit(self, *, chat_id: str, entry_id: str) -> None:
        rows = self._read_all_raw(chat_id)
        found = False
        now_iso = datetime.now(timezone.utc).isoformat()
        for r in rows:
            if r.get("id") == entry_id:
                r["hit_count"] = int(r.get("hit_count", 0)) + 1
                r["last_hit_at"] = now_iso
                found = True
        if not found:
            raise KeyError(entry_id)
        self._rewrite(chat_id, rows)

    def sweep(self, *, ttl_seconds: int) -> int:
        if not self.root.is_dir():
            return 0
        purged = 0
        now = datetime.now(timezone.utc)
        for jsonl_path in sorted(self.root.glob("*.jsonl")):
            chat_id = jsonl_path.stem
            rows = self._read_all_raw(chat_id)
            kept: list[dict] = []
            for r in rows:
                created = _parse_iso(r.get("created_at"))
                if created is None:
                    # Conservative: keep malformed entries so a bad row
                    # doesn't trigger silent data loss; list_active still
                    # filters them out from retrieval.
                    kept.append(r)
                    continue
                age = int((now - created).total_seconds())
                if age > ttl_seconds:
                    purged += 1
                    continue
                kept.append(r)
            if len(kept) != len(rows):
                self._rewrite(chat_id, kept)
        return purged


def _parse_iso(value: object) -> datetime | None:
    """Return an aware UTC datetime, or None for unparseable input.

    A hand-edited row may carry a naive timestamp (no tz suffix); arithmetic
    against `now()` (always aware) would raise TypeError and take down the
    whole sweep. Forcing UTC for naive values keeps a single bad row from
    poisoning the loop — the entry just behaves as if it were UTC-stamped.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
