"""`/lesson <content>` slash command — Tier 1 self-improvement loop.

User feedback into a project's `knowledge/lessons.md` for the agent to
consult on next load (via the project's CLAUDE.md referencing the file).

Why intercepted in scheduler (not delegated to claude):
  - Zero claude API cost: lessons are bookkeeping, not reasoning
  - Deterministic: a `/lesson` command always lands in the file regardless
    of model availability
  - Auditable: file diff makes it obvious what was recorded

File format (markdown):

    # Lessons learned
    ...header...

    ## 2026-04-28
    - [12:34] 自我介绍 ≤ 3 句
    - [13:45] 不主动暴露 PSM / repo 路径

Same-day entries group under one `## DATE` section to keep the file
chronologically scannable without one section per entry.
"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.timezone import now_bjt

_PREFIX = "/lesson"

_HEADER = """\
# Lessons learned

每次 agent 答错时，王佳磊在飞书发 `/lesson <内容>` 追加到这里。
agent 启动时通过 spring_billing/CLAUDE.md 引用本文件作为指令。
积累足够多后人工 review，把模式提炼到 SOUL.md / EVERGREEN.md。
"""


def is_lesson_command(text: str) -> bool:
    """Return True if `text` (after leading whitespace) starts with `/lesson`
    followed by EOS, whitespace, or end-of-token. Prevents false positives
    on substrings like '我想用 /lesson 来记'."""
    if not text:
        return False
    stripped = text.lstrip()
    if stripped == _PREFIX:
        return True
    return stripped.startswith(_PREFIX + " ") or stripped.startswith(_PREFIX + "\t")


def parse_lesson(text: str) -> str | None:
    """Extract the lesson body. Returns None when body is empty.

    Internal newlines are collapsed to single spaces so the entry stays on
    one markdown list line — multi-line content would break the section
    rendering. Multi-line lessons should be split into multiple `/lesson`
    commands or curated into SOUL.md directly.
    """
    if not is_lesson_command(text):
        return None
    body = text.lstrip()[len(_PREFIX):].strip()
    if not body:
        return None
    # Collapse any internal whitespace runs (incl. newlines) to single space.
    return " ".join(body.split())


def append_lesson(work_dir: Path, content: str) -> Path:
    """Append a lesson to <work_dir>/knowledge/lessons.md and return the path.

    - Creates knowledge/ and lessons.md if missing (with header).
    - Groups under today's `## YYYY-MM-DD` section (one per day).
    - Each entry: `- [HH:MM] <content>`.
    """
    knowledge = work_dir / "knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    f = knowledge / "lessons.md"

    now = now_bjt()
    today = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    entry = f"- [{hhmm}] {content}\n"
    section_header = f"## {today}"

    if not f.exists():
        f.write_text(_HEADER + "\n" + section_header + "\n\n" + entry, encoding="utf-8")
        return f

    existing = f.read_text(encoding="utf-8")
    if section_header in existing:
        # Append entry inside the existing today-section (just above the next
        # `## ` section, or at end-of-file if today is the last section).
        lines = existing.splitlines(keepends=True)
        insert_at = len(lines)
        seen_today = False
        for i, line in enumerate(lines):
            if line.startswith(section_header):
                seen_today = True
                continue
            if seen_today and line.startswith("## "):
                insert_at = i
                break
        # Trim any trailing blank lines from the today-section so the entry
        # tucks tightly against the previous one.
        while insert_at > 0 and lines[insert_at - 1].strip() == "":
            insert_at -= 1
        lines.insert(insert_at, entry)
        f.write_text("".join(lines), encoding="utf-8")
    else:
        # Brand-new section for today — prepended above older sections so the
        # most recent day reads first when the file is opened.
        if not existing.endswith("\n"):
            existing += "\n"
        # Find first `## ` heading and insert today's section above it.
        idx = existing.find("\n## ")
        if idx == -1:
            # No existing date sections; append at end.
            f.write_text(
                existing + "\n" + section_header + "\n\n" + entry, encoding="utf-8"
            )
        else:
            # Keep header (everything up to and including the newline before
            # the first ## ), then today's section, then the rest.
            head = existing[: idx + 1]
            tail = existing[idx + 1 :]
            f.write_text(
                head + section_header + "\n\n" + entry + "\n" + tail,
                encoding="utf-8",
            )
    return f
