"""Regression tests: streaming final answer must be the CLI-assembled final
assistant message (the `result` event), NOT the concatenation of every
text_delta across the turn.

Bug (observed in prod, Miaoda quota change 2026-06-01): a 20-tool turn posted
a card body that was the whole running commentary glued together —
"先 dry-run… 校验器有坑… Build 成功… 已发布… 复验线上值…" — because
``_run_read_stream`` appended EVERY text_delta into the answer. stream-json
emits a text_delta for every assistant text block, including the "what I'm
about to do" narration between tool calls, so the conclusion got buried under
the process narration. SOUL.md persona tweaks cannot fix this — it is a
stream-assembly bug, not a style problem.

Plan A fix: take the final answer from the `result` event's ``result`` field
(same source as the buffered path's ``data["result"]``); text_delta feeds only
the live progress card. ``composed`` (joined deltas) is kept solely as a
fallback for when no usable result event arrives.
"""

from unittest.mock import AsyncMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import scheduler, session


@pytest.fixture(autouse=True)
def _configure_session(tmp_path):
    session.configure(tmp_path / "sess.json")
    yield


@pytest.fixture
def fake_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.send_card = AsyncMock(return_value="om_card1")
    ch.update_card = AsyncMock(return_value=True)
    ch.reply = AsyncMock(return_value=None)
    return ch


@pytest.fixture
def parsed():
    return ParsedMsg(
        channel="feishu",
        message_id="om_q",
        thread_root_id="om_q",
        chat_id="oc_chat",
        sender_id="ou_user",
        sender_name="u",
        text="调整 Miaoda 的配置，将工具 generateImage 的额度由 0.05 调整为 500，只改 boe 的",
        mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


@pytest.fixture
def project_cfg():
    return {
        "work_dir": "/tmp/proj",
        "model": "sonnet",
        "read_phase": {
            "disallowed_tools": ["Edit", "Write"],
            "disallowed_bash_patterns": [],
        },
    }


@pytest.fixture
def runtime_cfg():
    return {
        "paths": {"meta_work_dir": "/tmp/meta"},
        "reply_timeout": 30,
        "channels": {
            "feishu": {
                "stream_card": {
                    "enabled": True,
                    "throttle_ms": 100,
                    "throttle_tool_calls": 2,
                }
            }
        },
    }


async def _fake_stream(events):
    for ev in events:
        yield ev


def _delta(text):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _tool(name, **inp):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "content_block": {"type": "tool_use", "name": name, "input": inp},
        },
    }


# The narration the model emits BETWEEN tool calls — exactly the kind of text
# that polluted the prod card. None of it must appear in the final answer.
_NARRATION = [
    "先 dry-run 预览改动，确认只动 BOE、只动这一项。",
    "校验器在本机 macOS arm64 上有已知的编译坑，先预编译 + 签名。",
    "Build 成功了。现在恢复 go.mod/go.sum、签名，再复用这个二进制跑校验。",
    "lib.sh 把 EB_VALIDATE_BIN 硬重置了，改成尊重已有 env。",
    "Edit 工具不可用，用 sed 改这一行。校验通过。",
]
# The clean, conclusion-first final message the CLI assembles on the result event.
_FINAL = (
    "已完成：generateImage 单价从 0.05 调到 500，调用一次扣 500（涉及的 3 个事件已同步），"
    "其它工具与模型计费规则未动。线上只改了 BOE，PRE/PROD 未触碰。"
)


@pytest.mark.asyncio
async def test_final_text_is_result_event_not_concatenated_narration(
    fake_channel, parsed, project_cfg, runtime_cfg,
):
    """Final answer == result event's text; inter-tool narration is excluded."""
    events = []
    for i, line in enumerate(_NARRATION):
        events.append(_delta(line))
        events.append(_tool(f"Bash{i}", command=f"step-{i}"))
    events.append(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "sess-miaoda",
            "result": _FINAL,
        }
    )

    import agent_runtime.claude_proc as cp
    orig = cp.run_stream
    cp.run_stream = lambda **kw: _fake_stream(events)
    try:
        result = await scheduler._run_read_stream(
            fake_channel, parsed, project_cfg, runtime_cfg, None,
            prompt=parsed.text,
        )
    finally:
        cp.run_stream = orig

    assert result.text == _FINAL
    for line in _NARRATION:
        assert line not in result.text, f"narration leaked into final answer: {line!r}"
    # session id still propagated from the result event
    assert result.session_id == "sess-miaoda"


