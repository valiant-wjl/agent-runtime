"""Tests for approval state machine — migrated from feishu-agent-gateway/tests/test_approval.py.

Migration notes:
- TestBuildApprovalCard (3 tests): _build_approval_card is gateway internal UI concern,
  not part of runtime/approval.py public API. Migrated to M2-T13 scheduler tests.
- TestHandleMessageApproval (3 tests): depends on handle_message/run_claude/send_reply
  gateway functions. Migrated to M2-T13 scheduler tests.
- TestWritePhase (2 tests): depends on _execute_write/_run_claude_write gateway internals.
  Migrated to M2-T13 scheduler tests.
- TestHandleMessageApprovalReplyRouting (1 test): depends on handle_message gateway function.
  Migrated to M2-T13 scheduler tests.
- TestApprovalTimeout (2 tests): timeout scheduling is gateway/scheduler concern, not
  runtime/approval.py. Timeout state transition logic tested via transition() API instead.
- parse_approval_block: new API returns ApprovalInfo dataclass (not dict with fields/analysis).
  Assertions adapted accordingly.
"""

import pytest

from agent_runtime import approval
from agent_runtime.approval import (
    State,
    ApprovalInfo,
    Approval,
)


# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

CLAUDE_OUTPUT_WITH_APPROVAL = """\
经过分析，TCC 配置需要修改。

[APPROVAL_REQUIRED]
操作: 修改 TCC 配置项 foo=bar
原因: 用户请求变更
影响: 影响 billing 服务
回滚: 恢复 foo=old_value
[/APPROVAL_REQUIRED]"""

CLAUDE_OUTPUT_NO_APPROVAL = "一切正常，无需操作。"

