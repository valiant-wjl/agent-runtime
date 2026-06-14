"""Tests for scheduler image-multimodal flow (S3) and topic-context flow (S5).

Mocks claude_proc.run + channel.download_image / channel.fetch_topic_history.
Asserts on the prompt built and passed to claude.
"""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu.adapter import ImageDownloadFailed, ImageTooLarge
from agent_runtime import concurrency, scheduler, session
from agent_runtime.claude_proc import RunResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _project_cfg(work_dir: Path, **overrides) -> dict:
    base = {
        "work_dir": str(work_dir),
        "display_name": "Bot",
        "model": "opus",
        "admin_users": [],
        "approval_timeout": 1800,
        "read_phase": {
            "disallowed_tools": ["Edit", "Write"],
            "disallowed_bash_patterns": [],
        },
        "write_phase": {"timeout": 600},
        # supported_msg_types intentionally omitted → exercise default
    }
    base.update(overrides)
    return base


_RUNTIME_CFG = {
    "reply_timeout": 300,
    "session_max_age": 86400,
    "per_chat_concurrent": 2,
}


def _parsed(
    *,
    text="hi",
    msg_type="text",
    image_keys=None,
    topic_id=None,
    thread_root_id="t-1",
    message_id="m-1",
):
    return ParsedMsg(
        channel="feishu",
        message_id=message_id,
        thread_root_id=thread_root_id,
        chat_id="c-1",
        sender_id="ou-sender",
        sender_name="u",
        text=text,
        mentions=[],
        raw_event={"event": {"message": {"message_type": msg_type}}},
        image_keys=image_keys or [],
        topic_id=topic_id,
    )


def _make_channel(history: list[str] | None = None):
    """Build a mocked channel.

    download_image side effect creates a small fake file at
    dest_dir / <image_key>.png and returns that path (mirroring real
    adapter behavior).
    """
    ch = AsyncMock()
    ch.name = "feishu"
    ch.reply = AsyncMock(return_value=None)
    ch.send_card = AsyncMock(return_value="card-1")
    ch.update_card = AsyncMock(return_value=None)
    ch.fetch_topic_history = AsyncMock(return_value=history or [])
    # Default: thread root anchor lookup returns None so existing tests
    # observe topic_history unchanged. Tests exercising the alarm-card
    # prepend path override this with a concrete string.
    ch.fetch_message_text = AsyncMock(return_value=None)

    async def _download(message_id, image_key, dest_dir, **kwargs):
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        p = dest_dir / f"{image_key}.png"
        p.write_bytes(b"x" * 64)
        return p

    ch.download_image = AsyncMock(side_effect=_download)
    return ch


@pytest.fixture(autouse=True)
def _init_concurrency():
    concurrency.init_global(10)
    yield


