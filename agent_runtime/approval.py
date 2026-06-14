r"""Write-operation approval state machine.

State transitions (scheduler contract; module does NOT enforce at runtime):

              +--------+  confirm   +----------+ scheduler +-----------+
              |PENDING | ---------> | APPROVED | --------> | EXECUTING |
              +--------+            +----------+           +-----------+
                  |                                           |      |
               cancel                          success <----/        \---> fail
                  v                               |                       |
              +-----------+                       v                       v
              | CANCELLED |                  +------+                 +--------+
              +-----------+                  | DONE |                 | FAILED |
                  ^                          +------+                 +--------+
                  | cancel                                                |
                  +-------------------------------------------------------+
                                                                       |
              retry (FAILED -> EXECUTING via handle_reply "重试")      |
                                                                       +--+

              PENDING -> TIMEOUT (scheduler timer, via transition())

Legal transitions:
  PENDING   -> APPROVED   (via handle_reply "确认"/"同意")
  PENDING   -> CANCELLED  (via handle_reply "取消")
  PENDING   -> TIMEOUT    (via transition(), scheduler timer)
  APPROVED  -> EXECUTING  (via transition(), scheduler starts write phase)
  EXECUTING -> DONE       (via transition(), scheduler write phase success)
  EXECUTING -> FAILED     (via transition(), scheduler write phase failure)
  FAILED    -> EXECUTING  (via handle_reply "重试")
  FAILED    -> CANCELLED  (via handle_reply "取消")

`transition()` does NOT validate transitions at runtime. Scheduler is the
sole caller; contract violations are implementation bugs that should be
caught in scheduler tests (M2-T13). Persistence layer (M3) may add
validation.

Thread-safety: Module-level `_pending` dict. All public funcs are
synchronous; safe within a single asyncio event loop (no await). NOT
safe across threads or multiple processes. M3 persistence will replace
this in-memory store.

Public API (spec §4.2):
  - parse_approval_block(text) -> ApprovalInfo | None
  - create(...) -> Approval
  - get(thread_key) -> Approval | None
  - check_permission(approval, sender_id, admin_users) -> bool
  - handle_reply(thread_key, sender_id, text, admin_users) -> ActionResult
  - transition(approval, new_state) -> None
  - remove(thread_key) -> Approval | None
  - reset() -> None
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & dataclasses
# ---------------------------------------------------------------------------


class State(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMEOUT = "TIMEOUT"


@dataclass
class ApprovalInfo:
    operation: str = ""
    reason: str = ""
    impact: str = ""
    rollback: str = ""
    # Model-declared target environment ("BOE"/"线上"/...). Empty when the
    # model omitted it; is_production() fails safe to production in that case.
    environment: str = ""


@dataclass
class Approval:
    thread_key: str
    agent_name: str
    info: ApprovalInfo
    sender_id: str
    admin_users: list[str]
    approval_timeout: int
    state: State = State.PENDING
    approval_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)
    card_msg_id: str | None = None


@dataclass
class ActionResult:
    action: str  # "approved" | "cancelled" | "retry" | "ignored" | "unrelated"
    approval: Approval | None = None
    # When action == "ignored", why — lets the scheduler give the user
    # actionable feedback instead of silence:
    #   "needs_admin" — production write, only an admin may approve
    #   "permission"  — sender is neither requester nor admin
    #   "bad_state"   — command not valid for the current state
    reason: str = ""


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

# Approval block tags resolved via rfind (not regex) so parse_approval_block
# can anchor at the LAST closing tag and enforce a "block-at-end-of-text"
# rule: real emissions end with the closing tag; quoted/example blocks in
# persona narrative get rejected. See parse_approval_block for the full rule.
_OPEN_TAG = "[APPROVAL_REQUIRED]"
_CLOSE_TAG = "[/APPROVAL_REQUIRED]"

# Whitespace-stripped chars allowed after [/APPROVAL_REQUIRED] before the
# block is treated as quoted/example. Sized so a brief outro line like
# "请确认。" passes while a multi-paragraph persona description does not.
_TRAILING_OUTRO_LIMIT = 80

# Field header line: 操作/原因/影响/回滚 followed by : or ：
# Captures label in group(1), first-line value in group(2).
#
# The model often decorates field lines with markdown — a bullet ("- 操作:"),
# an ordered-list marker ("1. 操作："), or bold around the name ("**操作**:").
# We tolerate all of these before the label so the value still parses; a
# bare "操作:" remains the canonical form. (Regression: 2026-05-25 incident
# where markdown-decorated fields parsed to all-empty and a content-less
# approval card was sent, confirmed, then executed nothing.)
_FIELD_HEADER_PATTERN = re.compile(
    r"^\s*"                          # leading whitespace
    r"(?:[-*+]\s+|\d+[.)、]\s*)?"    # optional bullet or ordered-list marker
    r"\**\s*"                        # optional bold-open around the name
    r"(操作|原因|影响|回滚|环境)"     # group(1): field label
    r"\s*\**\s*[:：]\**\s*"          # bold/colon (handles **操作**:, **操作:**)
    r"(.*)$",                        # group(2): first-line value
    re.MULTILINE,
)

_FIELD_MAP = {
    "操作": "operation",
    "原因": "reason",
    "影响": "impact",
    "回滚": "rollback",
    "环境": "environment",
}

# Environment classification (spec: only 线上/生产 escalates to admin). A
# production token wins if both kinds appear (fail-safe). Anything not
# clearly non-production — including an empty/unrecognized value — is
# treated as production so an unclassified write never self-clears.
_PROD_ENV_TOKENS = ("线上", "生产", "正式", "online", "prod", "production")
_NONPROD_ENV_TOKENS = (
    "boe", "ppe", "测试", "联调", "预发", "staging", "test", "dev",
)

# Known command tokens for handle_reply
_COMMANDS = frozenset(("确认", "同意", "取消", "重试"))

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_pending: dict[str, Approval] = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_approval_block(text: str) -> ApprovalInfo | None:
    """Parse [APPROVAL_REQUIRED]...[/APPROVAL_REQUIRED] block.

    Supports multi-line field values: continuation lines (lines without a
    field header) are appended to the previous field's value. Fields:
    操作/原因/影响/回滚 map to operation/reason/impact/rollback. Missing
    fields default to "". Supports both ASCII colon ':' and Chinese '：'.

    Block-at-end rule: a real emission ends with the closing tag (with at
    most ``_TRAILING_OUTRO_LIMIT`` chars of outro text after, e.g. "请确认。").
    Quoted/example blocks in the middle of a long narrative — like persona
    files describing the approval mechanism — have substantial trailing
    content and are rejected to None so the verifier path runs instead.
    For unclosed blocks, the last ``[APPROVAL_REQUIRED]`` opens a real
    emission only when no ``[/APPROVAL_REQUIRED]`` appears after it.

    Field lines may be decorated with markdown (bullet "- 操作:",
    ordered-list "1. 操作：", or bold "**操作**:"); the value parses
    regardless. A block whose 操作 ends up blank is not actionable and
    returns None (see empty-operation guard below).

    Returns None if no APPROVAL_REQUIRED block is found, if the block fails
    the at-end rule, or if the parsed 操作 field is empty.
    """
    last_close = text.rfind(_CLOSE_TAG)
    if last_close != -1:
        trailing = text[last_close + len(_CLOSE_TAG):].strip()
        if len(trailing) > _TRAILING_OUTRO_LIMIT:
            return None
        last_open = text.rfind(_OPEN_TAG, 0, last_close)
        if last_open == -1:
            return None
        block_body = text[last_open + len(_OPEN_TAG):last_close]
    else:
        last_open = text.rfind(_OPEN_TAG)
        if last_open == -1:
            return None
        block_body = text[last_open + len(_OPEN_TAG):]

    info = ApprovalInfo()
    current_field: str | None = None

    for line in block_body.splitlines():
        header_match = _FIELD_HEADER_PATTERN.match(line)
        if header_match:
            label = header_match.group(1)
            value = header_match.group(2).strip()
            current_field = _FIELD_MAP[label]
            setattr(info, current_field, value)
        elif current_field is not None and line.strip():
            # Continuation line: append to previous field value
            existing = getattr(info, current_field)
            setattr(info, current_field, (existing + "\n" + line.strip()).strip())

    # Empty-operation guard: a block whose 操作 is blank carries no
    # actionable instruction — the write phase would fork claude with an
    # empty prompt and have nothing to do. Treat it as not-an-approval so
    # the caller falls through to the normal reply/verifier path instead of
    # sending a content-less approval card. (Regression: 2026-05-25.)
    if not info.operation.strip():
        return None

    return info


def create(
    thread_key: str,
    agent_name: str,
    info: ApprovalInfo,
    sender_id: str,
    admin_users: list[str],
    approval_timeout: int,
) -> Approval:
    """Create and register a PENDING approval for `thread_key`.

    If a PENDING approval already exists at `thread_key`, it is overwritten
    with a warning in the log. Scheduler should call `remove(thread_key)`
    before re-creating if explicit handoff is desired.
    """
    existing = _pending.get(thread_key)
    if existing is not None and existing.state == State.PENDING:
        log.warning(
            "approval.create overwriting existing PENDING for thread_key=%s "
            "(prev approval_id=%s); prior user intent will be lost",
            thread_key,
            existing.approval_id,
        )
    approval_obj = Approval(
        thread_key=thread_key,
        agent_name=agent_name,
        info=info,
        sender_id=sender_id,
        admin_users=admin_users,
        approval_timeout=approval_timeout,
    )
    _pending[thread_key] = approval_obj
    return approval_obj


def get(thread_key: str) -> Approval | None:
    """Return the current approval for thread_key, or None."""
    return _pending.get(thread_key)


def check_permission(approval: Approval, sender_id: str, admin_users: list[str]) -> bool:
    """Return True iff sender is the original questioner or in admin_users.

    This is the BOE/non-production rule and the rule for cancel/retry. The
    stricter admin-only gate for *approving* a production write lives in
    can_approve().
    """
    if sender_id == approval.sender_id:
        return True
    if sender_id in admin_users:
        return True
    return False


def is_production(info: ApprovalInfo) -> bool:
    """Classify the declared environment as production-risk.

    True (production → admin approval required) unless the model declared an
    explicitly non-production environment. Fail-safe: empty or unrecognized
    environment is treated as production, and a production token wins when
    both kinds appear — an unclassified write must escalate to an admin
    rather than silently clear on the requester's own '确认'.
    """
    env = (info.environment or "").strip().lower()
    if not env:
        return True
    if any(tok in env for tok in _PROD_ENV_TOKENS):
        return True
    if any(tok in env for tok in _NONPROD_ENV_TOKENS):
        return False
    return True


def can_approve(approval: Approval, sender_id: str, admin_users: list[str]) -> bool:
    """Who may APPROVE (确认/同意) this write.

    - Production (is_production True): admin only.
    - Non-production (BOE/test): the original requester or an admin.
    """
    if is_production(approval.info):
        return sender_id in admin_users
    return check_permission(approval, sender_id, admin_users)


def handle_reply(
    thread_key: str,
    sender_id: str,
    text: str,
    admin_users: list[str],
) -> ActionResult:
    """Process a reply in a thread that may be an approval response.

    `admin_users` parameter should match `approval.admin_users` snapshot;
    scheduler is responsible for passing the correct list. M3 persistence
    will consolidate the two sources.

    Returns ActionResult with action one of:
    - "unrelated": no pending approval for this thread, OR text is not a
      known command ("确认"/"同意"/"取消"/"重试")
    - "ignored": pending approval exists but sender lacks permission, OR
      state not in transition-valid set
    - "approved": text == "确认"/"同意" AND state=PENDING → APPROVED
    - "cancelled": text == "取消" AND state ∈ (PENDING, FAILED) → CANCELLED
    - "retry": text == "重试" AND state == FAILED → EXECUTING
    """
    stripped = text.strip()
    approval_obj = _pending.get(thread_key)

    # No pending approval → unrelated; log if it looks like a stale command
    if approval_obj is None:
        if stripped in _COMMANDS:
            log.info(
                "approval.handle_reply command %r received for thread %s "
                "but no pending approval (stale / race / user error)",
                stripped,
                thread_key,
            )
        return ActionResult(action="unrelated")

    # Unknown command → unrelated (even if pending exists)
    if stripped not in _COMMANDS:
        return ActionResult(action="unrelated")

    # Approve: env-aware permission. Production writes require an admin; a
    # non-admin requester's "确认" is reported as needs_admin so the
    # scheduler can tell them who must approve (instead of silence).
    if stripped in ("确认", "同意"):
        if approval_obj.state != State.PENDING:
            return ActionResult("ignored", approval_obj, reason="bad_state")
        if not can_approve(approval_obj, sender_id, admin_users):
            reason = "needs_admin" if is_production(approval_obj.info) else "permission"
            return ActionResult("ignored", approval_obj, reason=reason)
        approval_obj.state = State.APPROVED
        return ActionResult(action="approved", approval=approval_obj)

    # Cancel / retry: not admin-gated — the requester (or an admin) may
    # always cancel their own request or retry a failed write.
    if not check_permission(approval_obj, sender_id, admin_users):
        return ActionResult("ignored", approval_obj, reason="permission")

    if stripped == "取消":
        if approval_obj.state in (State.PENDING, State.FAILED):
            approval_obj.state = State.CANCELLED
            return ActionResult(action="cancelled", approval=approval_obj)
        return ActionResult("ignored", approval_obj, reason="bad_state")

    elif stripped == "重试":
        if approval_obj.state == State.FAILED:
            approval_obj.state = State.EXECUTING
            return ActionResult(action="retry", approval=approval_obj)
        return ActionResult("ignored", approval_obj, reason="bad_state")

    # All known commands are handled above; reaching here is a bug.
    raise AssertionError(
        f"unreachable: stripped={stripped!r} approval_obj.state={approval_obj.state}"
    )


def transition(approval: Approval, new_state: State) -> None:
    """Force-set approval.state to new_state.

    Used by scheduler to advance:
      APPROVED  -> EXECUTING  (scheduler starts write phase)
      EXECUTING -> DONE       (write phase succeeded)
      EXECUTING -> FAILED     (write phase failed)
      PENDING   -> TIMEOUT    (scheduler timer expiry)

    No runtime validation of the transition; see module docstring for the
    full legal transition matrix. Contract violations are implementation
    bugs and should be caught in scheduler tests (M2-T13).
    """
    approval.state = new_state


def remove(thread_key: str) -> Approval | None:
    """Remove and return the approval at thread_key, or None if absent."""
    return _pending.pop(thread_key, None)


def reset() -> None:
    """Clear all pending approvals (for tests; conftest autouse calls this)."""
    _pending.clear()