CLAUDE_OUTPUT_UNCLOSED = """\
分析完毕。

[APPROVAL_REQUIRED]
操作: 重启服务
原因: 内存泄漏"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_approval(
    thread_key: str = "thread_001",
    sender_id: str = "ou_user_001",
    admin_users: list[str] | None = None,
    state: State = State.PENDING,
    environment: str = "BOE",
) -> Approval:
    """Create and store an approval via the public API.

    Defaults ``environment="BOE"`` so the generic state-machine tests keep
    exercising the requester-can-approve path; the production tier (admin
    required) is covered explicitly in TestEnvironmentTieredApproval.
    """
    if admin_users is None:
        admin_users = ["ou_admin_001"]
    info = ApprovalInfo(
        operation="test op", reason="test reason", environment=environment,
    )
    appr = approval.create(
        thread_key=thread_key,
        agent_name="test_agent",
        info=info,
        sender_id=sender_id,
        admin_users=admin_users,
        approval_timeout=300,
    )
    appr.state = state
    return appr


# ---------------------------------------------------------------------------
# TestParseApprovalBlock — migrated from legacy TestParseApprovalBlock
# ---------------------------------------------------------------------------


class TestParseApprovalBlock:
    def test_standard(self):
        """Standard block with all 4 fields should parse correctly."""
        result = approval.parse_approval_block(CLAUDE_OUTPUT_WITH_APPROVAL)
        assert result is not None
        assert isinstance(result, ApprovalInfo)
        assert "foo=bar" in result.operation
        assert result.reason != ""
        assert result.impact != ""
        assert result.rollback != ""

    def test_no_marker(self):
        """Text without APPROVAL_REQUIRED block returns None."""
        result = approval.parse_approval_block(CLAUDE_OUTPUT_NO_APPROVAL)
        assert result is None

    def test_partial_fields(self):
        """Block with only some fields — missing fields default to empty string."""
        text = "[APPROVAL_REQUIRED]\n操作: 做某事\n[/APPROVAL_REQUIRED]"
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "做某事"
        # Missing fields default to ""
        assert result.reason == ""
        assert result.impact == ""
        assert result.rollback == ""

    def test_with_analysis(self):
        """Text before the block is ignored; operation field is parsed."""
        result = approval.parse_approval_block(CLAUDE_OUTPUT_WITH_APPROVAL)
        assert result is not None
        # The analysis text is before the block (not part of ApprovalInfo)
        # but the block must be parsed correctly
        assert "TCC 配置项" in result.operation

    def test_unclosed(self):
        """Unclosed block (missing closing tag) should still be parsed."""
        result = approval.parse_approval_block(CLAUDE_OUTPUT_UNCLOSED)
        assert result is not None
        assert result.operation == "重启服务"

    # ------------------------------------------------------------------
    # Markdown-formatted field lines + empty-operation guard (regression
    # for 2026-05-25 incident: claude emitted the approval block with
    # markdown-decorated field lines ("- 操作:", "**操作**:"), the strict
    # ^\s*操作 anchor matched nothing, every field came back "", the card
    # rendered all-empty, the user confirmed, and the write phase forked
    # with an empty prompt — claude correctly refused, so the TCC publish
    # never ran. Fix: tolerate markdown markers, and reject blocks with no
    # actionable operation as not-an-approval.
    # ------------------------------------------------------------------

    def test_markdown_bullet_fields(self):
        """Field lines prefixed with a markdown bullet ('- 操作:') parse."""
        text = (
            "分析完毕。\n[APPROVAL_REQUIRED]\n"
            "- 操作: 发布 lark-tenant-a3 TCC\n"
            "- 原因: 联调需要\n"
            "- 影响: 线上配置\n"
            "- 回滚: 回退到上一版本\n"
            "[/APPROVAL_REQUIRED]\n请确认。"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "发布 lark-tenant-a3 TCC"
        assert result.reason == "联调需要"
        assert result.impact == "线上配置"
        assert result.rollback == "回退到上一版本"

    def test_markdown_bold_fields(self):
        """Field names wrapped in bold ('**操作**:') parse."""
        text = (
            "[APPROVAL_REQUIRED]\n"
            "**操作**: 发布 TCC\n"
            "**原因**: 联调\n"
            "[/APPROVAL_REQUIRED]\n请确认。"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "发布 TCC"
        assert result.reason == "联调"

    def test_ordered_list_fields(self):
        """Field lines as an ordered list ('1. 操作：') parse."""
        text = (
            "[APPROVAL_REQUIRED]\n"
            "1. 操作：改 TCC 配置\n"
            "2. 原因：需求变更\n"
            "[/APPROVAL_REQUIRED]\n请确认。"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "改 TCC 配置"

    def test_empty_operation_rejected(self):
        """Block with blank operation is not actionable → None (no card)."""
        text = (
            "[APPROVAL_REQUIRED]\n操作:\n原因:\n影响:\n回滚:\n"
            "[/APPROVAL_REQUIRED]\n请确认。"
        )
        assert approval.parse_approval_block(text) is None

    def test_tags_only_no_fields_rejected(self):
        """Bare tags with the action left in prose → None (no card)."""
        text = (
            "我要发布 TCC，内容写在正文里。\n"
            "[APPROVAL_REQUIRED]\n[/APPROVAL_REQUIRED]\n请确认。"
        )
        assert approval.parse_approval_block(text) is None

    def test_environment_field_parsed(self):
        """The optional 环境 field is captured into ApprovalInfo.environment."""
        text = (
            "[APPROVAL_REQUIRED]\n"
            "操作: 发布 TCC\n"
            "环境: 线上\n"
            "原因: 上线\n"
            "[/APPROVAL_REQUIRED]\n请确认。"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.environment == "线上"

    def test_environment_field_markdown_bullet(self):
        """环境 field also parses with markdown decoration."""
        text = (
            "[APPROVAL_REQUIRED]\n"
            "- 操作: 改配置\n- 环境: BOE\n"
            "[/APPROVAL_REQUIRED]\n请确认。"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.environment == "BOE"

    def test_environment_defaults_empty_when_absent(self):
        """Missing 环境 field leaves environment == '' (caller fail-safes)."""
        text = "[APPROVAL_REQUIRED]\n操作: 改配置\n[/APPROVAL_REQUIRED]\n请确认。"
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.environment == ""

    # ------------------------------------------------------------------
    # Block-at-end strictness (regression for 2026-05-05 leak — claude
    # quoted SOUL.md/CLAUDE.md verbatim, including the literal token,
    # and the parser greedily matched a non-emission block in the middle
    # of a self-introduction draft, short-circuiting the verifier path).
    # ------------------------------------------------------------------

    def test_persona_snippet_leak_rejected(self):
        """Quoted persona block (mid-text, long narrative after) must not parse.

        Mirrors the exact shape of the SOUL.md/CLAUDE.md leak: a code-fenced
        example of the approval block followed by hundreds of chars of
        unrelated description / lessons text. The parser MUST reject this
        and let the verifier path run instead.
        """
        leaked = (
            "我是 lbp-growth-agent，spring_billing 业务的数字员工。\n\n"
            "## 写操作审批\n"
            "涉及外部代码/配置/DB/发布，**不执行**，输出 [APPROVAL_REQUIRED] 块：\n\n"
            "    [APPROVAL_REQUIRED]\n"
            "    操作: 修改 TCC limit\n"
            "    原因: 业务需要\n"
            "    影响: billing 全量\n"
            "    回滚: 恢复 100\n"
            "    [/APPROVAL_REQUIRED]\n"
            "Gateway 转审批卡片，确认后写阶段 fork。\n\n"
            "## 我的能力\n"
            "1. 答业务问题（拉 wiki + verifier 复核）\n"
            "2. 写操作走审批闭环\n"
            "3. /lesson <内容> 录入纠错\n"
        )
        assert approval.parse_approval_block(leaked) is None

    def test_block_at_end_with_short_outro_still_parses(self):
        """Real emission may end with a brief outro (≤80 chars) — still valid."""
        text = (
            "分析完成，写操作如下：\n"
            "[APPROVAL_REQUIRED]\n"
            "操作: 改 X\n"
            "原因: r\n"
            "影响: i\n"
            "回滚: b\n"
            "[/APPROVAL_REQUIRED]\n"
            "请确认。"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "改 X"

    def test_block_at_end_with_long_trailing_rejected(self):
        """Block followed by >80 chars of additional text → quoted/example."""
        text = (
            "[APPROVAL_REQUIRED]\n"
            "操作: 改 X\n"
            "[/APPROVAL_REQUIRED]\n"
            + ("Gateway 转审批卡片，确认后写阶段 fork。" * 5)
        )
        assert approval.parse_approval_block(text) is None

    def test_two_blocks_first_quoted_second_real(self):
        """Persona quotes a block early, real emission appears at end → parses."""
        text = (
            "举例如：\n"
            "[APPROVAL_REQUIRED]\n操作: 示例\n[/APPROVAL_REQUIRED]\n"
            "正式申请：\n"
            "[APPROVAL_REQUIRED]\n"
            "操作: 实际操作 Y\n"
            "[/APPROVAL_REQUIRED]"
        )
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "实际操作 Y"

    def test_unclosed_block_with_closing_tag_earlier_rejected(self):
        """Unclosed-path: a [/APPROVAL_REQUIRED] appears earlier and another
        [APPROVAL_REQUIRED] follows without closing — this is the persona
        leak shape with an extra dangling tag. Must NOT parse."""
        text = (
            "示例块：\n"
            "[APPROVAL_REQUIRED]\n操作: ex\n[/APPROVAL_REQUIRED]\n"
            "更多说明...\n更多说明...\n更多说明...\n"
            "再次提及 [APPROVAL_REQUIRED] tag\n"
            "继续描述能力...\n继续描述能力...\n继续描述能力...\n"
        )
        assert approval.parse_approval_block(text) is None


# ---------------------------------------------------------------------------
# TestBuildApprovalCard — Migrated to M2-T13 scheduler
# (gateway _build_approval_card is UI/messaging concern, not runtime/approval.py)
# ---------------------------------------------------------------------------

# Migrated to M2-T13 scheduler


# ---------------------------------------------------------------------------
# TestCheckApprovalPermission — migrated from legacy TestCheckApprovalPermission
# ---------------------------------------------------------------------------


class TestCheckApprovalPermission:
    def test_original_sender(self):
        """Original sender always has permission."""
        appr = _make_approval(sender_id="ou_user_001", admin_users=["ou_admin_001"])
        assert approval.check_permission(appr, "ou_user_001", ["ou_admin_001"]) is True

    def test_admin_user(self):
        """Admin user has permission."""
        appr = _make_approval(sender_id="ou_user_001", admin_users=["ou_admin_001"])
        assert approval.check_permission(appr, "ou_admin_001", ["ou_admin_001"]) is True

    def test_unauthorized(self):
        """Random user without permission returns False."""
        appr = _make_approval(sender_id="ou_user_001", admin_users=["ou_admin_001"])
        assert approval.check_permission(appr, "ou_random_999", ["ou_admin_001"]) is False


# ---------------------------------------------------------------------------
# TestHandleMessageApproval — Migrated to M2-T13 scheduler
# (depends on handle_message/run_claude/send_reply gateway functions)
# ---------------------------------------------------------------------------

# Migrated to M2-T13 scheduler


# ---------------------------------------------------------------------------
# TestHandleApprovalReply — migrated from legacy TestHandleApprovalReply
# ---------------------------------------------------------------------------


class TestHandleApprovalReply:
    def test_confirm_transitions_to_approved(self):
        """'确认' on PENDING approval → APPROVED state."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001")
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="确认",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "approved"
        assert result.approval is not None
        assert result.approval.state == State.APPROVED

    def test_cancel_transitions_to_cancelled(self):
        """'取消' on PENDING approval → CANCELLED state."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001")
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="取消",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "cancelled"
        assert result.approval is not None
        assert result.approval.state == State.CANCELLED

    def test_unauthorized_reply_ignored(self):
        """Unauthorized sender gets 'ignored' and state unchanged."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001")
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_stranger",
            text="确认",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "ignored"
        appr = approval.get("thread_001")
        assert appr is not None
        assert appr.state == State.PENDING

    def test_confirm_on_non_pending_ignored(self):
        """'确认' on CANCELLED approval → ignored, state stays CANCELLED."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001", state=State.CANCELLED)
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="确认",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "ignored"
        appr = approval.get("thread_001")
        assert appr is not None
        assert appr.state == State.CANCELLED

    def test_retry_on_failed(self):
        """'重试' on FAILED approval → EXECUTING state."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001", state=State.FAILED)
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="重试",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "retry"
        assert result.approval is not None
        assert result.approval.state == State.EXECUTING

    def test_unrecognized_reply_ignored(self):
        """Non-command text → 'unrelated' even if pending exists, state unchanged."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001")
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="你好",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "unrelated"
        appr = approval.get("thread_001")
        assert appr is not None
        assert appr.state == State.PENDING


# ---------------------------------------------------------------------------
# TestEnvironmentTieredApproval — env-aware approve permission (2026-05-25):
# BOE/test writes may be cleared by the requester; 线上/production writes
# require an admin; unknown/empty environment fails safe to production.
# ---------------------------------------------------------------------------


class TestIsProduction:
    @pytest.mark.parametrize("env", ["", "  ", "线上", "生产", "online", "PROD", "正式"])
    def test_production_or_unknown_is_production(self, env):
        assert approval.is_production(ApprovalInfo(operation="x", environment=env)) is True

    @pytest.mark.parametrize("env", ["BOE", "boe", "测试", "test", "PPE", "staging", "联调", "预发"])
    def test_nonprod_is_not_production(self, env):
        assert approval.is_production(ApprovalInfo(operation="x", environment=env)) is False

    def test_prod_token_wins_over_nonprod(self):
        """If both appear, fail safe to production."""
        assert approval.is_production(
            ApprovalInfo(operation="x", environment="boe 线上"),
        ) is True


class TestEnvironmentTieredApproval:
    def test_production_original_sender_needs_admin(self):
        """线上: the requester's own '确认' is not enough → ignored/needs_admin."""
        _make_approval(sender_id="ou_user_001", environment="线上")
        result = approval.handle_reply(
            "thread_001", "ou_user_001", "确认", ["ou_admin_001"],
        )
        assert result.action == "ignored"
        assert result.reason == "needs_admin"
        assert approval.get("thread_001").state == State.PENDING

    def test_production_admin_can_approve(self):
        """线上: an admin '确认' → APPROVED."""
        _make_approval(sender_id="ou_user_001", environment="线上")
        result = approval.handle_reply(
            "thread_001", "ou_admin_001", "确认", ["ou_admin_001"],
        )
        assert result.action == "approved"
        assert result.approval.state == State.APPROVED

    def test_boe_original_sender_can_approve(self):
        """BOE: the requester's own '确认' is sufficient → APPROVED."""
        _make_approval(sender_id="ou_user_001", environment="BOE")
        result = approval.handle_reply(
            "thread_001", "ou_user_001", "确认", ["ou_admin_001"],
        )
        assert result.action == "approved"

    def test_boe_stranger_still_denied(self):
        """BOE: a third party (not requester, not admin) is still denied."""
        _make_approval(sender_id="ou_user_001", environment="BOE")
        result = approval.handle_reply(
            "thread_001", "ou_stranger", "确认", ["ou_admin_001"],
        )
        assert result.action == "ignored"
        assert result.reason == "permission"

    def test_empty_environment_fails_safe_to_production(self):
        """Unclassified write → treated as production → requester needs admin."""
        _make_approval(sender_id="ou_user_001", environment="")
        result = approval.handle_reply(
            "thread_001", "ou_user_001", "确认", ["ou_admin_001"],
        )
        assert result.action == "ignored"
        assert result.reason == "needs_admin"

    def test_production_requester_can_still_cancel(self):
        """Cancel is not admin-gated: the requester can always cancel."""
        _make_approval(sender_id="ou_user_001", environment="线上")
        result = approval.handle_reply(
            "thread_001", "ou_user_001", "取消", ["ou_admin_001"],
        )
        assert result.action == "cancelled"


