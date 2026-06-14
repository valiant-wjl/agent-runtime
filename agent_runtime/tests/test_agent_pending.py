"""US-pending-001: in-memory pending-confirmation store for /agent writes."""

from __future__ import annotations

from agent_runtime import agent_pending


def setup_function():
    agent_pending.clear_all()


def test_stage_and_get():
    p = agent_pending.stage(
        thread_key="t1",
        action="alert_remove",
        payload={"chat_id": "oc_a"},
        sender_id="u1",
        admin_users=["u1"],
    )
    assert agent_pending.get("t1") is p
    assert p.action == "alert_remove"
    assert p.payload["chat_id"] == "oc_a"


def test_handle_reply_approve():
    agent_pending.stage(
        thread_key="t1", action="alert_remove", payload={"chat_id": "oc_a"},
        sender_id="u1", admin_users=["u1"],
    )
    res = agent_pending.handle_reply("t1", "u1", "同意")
    assert res.action == "approved"
    assert res.pending is not None
    assert agent_pending.get("t1") is None  # consumed


def test_handle_reply_cancel():
    agent_pending.stage(
        thread_key="t1", action="alert_remove", payload={"chat_id": "oc_a"},
        sender_id="u1", admin_users=["u1"],
    )
    res = agent_pending.handle_reply("t1", "u1", "取消")
    assert res.action == "cancelled"
    assert agent_pending.get("t1") is None


def test_handle_reply_non_admin_ignored():
    agent_pending.stage(
        thread_key="t1", action="alert_remove", payload={"chat_id": "oc_a"},
        sender_id="u1", admin_users=["u1"],
    )
    res = agent_pending.handle_reply("t1", "u_other", "同意")
    assert res.action == "ignored"
    assert agent_pending.get("t1") is not None  # NOT consumed


def test_handle_reply_unrelated_text():
    agent_pending.stage(
        thread_key="t1", action="alert_remove", payload={"chat_id": "oc_a"},
        sender_id="u1", admin_users=["u1"],
    )
    res = agent_pending.handle_reply("t1", "u1", "hello")
    assert res.action == "unrelated"
    assert agent_pending.get("t1") is not None


def test_handle_reply_no_pending():
    res = agent_pending.handle_reply("t1", "u1", "同意")
    assert res.action == "unrelated"


def test_stage_overwrites_existing():
    agent_pending.stage(
        thread_key="t1", action="alert_remove", payload={"chat_id": "oc_a"},
        sender_id="u1", admin_users=["u1"],
    )
    p2 = agent_pending.stage(
        thread_key="t1", action="alert_register",
        payload={"chat_id": "oc_b", "project": "p"},
        sender_id="u1", admin_users=["u1"],
    )
    assert agent_pending.get("t1") is p2
    assert agent_pending.get("t1").action == "alert_register"