@pytest.mark.asyncio
async def test_falls_back_to_concatenated_deltas_when_no_result_text(
    fake_channel, parsed, project_cfg, runtime_cfg,
):
    """Older CLI / missing result field: keep prior behavior (joined deltas)."""
    events = [
        _delta("partial answer "),
        _delta("continued"),
        {"type": "result", "subtype": "success", "session_id": "s"},  # no `result`
    ]
    import agent_runtime.claude_proc as cp
    orig = cp.run_stream
    cp.run_stream = lambda **kw: _fake_stream(events)
    try:
        result = await scheduler._run_read_stream(
            fake_channel, parsed, project_cfg, runtime_cfg, None, prompt=parsed.text,
        )
    finally:
        cp.run_stream = orig

    assert result.text == "partial answer continued"


@pytest.mark.asyncio
async def test_api_error_result_surfaced(
    fake_channel, parsed, project_cfg, runtime_cfg,
):
    """is_error result with text → surfaced as api error, not swallowed."""
    events = [
        _delta("some partial text"),
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "weekly quota exceeded",
            "api_error_status": 429,
            "session_id": "s",
        },
    ]
    import agent_runtime.claude_proc as cp
    orig = cp.run_stream
    cp.run_stream = lambda **kw: _fake_stream(events)
    try:
        result = await scheduler._run_read_stream(
            fake_channel, parsed, project_cfg, runtime_cfg, None, prompt=parsed.text,
        )
    finally:
        cp.run_stream = orig

    assert "weekly quota exceeded" in result.text
    assert "429" in result.text
    assert result.exit_code == 429


# ---------------------------------------------------------------------------
# Superseded-answer recovery (Plan 2).
#
# Follow-up bug (observed in prod, lbp_billing 2026-06-02, 27-tool 902s deep
# turn): the `result` event carries only the turn's LAST assistant message.
# The model delivered a rich, conclusion-first answer mid-turn (block #1,
# 1072 chars), then kept working (one extra verification tool call) and ended
# the turn with a SHORT self-referential closer (block #2, 134 chars):
# "分析已完成并交付…结果已纳入证据…结论不变。无需再操作。". result_text == block #2,
# so the user got the throwaway closer; the rich answer (which only streamed to
# the ephemeral progress card) was lost.
#
# Plan 2: track per-block text runs (split on tool_use); when the final message
# is BOTH short AND a self-referential meta-closer ("已交付 / 结论不变 / 无需再… /
# 已纳入… / 如上所述"), recover the richest earlier streamed block. Conservative:
# a genuinely concise conclusion (no meta phrasing) is never replaced, so this
# cannot reintroduce the narration-gluing bug the result-event fix removed.
# ---------------------------------------------------------------------------

# Rich, conclusion-first answer the model emits mid-turn (block #1).
_RICH_ANSWER = (
    "分析完成，证据链已闭合。\n\n"
    "## 结论\n"
    "计费没成功。这条 AnyClaw 计费事件卡在『计费上报』环节，整笔被计费服务拒绝，"
    "所以下游基线没入账、看不到计费。\n\n"
    "## 失败原因\n"
    "上报的计费明细里有一个大模型 ID 不在白名单：上游传的是带版本号的原始串 "
    "`doubao-seed-2-0-pro-260215`，而计费配置只认规范名 `Doubao-Seed-2.0-pro`。"
    "一个 item 不被识别，整笔上报就返回失败（StatusCode -1, unsupported llm ID），重试 4 次同样的错。\n\n"
    "## 链路（断点在第 3 环）\n"
    "AnyClaw 事件 → 权益校验 ✅ → 额度查询 ✅ → 计费上报 ❌ → 入库（未发生）\n\n"
    "## 修复\n"
    "把这个 Key 改成规范名，或在配置白名单里补别名，即可恢复。"
)
# Short, self-referential closer the model ends the turn with (block #2).
_META_CLOSER = (
    "分析已完成并交付，这个后台搜索就是确认 LLM ID 配置缺口用的，"
    "结果已纳入证据，结论不变。无需再操作。"
)