# ---------------------------------------------------------------------------
# TestApprovalTimeout — adapted: timeout scheduling is scheduler's responsibility,
# but we test State.TIMEOUT transition via transition() API
# ---------------------------------------------------------------------------


class TestApprovalTimeout:
    def test_timeout_transition_sets_timeout_state(self):
        """transition() can force-set TIMEOUT state (as scheduler would do)."""
        appr = _make_approval(thread_key="thread_001")
        assert appr.state == State.PENDING
        approval.transition(appr, State.TIMEOUT)
        assert appr.state == State.TIMEOUT
        # In-store approval also reflects the change
        stored = approval.get("thread_001")
        assert stored is not None
        assert stored.state == State.TIMEOUT

    def test_approved_after_timeout_is_ignored(self):
        """Once TIMEOUT, '确认' command is ignored (state not PENDING)."""
        appr = _make_approval(thread_key="thread_001")
        approval.transition(appr, State.TIMEOUT)
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="确认",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "ignored"
        assert approval.get("thread_001").state == State.TIMEOUT


# ---------------------------------------------------------------------------
# TestWritePhase — Migrated to M2-T13 scheduler
# (depends on _execute_write/_run_claude_write gateway internals)
# ---------------------------------------------------------------------------

# Migrated to M2-T13 scheduler


