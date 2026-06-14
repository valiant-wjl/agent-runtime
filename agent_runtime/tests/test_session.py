import json
from pathlib import Path

import pytest

from agent_runtime import session


def test_put_get_roundtrip(tmp_path):
    p = tmp_path / "sessions.json"
    session.configure(p)
    session.put("thread-1", "sess-abc", agent="billing")
    got = session.get("thread-1")
    assert got is not None
    assert got["session_id"] == "sess-abc"
    assert got["agent"] == "billing"


def test_put_persists_to_disk(tmp_path):
    p = tmp_path / "sessions.json"
    session.configure(p)
    session.put("thread-1", "sess-abc", agent="billing")
    data = json.loads(p.read_text())
    assert data["thread-1"]["session_id"] == "sess-abc"


def test_load_from_disk(tmp_path):
    p = tmp_path / "sessions.json"
    p.write_text(json.dumps({"t1": {"session_id": "s1", "agent": "a1", "created_at": 100.0}}))
    session.configure(p)
    got = session.get("t1")
    assert got is not None
    assert got["session_id"] == "s1"


def test_cleanup_expired(tmp_path):
    import time
    p = tmp_path / "sessions.json"
    session.configure(p)
    session.put("old", "s-old", agent="a", created_at=time.time() - 100000)
    session.put("new", "s-new", agent="a")
    session.cleanup_expired(max_age=86400)
    assert session.get("old") is None
    assert session.get("new") is not None


def test_reset(tmp_path):
    p = tmp_path / "sessions.json"
    session.configure(p)
    session.put("t", "s", agent="a")
    session.reset()
    assert session.get("t") is None


def test_configure_corrupted_file_backs_up(tmp_path):
    """损坏的 sessions.json 应被 rename 备份, _sessions 空白启动."""
    p = tmp_path / "sessions.json"
    p.write_text("{not valid json")
    session.configure(p)
    # 源文件被 rename, _sessions 空
    assert session.get("anything") is None
    # 备份文件应该存在（匹配 .corrupted.*）
    backups = list(tmp_path.glob("sessions.json.corrupted.*"))
    assert len(backups) == 1


def test_put_overwrites_existing_key(tmp_path):
    import time
    p = tmp_path / "sessions.json"
    session.configure(p)
    session.put("t1", "s-old", agent="a", created_at=100.0)
    session.put("t1", "s-new", agent="b")
    got = session.get("t1")
    assert got["session_id"] == "s-new"
    assert got["agent"] == "b"
    assert got["created_at"] > 100.0


def test_cleanup_expired_on_empty_noop(tmp_path):
    p = tmp_path / "sessions.json"
    session.configure(p)
    session.cleanup_expired(max_age=3600)  # 不抛, 不写盘
    # 实际实现 cleanup_expired 在 expired 为空时不调 _save, 所以文件不存在
    assert not p.exists() or p.read_text().strip() in ("", "{}")


def test_put_before_configure_raises(tmp_path):
    """未 configure 就 put 应 fail-fast."""
    session.reset()
    # 手工把 _path 设 None
    import agent_runtime.session as sess_mod
    sess_mod._path = None
    with pytest.raises(RuntimeError, match="configure"):
        session.put("t1", "s1", agent="a")
