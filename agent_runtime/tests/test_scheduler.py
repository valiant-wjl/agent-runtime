"""Tests for scheduler.handle_message (5 migrated + 6 new from review)."""

import asyncio
import logging
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import approval, concurrency, scheduler, session
from agent_runtime.claude_proc import RunResult


_PROJECT_CFG = {
    "work_dir": "/tmp/billing",
    "display_name": "BillingBot",
    "model": "opus",
    "admin_users": ["ou_admin"],
    "approval_timeout": 1800,
    "read_phase": {
        "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
        "disallowed_bash_patterns": [],
    },
    "write_phase": {"timeout": 600},
    "supported_msg_types": ["text", "post"],
    "unsupported_msg_reply": "暂不支持",
}

_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


def _make_parsed(text="hello", message_type="text", sender_id="ou-sender"):
    return ParsedMsg(
        channel="feishu",
        message_id="m-1",
        thread_root_id="t-1",
        chat_id="c-1",
        sender_id=sender_id,
        sender_name="u",
        text=text,
        mentions=[],
        raw_event={"event": {"message": {"message_type": message_type}}},
    )


def _make_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.reply = AsyncMock(return_value=None)
    ch.send_card = AsyncMock(return_value="card-msg-1")
    ch.update_card = AsyncMock(return_value=None)
    return ch


@pytest.fixture(autouse=True)
def _init_concurrency_for_scheduler_tests():
    """Initialize global semaphore before each test; conftest resets after."""
    concurrency.init_global(10)
    yield


