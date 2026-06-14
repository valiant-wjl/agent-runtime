"""Tests for runtime/lesson.py — Tier 1 `/lesson` slash command.

Spec:
  - User sends `/lesson <content>` in feishu thread
  - scheduler intercepts BEFORE running claude (zero claude API cost)
  - Appends `<content>` to <project_work_dir>/knowledge/lessons.md
  - Replies "已记下：<content>" (or usage hint when content empty)
  - Same-day entries grouped under one `## YYYY-MM-DD` section to avoid
    file bloat from one section per entry
"""

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import concurrency, lesson, scheduler, session
from agent_runtime.claude_proc import RunResult


# --- is_lesson_command ---


def test_is_lesson_command_recognizes_slash_lesson():
    assert lesson.is_lesson_command("/lesson 自我介绍 ≤ 3 句")


def test_is_lesson_command_recognizes_slash_lesson_with_no_content():
    """`/lesson` with no body is still the command (parse will reject)."""
    assert lesson.is_lesson_command("/lesson")


def test_is_lesson_command_rejects_normal_text():
    assert not lesson.is_lesson_command("普通问题，介绍下计费链路")


def test_is_lesson_command_rejects_lesson_substring():
    """Don't match if /lesson appears mid-sentence."""
    assert not lesson.is_lesson_command("我想用 /lesson 来记")


def test_is_lesson_command_strips_leading_whitespace():
    """Tolerate user typing space before /lesson."""
    assert lesson.is_lesson_command("  /lesson 短一点")


# --- parse_lesson ---


def test_parse_lesson_extracts_content():
    assert lesson.parse_lesson("/lesson 自我介绍 ≤ 3 句") == "自我介绍 ≤ 3 句"


def test_parse_lesson_strips_extra_whitespace():
    assert lesson.parse_lesson("/lesson    短一点  ") == "短一点"


def test_parse_lesson_empty_returns_none():
    assert lesson.parse_lesson("/lesson") is None
    assert lesson.parse_lesson("/lesson   ") is None


def test_parse_lesson_collapses_internal_newlines():
    """Lessons are one-line entries; collapse newlines so the markdown list
    item stays on one line and doesn't break the section formatting."""
    assert lesson.parse_lesson("/lesson 第一行\n第二行") == "第一行 第二行"


# --- append_lesson ---


def test_append_lesson_creates_file_when_missing(tmp_path: Path):
    work_dir = tmp_path / "example_project"
    work_dir.mkdir()

    lesson.append_lesson(work_dir, "自我介绍 ≤ 3 句")

    f = work_dir / "knowledge" / "lessons.md"
    assert f.is_file()
    content = f.read_text()
    today = date.today().isoformat()
    assert f"## {today}" in content
    assert "自我介绍 ≤ 3 句" in content
    # Header should explain what this file is.
    assert "Lessons" in content or "lessons" in content


def test_append_lesson_groups_same_day_entries(tmp_path: Path):
    """Two entries on same day → one `## DATE` section with two list items.

    Why: avoids file bloat from one section per entry; daily section is the
    smallest grain that's still useful for chronological review.
    """
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    lesson.append_lesson(work_dir, "lesson1")
    lesson.append_lesson(work_dir, "lesson2")

    content = (work_dir / "knowledge" / "lessons.md").read_text()
    today = date.today().isoformat()
    # Only one section header for today.
    assert content.count(f"## {today}") == 1
    assert "lesson1" in content
    assert "lesson2" in content


def test_append_lesson_preserves_existing_other_day_sections(tmp_path: Path):
    """A new entry today must not erase yesterday's section."""
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    knowledge = work_dir / "knowledge"
    knowledge.mkdir()
    existing = "# Lessons\n\n## 2026-04-01\n- old lesson\n"
    (knowledge / "lessons.md").write_text(existing)

    lesson.append_lesson(work_dir, "today lesson")

    content = (knowledge / "lessons.md").read_text()
    assert "## 2026-04-01" in content
    assert "old lesson" in content
    assert "today lesson" in content


