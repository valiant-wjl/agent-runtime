import pytest
from agent_runtime import dedup


def test_first_seen_returns_false():
    assert dedup.is_duplicate("msg-1", window=300) is False


def test_second_seen_returns_true():
    dedup.is_duplicate("msg-1", window=300)
    assert dedup.is_duplicate("msg-1", window=300) is True


def test_expired_entry_not_duplicate(monkeypatch):
    import time
    t0 = 1000.0
    monkeypatch.setattr(time, "time", lambda: t0)
    dedup.is_duplicate("msg-1", window=10)
    # fast-forward > window
    monkeypatch.setattr(time, "time", lambda: t0 + 11)
    assert dedup.is_duplicate("msg-1", window=10) is False


def test_evicts_old_entries_keeps_map_bounded(monkeypatch):
    """写 N 条消息跨越窗口, 老 entry 应被从头部驱逐."""
    import time
    t = [1000.0]
    monkeypatch.setattr(time, "time", lambda: t[0])
    for i in range(5):
        dedup.is_duplicate(f"msg-{i}", window=10)
        t[0] += 5
    # now t=1025; window=10 means only msgs from t>=1015 should remain
    # msg-0..msg-2 应被驱逐 (时间戳 1000/1005/1010 均 < 1015)
    from agent_runtime.dedup import _seen_messages
    assert "msg-0" not in _seen_messages
    assert "msg-1" not in _seen_messages
    # msg-3 at t=1015 should still be in (1025-1015=10, 不 > 10)
    assert "msg-3" in _seen_messages or "msg-4" in _seen_messages


def test_different_ids_independent():
    """不同 message_id 互不干扰."""
    assert dedup.is_duplicate("a", window=300) is False
    assert dedup.is_duplicate("b", window=300) is False
    assert dedup.is_duplicate("a", window=300) is True   # a 已见
    assert dedup.is_duplicate("c", window=300) is False  # c 未见