# ---------------------------------------------------------------------------
# Original 5 migrated tests (call _handle_message_inner to bypass semaphore
# in unit-test context — handle_message public API tested in integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_message_happy_path_read_phase(tmp_path):
    """Normal text → claude_proc.run → final card flip carries answer; session persisted.

    US-003 changed the buffered-path contract: the placeholder '🔄 分析中...'
    card is now flipped to a final card via update_card instead of being
    abandoned + supplemented with a separate text reply. Verify the answer
    reaches the user via the flipped card.
    """
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _make_parsed("query")
    fake_result = RunResult(text="answer", session_id="sess-new", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    ch.update_card.assert_called_once()
    flipped_card = ch.update_card.call_args[0][1]
    assert "answer" in str(flipped_card)
    assert session.get("t-1")["session_id"] == "sess-new"


@pytest.mark.asyncio
async def test_handle_message_approval_block_triggers_card(tmp_path):
    """[APPROVAL_REQUIRED] in output → approval.create + send_card, no normal reply."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _make_parsed("change config")
    claude_out = (
        "分析结果\n[APPROVAL_REQUIRED]\n操作: 修改 TCC\n原因: test\n影响: small\n"
        "回滚: revert\n[/APPROVAL_REQUIRED]"
    )
    fake_result = RunResult(text=claude_out, session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    # approval 被 create
    appr = approval.get("t-1")
    assert appr is not None
    assert appr.info.operation == "修改 TCC"
    # send_card 被调 2 次：1 次初始 "分析中" + 1 次审批卡片
    assert ch.send_card.call_count >= 2
    # normal reply 没有被调
    ch.reply.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_unsupported_msg_type_rejected(tmp_path):
    """image message → reject with unsupported_msg_reply, no claude_proc.run."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _make_parsed(text="", message_type="image")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    ch.reply.assert_called_once_with(parsed, "暂不支持")
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_handle_message_reuses_session_id(tmp_path):
    """Existing session → claude_proc.run called with session_id=prev."""
    session.configure(tmp_path / "sess.json")
    session.put("t-1", "sess-prev", agent="billing")
    ch = _make_channel()
    parsed = _make_parsed()
    fake_result = RunResult(text="ok", session_id="sess-new", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    _, kwargs = mock_run.call_args
    assert kwargs["session_id"] == "sess-prev"


@pytest.mark.asyncio
async def test_handle_message_approval_confirm_triggers_write_phase(tmp_path):
    """Existing PENDING approval + '确认' + permission OK → write phase executed."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(
        operation="do X", reason="r", impact="i", rollback="b", environment="BOE",
    )
    approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    ch = _make_channel()
    parsed = _make_parsed(text="确认")
    fake_exec = RunResult(text="done", session_id=None, exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_exec)) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    mock_run.assert_called_once()
    ch.reply.assert_called_once()
    reply_text = ch.reply.call_args[0][1]
    assert "done" in reply_text or "✅" in reply_text


# ---------------------------------------------------------------------------
# 6 new tests from review (Imp 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_does_not_persist_session(tmp_path):
    """Claude timeout → reply sent, session NOT put."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _make_parsed()
    fake_result = RunResult(
        text="⚠️ 分析超时",
        session_id=None,
        exit_code=-1,
        timed_out=True,
    )
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    assert session.get("t-1") is None


@pytest.mark.asyncio
async def test_executing_state_replies_busy(tmp_path):
    """EXECUTING state → reply '执行中', no new read phase triggered."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="do X")
    appr = approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    approval.transition(appr, approval.State.EXECUTING)
    ch = _make_channel()
    parsed = _make_parsed(text="新问题")
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    ch.reply.assert_called_once()
    assert "执行中" in ch.reply.call_args[0][1]
    mock_run.assert_not_called()


@pytest.mark.asyncio
async def test_non_admin_approval_confirm_ignored(tmp_path):
    """非 admin 非 owner 发'确认' → ignored, 写阶段不执行, approval stays PENDING."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="dangerous")
    approval.create("t-1", "billing", info, "ou-original-asker", ["ou_admin"], 1800)
    ch = _make_channel()
    # sender is neither original asker nor admin
    parsed = ParsedMsg(
        channel="feishu",
        message_id="m-2",
        thread_root_id="t-1",
        chat_id="c-1",
        sender_id="ou-hacker",
        sender_name="h",
        text="确认",
        mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    mock_run.assert_not_called()
    assert approval.get("t-1").state == approval.State.PENDING


@pytest.mark.asyncio
async def test_write_phase_success_removes_approval(tmp_path):
    """Write phase success → approval removed from store."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="X", environment="BOE")
    approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    ch = _make_channel()
    parsed = _make_parsed(text="确认")
    fake = RunResult(text="done", session_id=None, exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    assert approval.get("t-1") is None


@pytest.mark.asyncio
async def test_write_phase_failure_keeps_approval_failed(tmp_path):
    """Write phase failure → approval kept with FAILED state for retry."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="X", environment="BOE")
    approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    ch = _make_channel()
    parsed = _make_parsed(text="确认")
    fake = RunResult(text="err", session_id=None, exit_code=1)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    appr_after = approval.get("t-1")
    assert appr_after is not None
    assert appr_after.state == approval.State.FAILED


# ---------------------------------------------------------------------------
# Environment-tiered approval (2026-05-25): 线上 needs admin + names the
# approver; BOE clears on the requester's own 确认.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_online_write_card_mentions_admin(tmp_path):
    """A 线上 approval card names the required admin via a real @mention."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _make_parsed("发布线上")
    claude_out = (
        "分析\n[APPROVAL_REQUIRED]\n操作: 发布 TCC\n环境: 线上\n"
        "原因: 上线\n影响: 全量\n回滚: 回退\n[/APPROVAL_REQUIRED]"
    )
    fake = RunResult(text=claude_out, session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    card_text = ch.send_card.call_args_list[-1][0][1]["fallback_text"]
    assert "线上" in card_text
    assert '<at id="ou_admin"></at>' in card_text  # real @mention of admin


@pytest.mark.asyncio
async def test_online_non_admin_confirm_blocked_with_feedback(tmp_path):
    """线上 + 发起人(非 admin)确认 → 写阶段不跑, PENDING, 明确反馈需管理员."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="发布 TCC", environment="线上")
    approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    ch = _make_channel()
    parsed = _make_parsed(text="确认", sender_id="ou-sender")  # original requester
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    mock_run.assert_not_called()
    assert approval.get("t-1").state == approval.State.PENDING
    # feedback card names the admin
    feedback = ch.send_card.call_args_list[-1][0][1]["fallback_text"]
    assert "管理员" in feedback
    assert '<at id="ou_admin"></at>' in feedback


@pytest.mark.asyncio
async def test_online_admin_confirm_executes(tmp_path):
    """线上 + admin 确认 → 写阶段执行."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="发布 TCC", environment="线上")
    approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    ch = _make_channel()
    parsed = _make_parsed(text="确认", sender_id="ou_admin")  # admin
    fake = RunResult(text="done", session_id=None, exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_boe_sender_confirm_executes(tmp_path):
    """BOE + 发起人确认 → 写阶段执行(无需 admin)."""
    session.configure(tmp_path / "sess.json")
    info = approval.ApprovalInfo(operation="改 BOE 配置", environment="BOE")
    approval.create("t-1", "billing", info, "ou-sender", ["ou_admin"], 1800)
    ch = _make_channel()
    parsed = _make_parsed(text="确认", sender_id="ou-sender")
    fake = RunResult(text="done", session_id=None, exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as mock_run:
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_approval_card_contains_all_fields(tmp_path):
    """Approval card text contains all four fields: operation/reason/impact/rollback."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _make_parsed("change config")
    claude_out = (
        "分析\n[APPROVAL_REQUIRED]\n"
        "操作: 修改 TCC limit\n"
        "原因: 业务需要\n"
        "影响: billing 全量\n"
        "回滚: 恢复 100\n"
        "[/APPROVAL_REQUIRED]"
    )
    fake = RunResult(text=claude_out, session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)):
        await scheduler.handle_message(ch, parsed, "billing", _PROJECT_CFG, _RUNTIME_CFG)
    # 第 1 次 send_card 是"分析中"; 第 2 次是审批卡
    assert ch.send_card.call_count >= 2
    card_text = ch.send_card.call_args_list[-1][0][1]["fallback_text"]
    assert "修改 TCC limit" in card_text
    assert "业务需要" in card_text
    assert "billing 全量" in card_text
    assert "恢复 100" in card_text


# ---------------------------------------------------------------------------
# US-002: subscribe natural-end logs INFO, not WARNING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_subscribe_natural_end_logs_info_not_warning(caplog):
    """lark-cli SSE batch 自然结束（无异常）→ INFO 级日志而非 WARNING.

    Why: 当前 lark-cli `event +subscribe` 接到一批消息就 close，scheduler
    重新订阅是预期的运行模式（不是错误）。legacy 'ended unexpectedly'
    WARNING 字面会刷爆 runtime.log，掩盖真异常。改成 INFO + 'completed'
    措辞，让运行时统计可见但不抢占注意力。
    """
    async def _empty_iter():
        # async-generator with zero yields — terminates immediately,
        # mimicking lark-cli closing after an empty event batch.
        if False:
            yield  # pragma: no cover

    ch = AsyncMock()
    ch.name = "feishu"
    ch.subscribe = lambda: _empty_iter()
    ch.parse = AsyncMock(return_value=None)
    # close() raises CancelledError so the while-True loop exits cleanly.
    ch.close = AsyncMock(side_effect=asyncio.CancelledError())

    caplog.set_level(logging.DEBUG, logger="agent_runtime.scheduler")

    with pytest.raises(asyncio.CancelledError):
        await scheduler.consume(ch, {}, {}, {}, None)

    # The natural-end reconnect log must exist and be at INFO level.
    completed_records = [r for r in caplog.records if "completed" in r.message]
    assert completed_records, (
        f"expected INFO 'completed' log; got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    assert all(r.levelname == "INFO" for r in completed_records), (
        f"reconnect log must be INFO; got: "
        f"{[(r.levelname, r.message) for r in completed_records]}"
    )

    # Legacy "ended unexpectedly" wording must be removed entirely so log
    # tooling that grepped on it doesn't mask the new INFO line as anomaly.
    legacy = [r for r in caplog.records if "ended unexpectedly" in r.message]
    assert not legacy, (
        f"legacy 'ended unexpectedly' wording must be removed; got: "
        f"{[(r.levelname, r.message) for r in legacy]}"
    )


# ---------------------------------------------------------------------------
# Routing dispatch observability (US-006): consume() must emit an INFO log
# when a parsed message is matched to a project, so live debugging from
# runtime.log can confirm "did the message reach a project, and via which
# routing signal?" without re-deriving from upstream context.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consume_logs_dispatch_decision_p2p(caplog):
    """Single-project p2p (Strategy 0) → INFO 'dispatched' with project/chat/msg/chat_type/mentioned."""
    parsed = ParsedMsg(
        channel="feishu",
        message_id="m-disp",
        thread_root_id="t-disp",
        chat_id="c-disp",
        sender_id="ou-x",
        sender_name="x",
        text="hi",
        mentions=[],
        chat_type="p2p",
        raw_event={"event": {"message": {"message_type": "text"}}},
    )

    async def _one_event():
        yield {"raw": "ev"}

    ch = AsyncMock()
    ch.name = "feishu"
    ch.subscribe = lambda: _one_event()
    ch.parse = AsyncMock(return_value=parsed)
    # close() raises CancelledError so the while-True loop exits cleanly after
    # the first iteration.
    ch.close = AsyncMock(side_effect=asyncio.CancelledError())

    caplog.set_level(logging.DEBUG, logger="agent_runtime.scheduler")

    with patch(
        "agent_runtime.scheduler.handle_message", AsyncMock(return_value=None)
    ):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.consume(
                ch,
                {"billing": _PROJECT_CFG},
                _RUNTIME_CFG,
                {},
                bot_mention_key=None,
            )

    dispatched = [r for r in caplog.records if "dispatched" in r.message]
    assert dispatched, (
        f"expected INFO 'dispatched' log; got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    assert all(r.levelname == "INFO" for r in dispatched)
    msg = dispatched[0].message
    assert "project=billing" in msg
    assert "chat=c-disp" in msg
    assert "msg=m-disp" in msg
    assert "chat_type=p2p" in msg
    assert "mentioned=False" in msg


@pytest.mark.asyncio
async def test_consume_logs_dispatch_decision_mentioned(caplog):
    """Group + mention → 'mentioned=True' in dispatched log."""
    parsed = ParsedMsg(
        channel="feishu",
        message_id="m-grp",
        thread_root_id="t-grp",
        chat_id="c-grp",
        sender_id="ou-x",
        sender_name="x",
        text="@bot hi",
        mentions=["@bot"],
        chat_type="group",
        raw_event={"event": {"message": {"message_type": "text"}}},
    )

    async def _one_event():
        yield {"raw": "ev"}

    ch = AsyncMock()
    ch.name = "feishu"
    ch.subscribe = lambda: _one_event()
    ch.parse = AsyncMock(return_value=parsed)
    ch.close = AsyncMock(side_effect=asyncio.CancelledError())

    caplog.set_level(logging.DEBUG, logger="agent_runtime.scheduler")

    with patch(
        "agent_runtime.scheduler.handle_message", AsyncMock(return_value=None)
    ):
        with pytest.raises(asyncio.CancelledError):
            await scheduler.consume(
                ch,
                {"billing": _PROJECT_CFG},
                _RUNTIME_CFG,
                {},
                bot_mention_key="@bot",
            )

    dispatched = [r for r in caplog.records if "dispatched" in r.message]
    assert dispatched
    assert "mentioned=True" in dispatched[0].message
    assert "chat_type=group" in dispatched[0].message