# ---------------------------------------------------------------------------
# S3 — image flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_message_default_supported_msg_types_accepts_image(tmp_path):
    """Default supported_msg_types now includes 'image' — no reject reply."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(
        text="[图片#1 (img_a)]", msg_type="image", image_keys=["img_a"],
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    # US-003: buffered path now flips the placeholder card with the answer
    # instead of dropping a separate text reply. The image-supported gate
    # is verified by the absence of the "暂不支持" reject — assert the
    # claude answer reached the user via update_card, AND no reject reply.
    ch.update_card.assert_called_once()
    flipped_card = ch.update_card.call_args[0][1]
    assert "ok" in str(flipped_card)
    # No "unsupported" reject reply was sent.
    for call in ch.reply.call_args_list:
        assert "暂不支持" not in call.args[1]


@pytest.mark.asyncio
async def test_image_keys_trigger_download_and_prompt_header(tmp_path):
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(
        text="解释一下这两张图",
        msg_type="image",
        image_keys=["img_a", "img_b"],
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    # Two download_image calls, one per image_key
    assert ch.download_image.await_count == 2
    download_keys = {
        c.kwargs["image_key"] for c in ch.download_image.await_args_list
    }
    assert download_keys == {"img_a", "img_b"}
    # Prompt to claude contains the Chinese header + the original text
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert "用户附带 2 张图片" in prompt
    assert "Read 工具" in prompt
    assert "解释一下这两张图" in prompt
    # Both absolute paths present
    expected_a = tmp_path / ".cache" / "images" / "m-1" / "img_a.png"
    expected_b = tmp_path / ".cache" / "images" / "m-1" / "img_b.png"
    assert str(expected_a) in prompt
    assert str(expected_b) in prompt


@pytest.mark.asyncio
async def test_image_partial_download_failure_marks_failed_in_prompt(tmp_path):
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    # First key succeeds (use real default side_effect); second raises
    real_download = ch.download_image.side_effect

    async def maybe_fail(message_id, image_key, dest_dir, **kwargs):
        if image_key == "img_bad":
            raise ImageDownloadFailed("network error")
        return await real_download(message_id, image_key, dest_dir, **kwargs)

    ch.download_image = AsyncMock(side_effect=maybe_fail)
    parsed = _parsed(
        text="t", msg_type="image", image_keys=["img_ok", "img_bad"],
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    # Successful path is in prompt
    assert "img_ok.png" in prompt
    # Failed key is annotated, not silently dropped
    assert "img_bad" in prompt
    assert "下载失败" in prompt


@pytest.mark.asyncio
async def test_image_oversized_marked_failed(tmp_path):
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()

    async def too_big(message_id, image_key, dest_dir, **kwargs):
        raise ImageTooLarge("12 MB > 10 MB")

    ch.download_image = AsyncMock(side_effect=too_big)
    parsed = _parsed(text="t", msg_type="image", image_keys=["img_huge"])
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert "img_huge" in prompt
    assert "下载失败" in prompt


@pytest.mark.asyncio
async def test_image_cleanup_after_run(tmp_path):
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(text="t", msg_type="image", image_keys=["img_a"])
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    expected = tmp_path / ".cache" / "images" / "m-1" / "img_a.png"
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    # Best-effort cleanup must remove the downloaded file after read.
    assert not expected.exists()


@pytest.mark.asyncio
async def test_no_image_keys_no_download_no_header(tmp_path):
    """Plain text message: download_image untouched, prompt unchanged."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(text="just text", msg_type="text")
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    ch.download_image.assert_not_called()
    _, kwargs = mock_run.call_args
    assert kwargs["prompt"] == "just text"


