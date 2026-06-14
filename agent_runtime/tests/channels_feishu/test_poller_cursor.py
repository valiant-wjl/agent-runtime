"""US-poll-002: PollerCursor — persist {chat_id: last_create_time_ms}.

Cursor survives scheduler restarts so a polling loop never re-processes
the same alert twice. Atomic write (tmp + rename) so a crash mid-write
can't leave a partial JSON on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.channels.feishu.poller import PollerCursor


# ---------------------------------------------------------------------------
# Empty / missing-file behaviour
# ---------------------------------------------------------------------------


def test_get_missing_chat_returns_none(tmp_path: Path):
    c = PollerCursor(path=tmp_path / "cursor.json")
    assert c.get("oc_never_seen") is None


def test_get_when_file_missing_returns_none(tmp_path: Path):
    c = PollerCursor(path=tmp_path / "does-not-exist.json")
    assert c.get("oc_x") is None


# ---------------------------------------------------------------------------
# Set + persist + read back
# ---------------------------------------------------------------------------


def test_set_persists_to_disk(tmp_path: Path):
    f = tmp_path / "cursor.json"
    c = PollerCursor(path=f)
    c.set("oc_a", 1778294173384)
    raw = json.loads(f.read_text())
    assert raw == {"oc_a": 1778294173384}


def test_set_isolates_per_chat(tmp_path: Path):
    f = tmp_path / "cursor.json"
    c = PollerCursor(path=f)
    c.set("oc_a", 100)
    c.set("oc_b", 200)
    assert c.get("oc_a") == 100
    assert c.get("oc_b") == 200


def test_set_overwrites_previous_value(tmp_path: Path):
    c = PollerCursor(path=tmp_path / "cursor.json")
    c.set("oc_a", 100)
    c.set("oc_a", 200)
    assert c.get("oc_a") == 200


# ---------------------------------------------------------------------------
# Restart / fresh-instance read
# ---------------------------------------------------------------------------


def test_fresh_instance_reads_existing_file(tmp_path: Path):
    """Restart simulation: process A writes, process B starts from disk."""
    f = tmp_path / "cursor.json"
    a = PollerCursor(path=f)
    a.set("oc_a", 12345)
    b = PollerCursor(path=f)
    assert b.get("oc_a") == 12345


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


def test_corrupt_cursor_file_falls_back_to_empty(tmp_path: Path, caplog):
    """Hand-edited / partially-written cursor must not poison startup."""
    f = tmp_path / "cursor.json"
    f.write_text("not valid JSON {")
    c = PollerCursor(path=f)
    assert c.get("oc_a") is None
    # ...but must still allow recovery via overwrite
    c.set("oc_a", 999)
    assert c.get("oc_a") == 999
    assert json.loads(f.read_text()) == {"oc_a": 999}


def test_set_creates_parent_dir_if_missing(tmp_path: Path):
    f = tmp_path / "deep" / "subdir" / "cursor.json"
    c = PollerCursor(path=f)
    c.set("oc_a", 1)
    assert f.is_file()


def test_set_uses_atomic_rename(tmp_path: Path):
    """After set() returns, a sibling .tmp file must NOT linger.

    Atomicity guard: a partial-write crash should leave either the
    previous good file or the tmp visible briefly, but successful
    completion means cleanup."""
    f = tmp_path / "cursor.json"
    c = PollerCursor(path=f)
    c.set("oc_a", 1)
    leftovers = list(tmp_path.glob("cursor.json.tmp*"))
    assert leftovers == [], f"tmp files leaked: {leftovers}"


def test_get_all_returns_full_snapshot(tmp_path: Path):
    """A snapshot view useful for the polling loop's batch iteration."""
    c = PollerCursor(path=tmp_path / "cursor.json")
    c.set("oc_a", 1)
    c.set("oc_b", 2)
    snap = c.get_all()
    assert snap == {"oc_a": 1, "oc_b": 2}
    # Mutating the returned snapshot must not affect the cursor (defensive copy).
    snap["oc_a"] = 999
    assert c.get("oc_a") == 1