def test_append_lesson_creates_knowledge_dir_if_missing(tmp_path: Path):
    """work_dir without a knowledge/ subdir should still work."""
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    # No knowledge/ subdir yet.

    lesson.append_lesson(work_dir, "x")

    assert (work_dir / "knowledge" / "lessons.md").is_file()


def test_append_lesson_includes_time_marker(tmp_path: Path):
    """Each entry should carry an HH:MM marker so we can review chronology
    within a busy day."""
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    lesson.append_lesson(work_dir, "x")

    content = (work_dir / "knowledge" / "lessons.md").read_text()
    # Match `[HH:MM]` somewhere on the entry line.
    import re
    assert re.search(r"\[\d{2}:\d{2}\]", content), \
        f"expected [HH:MM] marker; got:\n{content}"


# ---------------------------------------------------------------------------
# Scheduler integration: `/lesson` is intercepted before claude_proc.run
# ---------------------------------------------------------------------------


def _scheduler_parsed(text: str) -> ParsedMsg:
    return ParsedMsg(
        channel="feishu",
        message_id="m-lesson",
        thread_root_id="t-lesson",
        chat_id="c-lesson",
        sender_id="ou-sender",
        sender_name="u",
        text=text,
        mentions=[],
        raw_event={"event": {"message": {"message_type": "text"}}},
    )


def _scheduler_channel():
    ch = AsyncMock()
    ch.name = "feishu"
    ch.reply = AsyncMock(return_value=None)
    ch.send_card = AsyncMock(return_value="card-1")
    ch.update_card = AsyncMock(return_value=None)
    return ch


def _project_cfg(work_dir: Path) -> dict:
    return {
        "work_dir": str(work_dir),
        "display_name": "lbp-growth-agent",
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


@pytest.fixture(autouse=True)
def _scheduler_init():
    concurrency.init_global(10)
    yield


@pytest.mark.asyncio
async def test_scheduler_lesson_command_writes_file_and_replies(tmp_path):
    """/lesson <content> → write file, reply '已记下', do NOT invoke claude."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "example_project"
    work_dir.mkdir()
    ch = _scheduler_channel()
    parsed = _scheduler_parsed("/lesson 自我介绍 ≤ 3 句")

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "example_project", _project_cfg(work_dir), _RUNTIME_CFG
        )

    # Must NOT have invoked claude (zero-cost feedback path).
    run_mock.assert_not_called()
    # File written.
    f = work_dir / "knowledge" / "lessons.md"
    assert f.is_file()
    assert "自我介绍 ≤ 3 句" in f.read_text()
    # User-facing acknowledgement reply.
    ch.reply.assert_called_once()
    reply_text = ch.reply.call_args[0][1]
    assert "已记下" in reply_text or "记下" in reply_text


@pytest.mark.asyncio
async def test_scheduler_lesson_command_empty_replies_usage(tmp_path):
    """/lesson with no body → reply usage hint, no file write, no claude."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _scheduler_channel()
    parsed = _scheduler_parsed("/lesson")

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock()) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "example_project", _project_cfg(work_dir), _RUNTIME_CFG
        )

    run_mock.assert_not_called()
    assert not (work_dir / "knowledge" / "lessons.md").exists()
    ch.reply.assert_called_once()
    reply_text = ch.reply.call_args[0][1]
    assert "用法" in reply_text or "usage" in reply_text.lower() or "/lesson" in reply_text


@pytest.mark.asyncio
async def test_scheduler_normal_message_skips_lesson_handler(tmp_path):
    """Normal text without /lesson prefix → claude_proc.run IS invoked."""
    session.configure(tmp_path / "sess.json")
    work_dir = tmp_path / "p"
    work_dir.mkdir()
    ch = _scheduler_channel()
    parsed = _scheduler_parsed("介绍下计费链路")
    fake = RunResult(text="answer", session_id="s-1", exit_code=0)

    with patch("agent_runtime.scheduler.claude_proc.run", AsyncMock(return_value=fake)) as run_mock:
        await scheduler.handle_message(
            ch, parsed, "example_project", _project_cfg(work_dir), _RUNTIME_CFG
        )

    run_mock.assert_called_once()
    # And the lessons file should NOT have been touched.
    assert not (work_dir / "knowledge" / "lessons.md").exists()