# ---------------------------------------------------------------------------
# S5 — topic flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_id_used_as_session_key_when_present(tmp_path):
    """topic_id set → session.put keys by topic_id, NOT by thread_root_id."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(
        text="t", topic_id="omt_topicA", thread_root_id="om_root_unused",
    )
    fake_result = RunResult(text="ok", session_id="sess-new", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    assert session.get("omt_topicA") is not None
    assert session.get("omt_topicA")["session_id"] == "sess-new"
    # thread_root_id NOT used as key
    assert session.get("om_root_unused") is None


@pytest.mark.asyncio
async def test_topic_first_touch_fetches_history_into_prompt(tmp_path):
    session.configure(tmp_path / "sess.json")
    history = [
        "ou_alice: 我们要做这个方案",
        "ou_bob: 那风险点呢",
    ]
    ch = _make_channel(history=history)
    parsed = _parsed(text="@bot 帮我分析", topic_id="omt_topicA")
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    ch.fetch_topic_history.assert_awaited_once()
    args, kwargs_call = ch.fetch_topic_history.call_args
    # topic_id passed positionally or kw
    assert "omt_topicA" in (list(args) + list(kwargs_call.values()))
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert "话题历史" in prompt
    assert "ou_alice: 我们要做这个方案" in prompt
    assert "ou_bob: 那风险点呢" in prompt
    assert "@bot 帮我分析" in prompt


@pytest.mark.asyncio
async def test_topic_resume_still_refetches_history(tmp_path):
    """Existing session under topic_id key → fetch_topic_history STILL fires
    so subsequent turns see fresh topic state. Claude --resume handles the
    redundancy with already-seen context. Behavior switched after observing
    the empty-@-mention bug: first turn could create a placeholder session
    that locks out history forever under a once-per-thread heuristic."""
    session.configure(tmp_path / "sess.json")
    session.put("omt_topicA", "sess-prev", agent="p")
    ch = _make_channel(history=["ou_alice: prior", "ou_bob: another"])
    parsed = _parsed(text="next turn", topic_id="omt_topicA")
    fake_result = RunResult(text="ok", session_id="sess-prev", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    ch.fetch_topic_history.assert_awaited_once()
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert "话题历史" in prompt
    assert "ou_alice: prior" in prompt
    assert "ou_bob: another" in prompt
    # --resume still rides the prior session for accumulated agent state.
    assert kwargs["session_id"] == "sess-prev"


@pytest.mark.asyncio
async def test_topic_history_limit_from_project_cfg(tmp_path):
    session.configure(tmp_path / "sess.json")
    ch = _make_channel(history=[])
    parsed = _parsed(text="t", topic_id="omt_topicA")
    cfg = _project_cfg(tmp_path, topic_history_limit=7)
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(ch, parsed, "p", cfg, _RUNTIME_CFG)
    args, kwargs_call = ch.fetch_topic_history.call_args
    # Either positional limit=7 or kw limit=7
    assert (kwargs_call.get("limit") == 7) or (7 in args)


@pytest.mark.asyncio
async def test_topic_history_prepends_thread_root_anchor(tmp_path):
    """Feishu's threads-messages-list omits the thread anchor (the
    message the thread was started from). For alarm-driven topics the
    anchor IS the alarm card. Scheduler must fetch it via
    channel.fetch_message_text and prepend so Claude sees the incident."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel(history=["ou_alice: 我们看下这个", "ou_bob: 嗯"])
    ch.fetch_message_text = AsyncMock(
        return_value="cli_alarm_bot: Aily商业化报警通知 错误描述：上报失败",
    )
    parsed = _parsed(
        text="@bot 分析下报警原因",
        topic_id="omt_topicA",
        thread_root_id="om_alarm",
        message_id="om_user_at",
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch(
        "agent_runtime.scheduler.claude_proc.run",
        AsyncMock(return_value=fake_result),
    ) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    ch.fetch_message_text.assert_awaited_once_with("om_alarm")
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    # Anchor must precede the threads-list rows so reading top-down
    # surfaces incident first, conversation second.
    anchor_pos = prompt.index("Aily商业化报警通知")
    alice_pos = prompt.index("ou_alice: 我们看下这个")
    assert anchor_pos < alice_pos
    assert "上报失败" in prompt


@pytest.mark.asyncio
async def test_topic_history_skips_anchor_when_root_is_self(tmp_path):
    """If thread_root_id == message_id (we ARE the thread root), do not
    fetch our own message — it would just duplicate the user's text."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel(history=[])
    parsed = _parsed(
        text="自己开的话题",
        topic_id="omt_topicA",
        thread_root_id="m-self",
        message_id="m-self",
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch(
        "agent_runtime.scheduler.claude_proc.run",
        AsyncMock(return_value=fake_result),
    ):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    ch.fetch_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_topic_history_skips_anchor_when_already_in_thread_list(tmp_path):
    """Defensive: if the threads-messages-list already returns a row
    matching root_id (some Feishu API behaviour quirks), don't double-add
    it. The substring check on history rows guards this cheaply."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel(history=["cli_alarm_bot: alarm body | id=om_alarm"])
    parsed = _parsed(
        text="t",
        topic_id="omt_topicA",
        thread_root_id="om_alarm",
        message_id="m-1",
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch(
        "agent_runtime.scheduler.claude_proc.run",
        AsyncMock(return_value=fake_result),
    ):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    ch.fetch_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_topic_id_uses_thread_root_id_as_session_key(tmp_path):
    """Backward compat: topic_id None → session keyed by thread_root_id."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(text="t", topic_id=None, thread_root_id="t-legacy")
    fake_result = RunResult(text="ok", session_id="sess-new", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    assert session.get("t-legacy") is not None


# ---------------------------------------------------------------------------
# Composition: image + topic together
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_topic_approval_remove_uses_topic_key(tmp_path):
    """Regression: write-phase cleanup must remove the approval keyed by
    topic_id (matching create()), not by thread_root_id — otherwise the
    approval entry leaks and the next topic message hits a stale state.
    """
    from agent_runtime import approval

    session.configure(tmp_path / "sess.json")
    # Pre-stage: an EXECUTING approval keyed under topic_id (mimicking the
    # state right after user replied "确认" and write phase started).
    info = approval.ApprovalInfo(
        operation="op", reason="r", impact="i", rollback="b",
    )
    appr = approval.Approval(
        thread_key="omt_topicA",
        agent_name="p",
        info=info,
        sender_id="ou-sender",
        admin_users=["ou-sender"],
        approval_timeout=1800,
    )
    # Inject directly into the in-memory approval store
    approval._pending["omt_topicA"] = appr

    parsed = _parsed(
        text="dummy",
        topic_id="omt_topicA",
        thread_root_id="om_root_unused",
    )
    fake_result = RunResult(text="done", session_id=None, exit_code=0)
    ch = _make_channel()
    cfg = _project_cfg(tmp_path)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)):
        await scheduler._execute_write_phase(ch, parsed, appr, cfg)
    # The approval must be gone from the topic-keyed slot
    assert approval.get("omt_topicA") is None
    # And NOT at the thread_root_id slot (would mean key drift)
    assert approval.get("om_root_unused") is None


@pytest.mark.asyncio
async def test_empty_text_no_images_no_topic_uses_placeholder(tmp_path):
    """User @ed bot with no other content (text='', no images, topic fetch
    returned empty / not in topic group): prompt MUST NOT be empty —
    claude --print rejects empty prompt with 'Input must be provided'.
    Substitute a placeholder that nudges the agent to greet + ask back.
    """
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(text="", topic_id=None, image_keys=[])
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert prompt.strip(), "empty prompt would crash claude --print"
    assert "@" in prompt or "没有" in prompt or "问候" in prompt


@pytest.mark.asyncio
async def test_empty_text_with_images_no_placeholder(tmp_path):
    """text='' + image attached: image header carries the directive
    ('用户附带 N 张图片，请用 Read 工具查看后再回答'), no placeholder needed."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel()
    parsed = _parsed(text="", image_keys=["img_a"], msg_type="image")
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    # Must have image header but NOT the placeholder text
    assert "用户附带 1 张图片" in prompt
    assert "没有发送具体内容" not in prompt


@pytest.mark.asyncio
async def test_empty_text_with_topic_history_no_placeholder(tmp_path):
    """text='' + topic history present: history is meaningful context;
    don't add the placeholder."""
    session.configure(tmp_path / "sess.json")
    ch = _make_channel(history=["ou_alice: 之前讨论过"])
    parsed = _parsed(text="", topic_id="omt_topic1")
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert "话题历史" in prompt
    assert "ou_alice: 之前讨论过" in prompt
    assert "没有发送具体内容" not in prompt


@pytest.mark.asyncio
async def test_image_and_topic_history_compose(tmp_path):
    """Both apply: prompt has 话题历史 outer, image header inner, user text last."""
    session.configure(tmp_path / "sess.json")
    history = ["ou_alice: 之前讨论过"]
    ch = _make_channel(history=history)
    parsed = _parsed(
        text="再看下这张图",
        msg_type="image",
        image_keys=["img_z"],
        topic_id="omt_topicA",
    )
    fake_result = RunResult(text="ok", session_id="s1", exit_code=0)
    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake_result)) as mock_run:
        await scheduler.handle_message(
            ch, parsed, "p", _project_cfg(tmp_path), _RUNTIME_CFG,
        )
    _, kwargs = mock_run.call_args
    prompt = kwargs["prompt"]
    assert "话题历史" in prompt
    assert "ou_alice: 之前讨论过" in prompt
    assert "用户附带 1 张图片" in prompt
    assert "img_z.png" in prompt
    assert "再看下这张图" in prompt
    # Topic history must appear before image header (outer wraps inner)
    assert prompt.index("话题历史") < prompt.index("用户附带")