@pytest.mark.asyncio
async def test_recovers_rich_block_when_final_is_meta_closer(
    fake_channel, parsed, project_cfg, runtime_cfg,
):
    """Model answered mid-turn, then closed with a meta-ack → recover the rich block."""
    events = [
        _delta(_RICH_ANSWER),          # block #1: the real answer
        _tool("Bash", command="grep supported_ids"),  # one more verification
        _delta(_META_CLOSER),          # block #2: self-referential closer
        {
            "type": "result",
            "subtype": "success",
            "session_id": "sess-billing",
            "result": _META_CLOSER,    # result event carries ONLY the last message
        },
    ]
    import agent_runtime.claude_proc as cp
    orig = cp.run_stream
    cp.run_stream = lambda **kw: _fake_stream(events)
    try:
        result = await scheduler._run_read_stream(
            fake_channel, parsed, project_cfg, runtime_cfg, None, prompt=parsed.text,
        )
    finally:
        cp.run_stream = orig

    assert result.text == _RICH_ANSWER
    assert "无需再操作" not in result.text
    assert "结论不变" not in result.text
    assert result.session_id == "sess-billing"


@pytest.mark.asyncio
async def test_concise_conclusion_is_not_replaced_by_earlier_narration(
    fake_channel, parsed, project_cfg, runtime_cfg,
):
    """No false positive: a genuinely concise final answer (no meta phrasing)
    must NOT be swapped for a longer earlier exploration block — that would
    re-introduce the narration-as-answer bug."""
    long_exploration = (
        "先看一下上报链路。我去翻 ReportBillingEvent 的调用方，"
        "确认参数怎么组装的，再看计费配置在哪里加载白名单，"
        "顺便核对一下重试逻辑和返回码语义，分析过程比较长……" * 3
    )
    concise_final = "已完成：把模型 Key 改成规范名 `Doubao-Seed-2.0-pro` 就能恢复，单价不变。"
    events = [
        _delta(long_exploration),
        _tool("Read", file_path="billing.go"),
        _delta(concise_final),
        {
            "type": "result",
            "subtype": "success",
            "session_id": "s",
            "result": concise_final,
        },
    ]
    import agent_runtime.claude_proc as cp
    orig = cp.run_stream
    cp.run_stream = lambda **kw: _fake_stream(events)
    try:
        result = await scheduler._run_read_stream(
            fake_channel, parsed, project_cfg, runtime_cfg, None, prompt=parsed.text,
        )
    finally:
        cp.run_stream = orig

    assert result.text == concise_final
    assert "分析过程比较长" not in result.text


@pytest.mark.asyncio
async def test_meta_closer_kept_when_no_richer_earlier_block(
    fake_channel, parsed, project_cfg, runtime_cfg,
):
    """Meta-closer final but nothing substantial earlier → keep the closer
    (don't swap in trivially-short narration)."""
    events = [
        _delta("好的，我查一下。"),       # too short to be a real answer
        _tool("Bash", command="ls"),
        _delta(_META_CLOSER),
        {"type": "result", "subtype": "success", "session_id": "s", "result": _META_CLOSER},
    ]
    import agent_runtime.claude_proc as cp
    orig = cp.run_stream
    cp.run_stream = lambda **kw: _fake_stream(events)
    try:
        result = await scheduler._run_read_stream(
            fake_channel, parsed, project_cfg, runtime_cfg, None, prompt=parsed.text,
        )
    finally:
        cp.run_stream = orig

    assert result.text == _META_CLOSER


def test_looks_like_meta_closer_unit():
    """Tight matcher: self-referential closers match; legit conclusions don't."""
    assert scheduler._looks_like_meta_closer(_META_CLOSER)
    assert scheduler._looks_like_meta_closer("详见上文，不再赘述。")
    assert scheduler._looks_like_meta_closer("结论不变，无需再确认。")
    # legit concise conclusions that merely contain 已完成 must NOT match
    assert not scheduler._looks_like_meta_closer("已完成：改好了，单价 500。")
    assert not scheduler._looks_like_meta_closer("分析完成，根因是 Key 用错了。")
    assert not scheduler._looks_like_meta_closer(_RICH_ANSWER)
