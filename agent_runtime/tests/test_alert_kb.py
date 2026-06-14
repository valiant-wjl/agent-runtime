"""Tests for runtime/alert_kb.py — alert knowledge base (jsonl per chat_id).

Covers (per US-001 acceptance criteria):
  - AlertEntry shape
  - add() id sequence per chat_id per day
  - list_active() TTL boundary inclusion + status filter
  - mark_hit() increments and persists
  - sweep() physically removes expired and returns purged count
  - corrupt-line tolerance (warn + skip)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_runtime.alert_kb import AlertEntry, AlertKB


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_alert_entry_fields_exactly():
    """Schema lock: any silent rename here is a breaking change for kb files
    on disk. Update spec + migration plan if you really need to change it."""
    e = AlertEntry(
        id="alert-2026-05-07-001",
        created_at="2026-05-07T10:00:00+00:00",
        alert_text="boom",
        conclusion="restart",
        source_message_id="om_x",
        status="active",
        hit_count=0,
        last_hit_at=None,
    )
    assert e.id == "alert-2026-05-07-001"
    # All fields are settable / readable
    assert e.status == "active"
    assert e.hit_count == 0
    assert e.last_hit_at is None


def test_add_creates_jsonl_file_and_assigns_sequential_id(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    e1 = kb.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m1")
    e2 = kb.add(chat_id="oc_a", alert_text="x2", conclusion="y2", source_message_id="m2")
    f = tmp_path / "oc_a.jsonl"
    assert f.is_file()
    today = datetime.now().strftime("%Y-%m-%d")
    assert e1.id == f"alert-{today}-001"
    assert e2.id == f"alert-{today}-002"
    rows = _read_jsonl(f)
    assert [r["id"] for r in rows] == [e1.id, e2.id]


def test_add_creates_parent_dir_if_missing(tmp_path: Path):
    root = tmp_path / "deep" / "alerts"
    kb = AlertKB(root=root)
    kb.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m1")
    assert (root / "oc_a.jsonl").is_file()


def test_add_id_sequence_isolated_per_chat_id(tmp_path: Path):
    """Two chat_ids on the same day each start from 001."""
    kb = AlertKB(root=tmp_path)
    a = kb.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m1")
    b = kb.add(chat_id="oc_b", alert_text="x", conclusion="y", source_message_id="m1")
    today = datetime.now().strftime("%Y-%m-%d")
    assert a.id == f"alert-{today}-001"
    assert b.id == f"alert-{today}-001"


def test_add_id_sequence_continues_across_process_restart(tmp_path: Path):
    """A fresh AlertKB instance must read existing entries to avoid id reuse."""
    kb1 = AlertKB(root=tmp_path)
    e1 = kb1.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m1")
    # Simulate restart: new instance, same root.
    kb2 = AlertKB(root=tmp_path)
    e2 = kb2.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m2")
    assert e1.id != e2.id
    today = datetime.now().strftime("%Y-%m-%d")
    assert e2.id == f"alert-{today}-002"


def test_list_active_returns_only_active_within_ttl(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    # Manually inject entries with crafted created_at values.
    f = tmp_path / "oc_a.jsonl"
    now = datetime.now(timezone.utc)
    fresh = {
        "id": "alert-fresh-001", "created_at": now.isoformat(),
        "alert_text": "fresh", "conclusion": "c", "source_message_id": "m",
        "status": "active", "hit_count": 0, "last_hit_at": None,
    }
    expired = {
        "id": "alert-old-001", "created_at": (now - timedelta(days=20)).isoformat(),
        "alert_text": "old", "conclusion": "c", "source_message_id": "m",
        "status": "active", "hit_count": 0, "last_hit_at": None,
    }
    rejected = {
        "id": "alert-rej-001", "created_at": now.isoformat(),
        "alert_text": "rej", "conclusion": "c", "source_message_id": "m",
        "status": "rejected", "hit_count": 0, "last_hit_at": None,
    }
    f.write_text("\n".join(json.dumps(r) for r in (fresh, expired, rejected)) + "\n")

    out = kb.list_active(chat_id="oc_a", ttl_seconds=14 * 86400)
    ids = [e.id for e in out]
    assert ids == ["alert-fresh-001"]


def test_list_active_ttl_boundary_inclusive(tmp_path: Path):
    """An entry exactly at age == ttl_seconds should still be included."""
    kb = AlertKB(root=tmp_path)
    ttl = 14 * 86400
    f = tmp_path / "oc_a.jsonl"
    now = datetime.now(timezone.utc)
    on_boundary = {
        "id": "alert-boundary-001",
        "created_at": (now - timedelta(seconds=ttl)).isoformat(),
        "alert_text": "x", "conclusion": "y", "source_message_id": "m",
        "status": "active", "hit_count": 0, "last_hit_at": None,
    }
    f.write_text(json.dumps(on_boundary) + "\n")
    out = kb.list_active(chat_id="oc_a", ttl_seconds=ttl)
    assert len(out) == 1


def test_list_active_missing_file_returns_empty(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    assert kb.list_active(chat_id="oc_never_seen", ttl_seconds=86400) == []


def test_list_active_corrupt_line_skipped(tmp_path: Path, caplog):
    kb = AlertKB(root=tmp_path)
    f = tmp_path / "oc_a.jsonl"
    now = datetime.now(timezone.utc)
    good = json.dumps({
        "id": "alert-good-001", "created_at": now.isoformat(),
        "alert_text": "x", "conclusion": "y", "source_message_id": "m",
        "status": "active", "hit_count": 0, "last_hit_at": None,
    })
    f.write_text(good + "\nNOT_VALID_JSON\n" + good + "\n")
    import logging
    with caplog.at_level(logging.WARNING):
        out = kb.list_active(chat_id="oc_a", ttl_seconds=86400)
    assert len(out) == 2
    assert any("corrupt" in rec.message.lower() or "skip" in rec.message.lower() or "json" in rec.message.lower()
               for rec in caplog.records)


def test_mark_hit_increments_and_persists(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    e = kb.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m")
    kb.mark_hit(chat_id="oc_a", entry_id=e.id)
    kb.mark_hit(chat_id="oc_a", entry_id=e.id)

    rows = _read_jsonl(tmp_path / "oc_a.jsonl")
    assert len(rows) == 1
    assert rows[0]["hit_count"] == 2
    assert rows[0]["last_hit_at"] is not None


def test_mark_hit_unknown_id_raises(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    kb.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m")
    with pytest.raises(KeyError):
        kb.mark_hit(chat_id="oc_a", entry_id="alert-bogus-999")


def test_sweep_removes_expired_and_returns_count(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    f = tmp_path / "oc_a.jsonl"
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": "alert-fresh-001", "created_at": now.isoformat(),
            "alert_text": "fresh", "conclusion": "c", "source_message_id": "m",
            "status": "active", "hit_count": 0, "last_hit_at": None,
        },
        {
            "id": "alert-old-001",
            "created_at": (now - timedelta(days=20)).isoformat(),
            "alert_text": "old", "conclusion": "c", "source_message_id": "m",
            "status": "active", "hit_count": 0, "last_hit_at": None,
        },
        {
            "id": "alert-old-002",
            "created_at": (now - timedelta(days=30)).isoformat(),
            "alert_text": "older", "conclusion": "c", "source_message_id": "m",
            "status": "active", "hit_count": 0, "last_hit_at": None,
        },
    ]
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    purged = kb.sweep(ttl_seconds=14 * 86400)
    assert purged == 2
    after = _read_jsonl(f)
    assert [r["id"] for r in after] == ["alert-fresh-001"]


def test_sweep_handles_multiple_chat_files(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    now = datetime.now(timezone.utc)
    for chat in ("oc_a", "oc_b"):
        rows = [
            {
                "id": "alert-fresh-001",
                "created_at": now.isoformat(),
                "alert_text": "x", "conclusion": "y", "source_message_id": "m",
                "status": "active", "hit_count": 0, "last_hit_at": None,
            },
            {
                "id": "alert-old-001",
                "created_at": (now - timedelta(days=30)).isoformat(),
                "alert_text": "x", "conclusion": "y", "source_message_id": "m",
                "status": "active", "hit_count": 0, "last_hit_at": None,
            },
        ]
        (tmp_path / f"{chat}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    purged = kb.sweep(ttl_seconds=14 * 86400)
    assert purged == 2
    for chat in ("oc_a", "oc_b"):
        after = _read_jsonl(tmp_path / f"{chat}.jsonl")
        assert [r["id"] for r in after] == ["alert-fresh-001"]


def test_sweep_skips_non_jsonl_files(tmp_path: Path):
    """Lockfile or stray files in the kb root should not break sweep."""
    kb = AlertKB(root=tmp_path)
    (tmp_path / "oc_a.lock").write_text("")
    (tmp_path / "stray.txt").write_text("hello")
    # No real jsonl yet — sweep should be a no-op.
    assert kb.sweep(ttl_seconds=14 * 86400) == 0


def test_sweep_empty_root_returns_zero(tmp_path: Path):
    kb = AlertKB(root=tmp_path)
    assert kb.sweep(ttl_seconds=86400) == 0


def test_naive_datetime_in_row_does_not_crash_sweep(tmp_path: Path):
    """A hand-edited row with a tz-naive ISO string must not poison sweep
    (architect review: aware-vs-naive arithmetic raises TypeError)."""
    kb = AlertKB(root=tmp_path)
    f = tmp_path / "oc_a.jsonl"
    naive = {
        "id": "alert-naive-001",
        "created_at": datetime.now().isoformat(),  # NO tzinfo
        "alert_text": "x", "conclusion": "y", "source_message_id": "m",
        "status": "active", "hit_count": 0, "last_hit_at": None,
    }
    f.write_text(json.dumps(naive) + "\n")
    # Should not raise.
    purged = kb.sweep(ttl_seconds=14 * 86400)
    assert purged == 0
    # And list_active should treat it as UTC, returning the entry.
    out = kb.list_active(chat_id="oc_a", ttl_seconds=14 * 86400)
    assert len(out) == 1
