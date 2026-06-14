"""Tests for runtime/lesson_distill.py — periodic lessons.md → SOUL.md fold.

The /lesson command appends corrections to a project's knowledge/lessons.md
(highest-priority per-turn override). This module periodically folds them into
the curated persona (meta/SOUL.md) via an LLM rewrite, then resets lessons.md.
Guardrails under test: noop when empty, atomic write + backup on success,
preserve lessons.md when the LLM output is invalid or the call fails.
"""

from dataclasses import dataclass

import pytest

from agent_runtime import lesson_distill


@dataclass
class _FakeResult:
    text: str
    exit_code: int = 0
    timed_out: bool = False


_SOUL = """# 人格 · lbp-growth-agent

## 2. 说话风格
- 结论先行。

## 3. 输出契约
### 反模式
- 不复述排查过程。
"""

_LESSONS_WITH_ENTRIES = """# Lessons learned

一些说明文字。

## 2026-06-01
- [13:00] 回答额度问题先报结论再给链路
- [13:05] 不要贴大段代码
"""

_LESSONS_EMPTY = """# Lessons learned

一些说明文字。本文件目前没有任何 lesson 条目。
"""


# --- pure helpers ---------------------------------------------------------

def test_count_lessons_counts_only_entry_lines():
    assert lesson_distill.count_lessons(_LESSONS_WITH_ENTRIES) == 2
    assert lesson_distill.count_lessons(_LESSONS_EMPTY) == 0
    assert lesson_distill.count_lessons("") == 0


def test_build_distill_prompt_includes_soul_lessons_and_directive():
    p = lesson_distill.build_distill_prompt(_SOUL, _LESSONS_WITH_ENTRIES)
    assert "lbp-growth-agent" in p          # current SOUL embedded
    assert "先报结论再给链路" in p           # lessons embedded
    # must instruct: output ONLY the full updated SOUL markdown, no commentary
    assert "只输出" in p or "完整" in p


def test_strip_code_fence_unwraps_markdown_block():
    fenced = "```markdown\n# 人格\nbody\n```"
    assert lesson_distill._strip_code_fence(fenced) == "# 人格\nbody"
    plain = "# 人格\nbody"
    assert lesson_distill._strip_code_fence(plain) == "# 人格\nbody"


def test_validate_distilled_soul():
    # good: has persona header, comparable length, all sections survive
    assert lesson_distill.validate_distilled_soul(_SOUL + "\n- 新增", _SOUL)
    # bad: empty
    assert not lesson_distill.validate_distilled_soul("", _SOUL)
    # bad: missing persona header
    assert not lesson_distill.validate_distilled_soul("# 随便\nfoo", _SOUL)
    # bad: suspiciously short (truncation / destruction guard)
    assert not lesson_distill.validate_distilled_soul("# 人格\n短", _SOUL)


def test_validate_rejects_dropped_section():
    # new text keeps the persona header + enough length but silently drops the
    # '## 3. 输出契约' / '### 反模式' sections — must be rejected.
    truncated = (
        "# 人格 · lbp-growth-agent\n\n## 2. 说话风格\n- 结论先行。\n"
        "（这里塞一些填充文字让长度过线" + "凑字数" * 20 + "）\n"
    )
    assert not lesson_distill.validate_distilled_soul(truncated, _SOUL)


# --- distill_once ---------------------------------------------------------

def _setup_dirs(tmp_path, soul=_SOUL, lessons=_LESSONS_WITH_ENTRIES):
    meta = tmp_path / "meta"
    work = tmp_path / "example_project"
    (meta).mkdir()
    (work / "knowledge").mkdir(parents=True)
    (meta / "SOUL.md").write_text(soul, encoding="utf-8")
    (work / "knowledge" / "lessons.md").write_text(lessons, encoding="utf-8")
    return meta, work


@pytest.mark.asyncio
async def test_distill_once_noop_when_no_lessons(tmp_path):
    meta, work = _setup_dirs(tmp_path, lessons=_LESSONS_EMPTY)
    called = {"run": 0}

    async def run_fn(**kw):
        called["run"] += 1
        return _FakeResult(text="should not be called")

    async def notify_fn(text, **kw):
        return True

    res = await lesson_distill.distill_once(
        project_name="example_project",
        project_cfg={"work_dir": str(work)},
        meta_work_dir=str(meta),
        run_fn=run_fn,
        notify_fn=notify_fn,
    )
    assert res["status"] == "noop"
    assert called["run"] == 0
    # lessons untouched
    assert "没有任何 lesson" in (work / "knowledge" / "lessons.md").read_text()