# ---------------------------------------------------------------------------
# TestResetApprovals — migrated from legacy TestResetApprovals
# ---------------------------------------------------------------------------


class TestResetApprovals:
    def test_clears_state(self):
        """reset() clears all pending approvals."""
        approval.create(
            thread_key="key1",
            agent_name="agent",
            info=ApprovalInfo(),
            sender_id="u1",
            admin_users=[],
            approval_timeout=300,
        )
        approval.create(
            thread_key="key2",
            agent_name="agent",
            info=ApprovalInfo(),
            sender_id="u2",
            admin_users=[],
            approval_timeout=300,
        )
        approval.reset()
        assert approval.get("key1") is None
        assert approval.get("key2") is None


# ---------------------------------------------------------------------------
# TestHandleMessageApprovalReplyRouting — Migrated to M2-T13 scheduler
# (depends on handle_message gateway function)
# ---------------------------------------------------------------------------

# Migrated to M2-T13 scheduler


# ---------------------------------------------------------------------------
# Additional state machine edge-case tests (supplement legacy coverage)
# ---------------------------------------------------------------------------


class TestStateMachineEdgeCases:
    def test_no_pending_approval_returns_unrelated(self):
        """handle_reply with no pending approval returns 'unrelated'."""
        result = approval.handle_reply(
            thread_key="nonexistent",
            sender_id="ou_user_001",
            text="确认",
            admin_users=[],
        )
        assert result.action == "unrelated"
        assert result.approval is None

    def test_tongyi_keyword_also_approves(self):
        """'同意' keyword also triggers approval."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001")
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="同意",
            admin_users=[],
        )
        assert result.action == "approved"
        assert result.approval.state == State.APPROVED

    def test_cancel_on_failed_allowed(self):
        """'取消' on FAILED approval is allowed."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001", state=State.FAILED)
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="取消",
            admin_users=[],
        )
        assert result.action == "cancelled"
        assert result.approval.state == State.CANCELLED

    def test_retry_on_pending_ignored(self):
        """'重试' on PENDING approval is ignored."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001")
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="重试",
            admin_users=[],
        )
        assert result.action == "ignored"
        assert approval.get("thread_001").state == State.PENDING

    def test_cancel_on_approved_ignored(self):
        """'取消' on APPROVED approval is ignored."""
        _make_approval(thread_key="thread_001", sender_id="ou_user_001", state=State.APPROVED)
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_user_001",
            text="取消",
            admin_users=[],
        )
        assert result.action == "ignored"
        assert approval.get("thread_001").state == State.APPROVED

    def test_admin_can_approve(self):
        """Admin user can approve even if not the original sender."""
        _make_approval(
            thread_key="thread_001",
            sender_id="ou_user_001",
            admin_users=["ou_admin_001"],
        )
        result = approval.handle_reply(
            thread_key="thread_001",
            sender_id="ou_admin_001",
            text="确认",
            admin_users=["ou_admin_001"],
        )
        assert result.action == "approved"

    def test_remove_deletes_approval(self):
        """remove() returns and deletes the approval."""
        _make_approval(thread_key="thread_001")
        removed = approval.remove("thread_001")
        assert removed is not None
        assert removed.thread_key == "thread_001"
        assert approval.get("thread_001") is None

    def test_remove_nonexistent_returns_none(self):
        """remove() on nonexistent key returns None."""
        result = approval.remove("no_such_thread")
        assert result is None

    def test_transition_sets_executing(self):
        """transition() can set EXECUTING state."""
        appr = _make_approval(thread_key="thread_001")
        approval.transition(appr, State.EXECUTING)
        assert appr.state == State.EXECUTING

    def test_transition_sets_done(self):
        """transition() can set DONE state."""
        appr = _make_approval(thread_key="thread_001", state=State.EXECUTING)
        approval.transition(appr, State.DONE)
        assert appr.state == State.DONE

    def test_create_returns_pending_approval(self):
        """create() returns an Approval with PENDING state."""
        info = ApprovalInfo(operation="deploy", reason="new version")
        appr = approval.create(
            thread_key="thread_new",
            agent_name="billing_agent",
            info=info,
            sender_id="ou_user_001",
            admin_users=["ou_admin_001"],
            approval_timeout=600,
        )
        assert appr.state == State.PENDING
        assert appr.thread_key == "thread_new"
        assert appr.agent_name == "billing_agent"
        assert appr.info.operation == "deploy"
        assert appr.approval_id != ""
        assert appr.created_at > 0

    def test_get_returns_stored_approval(self):
        """get() returns the stored approval."""
        appr = _make_approval(thread_key="thread_001")
        stored = approval.get("thread_001")
        assert stored is appr

    def test_get_nonexistent_returns_none(self):
        """get() on unknown key returns None."""
        assert approval.get("no_such_key") is None

    def test_parse_chinese_colon(self):
        """parse_approval_block handles Chinese full-width colon ：."""
        text = "[APPROVAL_REQUIRED]\n操作：部署新版本\n原因：修复 bug\n[/APPROVAL_REQUIRED]"
        result = approval.parse_approval_block(text)
        assert result is not None
        assert result.operation == "部署新版本"
        assert result.reason == "修复 bug"

    def test_conftest_reset_works(self):
        """Verify conftest autouse reset: _pending should be empty at test start."""
        # conftest calls reset() before each test, so _pending must be empty here
        assert approval.get("any_key") is None

    def test_parse_multiline_impact(self):
        """parse_approval_block supports multi-line field values."""
        text = """[APPROVAL_REQUIRED]
