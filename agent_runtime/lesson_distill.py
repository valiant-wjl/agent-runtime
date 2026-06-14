"""Periodic distillation of accumulated knowledge/lessons.md into meta/SOUL.md.

The ``/lesson`` command appends user corrections to a project's
``knowledge/lessons.md`` — the highest-priority per-turn override. Left
unbounded that file grows forever and accumulates rules that really belong in
the curated persona contract (``SOUL.md`` in ``meta_work_dir``). This module
periodically folds the accumulated lessons INTO SOUL.md via an LLM rewrite,
then resets lessons.md to its header — so the persona stays the single curated
source and lessons.md stays short.

Guardrails (autonomous SOUL rewrite is sensitive):
  - The LLM call is **read-only** (no Edit/Write tools) and merely returns the
    full updated SOUL text; this module does the file write deterministically.
  - The new SOUL is **validated** (persona header present, not truncated)
    before it touches disk.
  - The old SOUL is **backed up** (``SOUL.md.bak.<ts>``) before overwrite.
  - lessons.md is reset **only after** SOUL is successfully written; on any
    failure (invalid output / LLM error) lessons are preserved for retry.
  - The user is notified (push_to_self) of every fold / skip.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from agent_runtime import claude_proc
from agent_runtime.lesson import _HEADER as _LESSON_HEADER
from agent_runtime.push import push_to_self
from agent_runtime.timezone import now_bjt

log = logging.getLogger(__name__)

# A lesson entry line, as written by lesson.append_lesson: ``- [HH:MM] ...``.
_ENTRY_RE = re.compile(r"^- \[\d{2}:\d{2}\]", re.MULTILINE)
# Full entry line (used to diff snapshot vs current for in-flight preservation).
_ENTRY_LINE_RE = re.compile(r"^- \[\d{2}:\d{2}\].*$", re.MULTILINE)
# Persona header that every valid SOUL.md must retain.
_SOUL_MARKER = "# 人格"
# Markdown section headings (## / ###) — used to assert the distiller did not
# silently delete a whole section while "merging".
_HEADING_RE = re.compile(r"^#{2,3}\s+.*$", re.MULTILINE)
# Reject distilled output shorter than this fraction of the original — a
# strong signal the LLM truncated or destroyed the file rather than folding.
_MIN_LENGTH_RATIO = 0.6


def count_lessons(lessons_text: str) -> int:
    """Number of ``- [HH:MM]`` entry lines (i.e. distillable lessons)."""
    if not lessons_text:
        return 0
    return len(_ENTRY_RE.findall(lessons_text))


def build_distill_prompt(soul_text: str, lessons_text: str) -> str:
    """Prompt the model to fold lessons into SOUL and return the full new SOUL.

    The model is told to output ONLY the updated SOUL.md markdown — no fences,
    no commentary — because the caller writes that text straight to disk.
    """
    return (
        "你是一个维护「数字人人格文件」的助手。下面是当前的 SOUL.md（人格契约）"
        "和一批通过 /lesson 累积的纠偏条目。请把这些 lesson **提炼并合并进 "
        "SOUL.md 的恰当章节**（说话风格 / 输出契约·反模式 / 价值观），要求：\n"
        "1. 去重：与 SOUL 中已有规则重复的 lesson 不要再加，已覆盖即视为完成；\n"
        "2. 归类：每条新规则放进语义最贴合的现有章节，不要新建无关章节；\n"
        "3. 保结构：保留 SOUL 原有的标题层级、章节顺序与整体风格，只做增量融合；\n"
        "4. 精炼：合并同类项，用一句话表达一条规则，不堆砌。\n\n"
        "**只输出融合后的完整 SOUL.md 全文**（Markdown），不要加任何解释、"
        "不要用代码块包裹、不要写「以下是」之类的话。\n\n"
        "===== 当前 SOUL.md =====\n"
        f"{soul_text}\n\n"
        "===== 待提炼的 lessons =====\n"
        f"{lessons_text}\n"
    )


def _strip_code_fence(text: str) -> str:
    """Remove a wrapping ```/```markdown fence if the model added one."""
    t = text.strip()
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    # drop first fence line (``` or ```markdown)
    lines = lines[1:]
    # drop trailing fence line if present
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def validate_distilled_soul(new_text: str, old_text: str) -> bool:
    """Guard against an LLM that returned garbage / a truncated SOUL.

    Rejects output that is empty, missing the persona header, suspiciously
    short, or that dropped a top-level section present in the original
    (folding should preserve structure, not delete 章节).
    """
    if not new_text or not new_text.strip():
        return False
    if _SOUL_MARKER not in new_text:
        return False
    if len(new_text.strip()) < len(old_text.strip()) * _MIN_LENGTH_RATIO:
        return False
    # Every ## / ### heading in the old SOUL must survive in the new one.
    old_headings = {h.strip() for h in _HEADING_RE.findall(old_text)}
    new_headings = {h.strip() for h in _HEADING_RE.findall(new_text)}
    if not old_headings.issubset(new_headings):
        return False
    return True


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _finalize_lessons(path: Path, distilled_snapshot: str) -> None:
    """Reset lessons.md, preserving any entries added DURING the distill call.

    The LLM ran against ``distilled_snapshot`` (the lessons text read before the
    call). A user may have fired ``/lesson`` while the call was in flight; those
    entries are NOT in SOUL yet, so clearing them would lose data. We re-read the
    current file, keep only entries absent from the snapshot, and back up the
    full pre-reset file for recoverability.
    """
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = distilled_snapshot
    # Safety net: keep a copy of the exact file we are about to truncate.
    ts_full = now_bjt().strftime("%Y%m%d-%H%M%S")
    try:
        path.with_name(f"{path.name}.bak.{ts_full}").write_text(current, encoding="utf-8")
    except OSError as e:
        log.warning("lesson_distill: lessons backup failed: %s", e)

    distilled_entries = set(_ENTRY_LINE_RE.findall(distilled_snapshot))
    residual = [
        line for line in _ENTRY_LINE_RE.findall(current)
        if line not in distilled_entries
    ]

    ts = now_bjt().strftime("%Y-%m-%d %H:%M")
    marker = (
        f"\n> {ts}：累积的 lessons 已自动提炼进 meta/SOUL.md，本文件重置。"
        "后续新 lesson 从这里继续累积。\n"
    )
    body = _LESSON_HEADER + marker
    if residual:
        # Entries that arrived mid-distill — keep them for the next cycle.
        section = now_bjt().strftime("## %Y-%m-%d")
        body += "\n" + section + "\n\n" + "\n".join(residual) + "\n"
    _atomic_write(path, body)


async def distill_once(
    *,
    project_name: str,
    project_cfg: dict,
    meta_work_dir: str | None,
    run_fn=claude_proc.run,
    notify_fn=push_to_self,
    model: str | None = None,
    timeout: int = 300,
) -> dict:
    """Fold one project's accumulated lessons into SOUL.md.

    Returns a status dict: ``{"status": "noop"|"ok"|"invalid"|"error", ...}``.
    Never raises for expected failure modes — a bad cycle must not crash the
    loop. lessons.md is reset ONLY on ``ok``.
    """
    if not meta_work_dir:
        return {"status": "noop", "reason": "no meta_work_dir"}
    work_dir = project_cfg.get("work_dir")
    if not work_dir:
        return {"status": "noop", "reason": "no work_dir"}

    soul_path = Path(meta_work_dir) / "SOUL.md"
    lessons_path = Path(work_dir) / "knowledge" / "lessons.md"
    if not soul_path.exists() or not lessons_path.exists():
        return {"status": "noop", "reason": "missing SOUL or lessons"}

    soul_text = soul_path.read_text(encoding="utf-8")
    lessons_text = lessons_path.read_text(encoding="utf-8")
    n = count_lessons(lessons_text)
    if n == 0:
        return {"status": "noop", "reason": "no lessons"}

    prompt = build_distill_prompt(soul_text, lessons_text)
    try:
        # Read-only LLM call: it returns the full new SOUL as text; we write it
        # ourselves. meta_work_dir=None so the distiller does NOT re-inject the
        # persona as a system prompt (SOUL is already in the prompt body).
        result = await run_fn(
            work_dir=meta_work_dir,
            prompt=prompt,
            timeout=timeout,
            session_id=None,
            disallowed_tools=["Edit", "Write", "NotebookEdit"],
            model=model,
            meta_work_dir=None,
        )
    except Exception as e:  # noqa: BLE001 — loop must survive any run failure
        log.warning("lesson_distill: run failed for %s: %s", project_name, e)
        await _safe_notify(
            notify_fn,
            f"⚠️ lessons 提炼失败（{project_name}）：LLM 调用异常，lessons 已保留待重试。",
        )
        return {"status": "error", "reason": str(e)}

    if getattr(result, "timed_out", False) or getattr(result, "exit_code", 0) != 0:
        log.warning("lesson_distill: run errored for %s (exit/timeout)", project_name)
        await _safe_notify(
            notify_fn,
            f"⚠️ lessons 提炼失败（{project_name}）：LLM 超时/非零退出，lessons 已保留。",
        )
        return {"status": "error", "reason": "llm exit/timeout"}

    new_soul = _strip_code_fence(getattr(result, "text", "") or "")
    if not validate_distilled_soul(new_soul, soul_text):
        log.warning("lesson_distill: invalid distilled SOUL for %s; skipping", project_name)
        await _safe_notify(
            notify_fn,
            f"⚠️ lessons 提炼跳过（{project_name}）：输出未通过校验，SOUL 未改、lessons 已保留。",
        )
        return {"status": "invalid", "reason": "failed validation"}

    # Backup old SOUL, write new, then reset lessons (order matters: lessons
    # are only cleared after SOUL is safely on disk).
    ts = now_bjt().strftime("%Y%m%d-%H%M%S")
    backup = soul_path.with_name(f"SOUL.md.bak.{ts}")
    try:
        backup.write_text(soul_text, encoding="utf-8")
        _atomic_write(soul_path, new_soul if new_soul.endswith("\n") else new_soul + "\n")
        # Pass the snapshot the LLM actually saw so entries added mid-call are
        # preserved (not silently destroyed).
        _finalize_lessons(lessons_path, lessons_text)
    except OSError as e:
        log.error("lesson_distill: write failed for %s: %s", project_name, e)
        await _safe_notify(
            notify_fn,
            f"⚠️ lessons 提炼写盘失败（{project_name}）：{e}",
        )
        return {"status": "error", "reason": str(e)}

    log.info("lesson_distill: folded %d lessons into SOUL for %s", n, project_name)
    await _safe_notify(
        notify_fn,
        f"✅ 已把 {n} 条 lessons 提炼进 SOUL（{project_name}）。"
        f"旧版备份：{backup.name}。如不满意可回滚。",
    )
    return {"status": "ok", "folded": n, "backup": backup.name}


async def _safe_notify(notify_fn, text: str) -> None:
    """Best-effort notify; a notify failure must not break distillation."""
    try:
        await notify_fn(text)
    except Exception as e:  # noqa: BLE001
        log.debug("lesson_distill: notify failed: %s", e)


async def distill_loop(
    projects: dict,
    meta_work_dir: str | None,
    *,
    interval_seconds: int = 86400,
    min_lessons: int = 3,
    max_per_cycle: int = 5,
    model: str | None = None,
    timeout: int = 300,
    run_fn=claude_proc.run,
    notify_fn=push_to_self,
    sleep_fn=asyncio.sleep,
) -> None:
    """Background loop: every ``interval_seconds`` distill each project whose
    lessons.md has accumulated at least ``min_lessons`` entries.

    Bounded: at most ``max_per_cycle`` projects are distilled per wake, and each
    distill is wrapped in an ``asyncio.wait_for`` backstop (``timeout`` + slack)
    so a hung LLM call cannot stall the loop indefinitely. All per-project errors
    are logged + swallowed so one bad project can't take down the loop.
    CancelledError propagates for clean shutdown.
    """
    while True:
        distilled = 0
        for name, cfg in (projects or {}).items():
            if distilled >= max_per_cycle:
                log.info("lesson_distill: hit max_per_cycle=%d; deferring rest", max_per_cycle)
                break
            try:
                work_dir = cfg.get("work_dir")
                if not work_dir:
                    continue
                lessons_path = Path(work_dir) / "knowledge" / "lessons.md"
                if not lessons_path.exists():
                    continue
                if count_lessons(lessons_path.read_text(encoding="utf-8")) < min_lessons:
                    continue
                res = await asyncio.wait_for(
                    distill_once(
                        project_name=name,
                        project_cfg=cfg,
                        meta_work_dir=cfg.get("meta_work_dir") or meta_work_dir,
                        run_fn=run_fn,
                        notify_fn=notify_fn,
                        model=model,
                        timeout=timeout,
                    ),
                    timeout=timeout + 60,
                )
                if res.get("status") == "ok":
                    distilled += 1
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("lesson_distill: distill_once timed out for %s", name)
            except Exception:
                log.exception("lesson_distill: loop iteration failed for %s", name)
        try:
            await sleep_fn(interval_seconds)
        except asyncio.CancelledError:
            raise