@pytest.mark.asyncio
async def test_distill_once_happy_path_writes_soul_backs_up_and_resets(tmp_path):
    meta, work = _setup_dirs(tmp_path)
    new_soul = _SOUL + "\n- [folded] 答额度先结论再链路；少贴代码。\n"
    notes = []

    async def run_fn(**kw):
        # LLM is read-only; it returns the full updated SOUL as text
        assert kw.get("meta_work_dir") is None  # distill must NOT re-inject persona
        return _FakeResult(text=new_soul)

    async def notify_fn(text, **kw):
        notes.append(text)
        return True

    res = await lesson_distill.distill_once(
        project_name="example_project",
        project_cfg={"work_dir": str(work)},
        meta_work_dir=str(meta),
        run_fn=run_fn,
        notify_fn=notify_fn,
    )

    assert res["status"] == "ok"
    assert res["folded"] == 2
    # SOUL updated to the distilled content
    assert (meta / "SOUL.md").read_text() == new_soul
    # a timestamped backup of the OLD soul exists
    backups = list(meta.glob("SOUL.md.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == _SOUL
    # lessons.md reset (no more entry lines)
    assert lesson_distill.count_lessons(
        (work / "knowledge" / "lessons.md").read_text()
    ) == 0
    # user notified
    assert notes and "example_project" in notes[0]


@pytest.mark.asyncio
async def test_distill_once_preserves_lessons_added_during_call(tmp_path):
    """A /lesson fired WHILE the distill LLM runs must survive the reset."""
    meta, work = _setup_dirs(tmp_path)
    lessons_path = work / "knowledge" / "lessons.md"
    new_soul = _SOUL + "\n- [folded] 已折叠两条。\n"

    async def run_fn(**kw):
        # Simulate a user firing /lesson mid-distill: append a fresh entry.
        with lessons_path.open("a", encoding="utf-8") as f:
            f.write("- [23:59] 排查时先报结论\n")
        return _FakeResult(text=new_soul)

    async def notify_fn(text, **kw):
        return True

    res = await lesson_distill.distill_once(
        project_name="example_project",
        project_cfg={"work_dir": str(work)},
        meta_work_dir=str(meta),
        run_fn=run_fn,
        notify_fn=notify_fn,
    )

    assert res["status"] == "ok"
    after = lessons_path.read_text()
    # the 2 distilled entries are gone, the in-flight one survives
    assert lesson_distill.count_lessons(after) == 1
    assert "23:59" in after
    # full pre-reset lessons backed up for recoverability
    assert list((work / "knowledge").glob("lessons.md.bak.*"))


@pytest.mark.asyncio
async def test_distill_once_invalid_output_preserves_lessons(tmp_path):
    meta, work = _setup_dirs(tmp_path)

    async def run_fn(**kw):
        return _FakeResult(text="oops totally broke it")  # no persona header

    notes = []

    async def notify_fn(text, **kw):
        notes.append(text)
        return True

    res = await lesson_distill.distill_once(
        project_name="example_project",
        project_cfg={"work_dir": str(work)},
        meta_work_dir=str(meta),
        run_fn=run_fn,
        notify_fn=notify_fn,
    )

    assert res["status"] == "invalid"
    # SOUL untouched, lessons preserved (will retry next cycle)
    assert (meta / "SOUL.md").read_text() == _SOUL
    assert lesson_distill.count_lessons(
        (work / "knowledge" / "lessons.md").read_text()
    ) == 2
    assert not list(meta.glob("SOUL.md.bak.*"))


@pytest.mark.asyncio
async def test_distill_once_llm_error_preserves_lessons(tmp_path):
    meta, work = _setup_dirs(tmp_path)

    async def run_fn(**kw):
        return _FakeResult(text="", exit_code=1, timed_out=False)

    async def notify_fn(text, **kw):
        return True

    res = await lesson_distill.distill_once(
        project_name="example_project",
        project_cfg={"work_dir": str(work)},
        meta_work_dir=str(meta),
        run_fn=run_fn,
        notify_fn=notify_fn,
    )
    assert res["status"] in ("error", "invalid")
    assert (meta / "SOUL.md").read_text() == _SOUL
    assert lesson_distill.count_lessons(
        (work / "knowledge" / "lessons.md").read_text()
    ) == 2