操作: 修改 foo
原因: 用户请求
影响: billing 服务
     以及 downstream A
     以及 downstream B
回滚: git revert
[/APPROVAL_REQUIRED]"""
        info = approval.parse_approval_block(text)
        assert info is not None
        assert info.operation == "修改 foo"
        assert info.reason == "用户请求"
        assert "billing 服务" in info.impact
        assert "downstream A" in info.impact
        assert "downstream B" in info.impact
        assert info.rollback == "git revert"


# ---------------------------------------------------------------------------
# TestCreateOverwrite — Important #2: overwrite warning log
# ---------------------------------------------------------------------------


class TestCreateOverwrite:
    def test_create_overwrites_existing_logs_warning(self, caplog):
        """create() on existing PENDING key logs a warning."""
        import logging

        info = ApprovalInfo(operation="A")
        approval.create("thread_001", "billing", info, "ou_user_001", ["ou_admin_001"], 1800)
        with caplog.at_level(logging.WARNING, logger="agent_runtime.approval"):
            approval.create("thread_001", "billing", info, "ou_user_001", ["ou_admin_001"], 1800)
        assert any("overwriting existing PENDING" in r.message for r in caplog.records)
        assert approval.get("thread_001") is not None  # new approval replaces old

    def test_create_no_warning_when_not_pending(self, caplog):
        """create() over a non-PENDING entry does not log overwrite warning."""
        import logging

        info = ApprovalInfo(operation="A")
        appr = approval.create("thread_001", "billing", info, "ou_user_001", [], 300)
        appr.state = State.DONE
        with caplog.at_level(logging.WARNING, logger="agent_runtime.approval"):
            approval.create("thread_001", "billing", info, "ou_user_001", [], 300)
        assert not any("overwriting existing PENDING" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# TestTerminalStateIdempotency — Minor M-3: parametrize 12 tests
# Terminal states should ignore all commands (at-least-once msg replay safe)
# ---------------------------------------------------------------------------


class TestTerminalStateIdempotency:
    """Terminal states should ignore all commands (at-least-once msg replay safe)."""

    @pytest.mark.parametrize("terminal_state", [State.DONE, State.CANCELLED, State.TIMEOUT])
    @pytest.mark.parametrize("cmd", ["确认", "同意", "取消", "重试"])
    def test_terminal_state_ignores_all_commands(self, terminal_state, cmd):
        _make_approval(state=terminal_state)
        result = approval.handle_reply("thread_001", "ou_user_001", cmd, ["ou_admin_001"])
        assert result.action == "ignored", f"expected ignored for {terminal_state} + {cmd}"
        assert approval.get("thread_001").state == terminal_state  # state unchanged
