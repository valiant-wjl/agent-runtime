"""In-memory pending-confirmation store for /agent write operations.

Parallel to ``runtime/approval.py`` but simpler:
  - No state machine (PENDING / APPROVED / EXECUTING etc.) — entries are
    either present (pending) or consumed (gone).
  - No commands list — apply logic lives in agent_admin per action.
  - Same reply tokens ("同意" / "确认" / "取消") for consistency.

Keying: `(chat_id, sender_id)` tuple stringified. DELIBERATELY NOT the
scheduler's `_thread_key` (which is `topic_id or thread_root_id`). The
scheduler key requires the admin's "同意" reply to share the thread root
with their original `/agent` message — feishu only sets root_id on
explicit "回复" gestures, so a plain group message wouldn't match. With
chat_id+sender_id we accept any reply in the same chat from the same
admin, which is the natural single-admin-at-a-time UX. Multi-admin in
one chat each get their own slot (different sender_id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.channels import ParsedMsg


_APPROVE_TOKENS = {"同意", "确认", "Y", "y"}
_CANCEL_TOKENS = {"取消", "N", "n"}


@dataclass
class Pending:
    thread_key: str
    action: str  # "alert_remove" | "alert_register"
    payload: dict[str, Any]
    sender_id: str
    admin_users: list[str] = field(default_factory=list)


@dataclass
class ReplyResult:
    action: str  # "approved" | "cancelled" | "ignored" | "unrelated"
    pending: Pending | None = None


_pending: dict[str, Pending] = {}


def thread_key(parsed: ParsedMsg) -> str:
    """Single source of truth for agent_pending keying. agent_admin uses
    this to stage; scheduler uses it to look up on every incoming
    message. See module docstring for the rationale."""
    return f"{parsed.chat_id}:{parsed.sender_id}"


def stage(
    *,
    thread_key: str,
    action: str,
    payload: dict[str, Any],
    sender_id: str,
    admin_users: list[str],
) -> Pending:
    p = Pending(
        thread_key=thread_key,
        action=action,
        payload=payload,
        sender_id=sender_id,
        admin_users=list(admin_users),
    )
    _pending[thread_key] = p
    return p


def get(thread_key: str) -> Pending | None:
    return _pending.get(thread_key)


def clear_all() -> None:
    _pending.clear()


def handle_reply(thread_key: str, sender_id: str, text: str) -> ReplyResult:
    p = _pending.get(thread_key)
    if p is None:
        return ReplyResult(action="unrelated")
    stripped = text.strip()
    if stripped not in _APPROVE_TOKENS and stripped not in _CANCEL_TOKENS:
        return ReplyResult(action="unrelated", pending=p)
    if sender_id != p.sender_id and sender_id not in p.admin_users:
        return ReplyResult(action="ignored", pending=p)
    if stripped in _APPROVE_TOKENS:
        _pending.pop(thread_key, None)
        return ReplyResult(action="approved", pending=p)
    _pending.pop(thread_key, None)
    return ReplyResult(action="cancelled", pending=p)
