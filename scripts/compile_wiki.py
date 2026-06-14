"""Orchestrator for the /compile-wiki slash command.

Wraps precheck + claude invocation + summary parse + rollback into a single
Python entry point so the flow can be integration-tested with mocks.

The slash command itself runs inside Claude Code interactively. This module
exists primarily so the same flow can be exercised programmatically (e.g. by
the scheduler or by tests) with subprocess + claude_proc.run mocked out.
"""
from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow `from runtime.claude_proc import run as claude_run` from this script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.claude_proc import run as claude_run  # noqa: E402
from runtime.claude_proc import RunResult  # noqa: E402

log = logging.getLogger(__name__)

PRECHECK_SCRIPT = Path(__file__).resolve().parent / "compile-wiki-precheck.sh"

SUMMARY_HEADER = "compile-wiki summary"


@dataclass
class CompileResult:
    ok: bool
    update_count: int = 0
    create_count: int = 0
    create_adr_count: int = 0
    skip_count: int = 0
    user_locked_count: int = 0
    verify_conflicts: list[str] = field(default_factory=list)
    summary_text: str = ""
    error: str | None = None
    rolled_back: bool = False
    timed_out: bool = False
    dry_run: bool = False


def parse_summary(text: str) -> CompileResult:
    """Parse the summary block written by the LLM.

    Recognized lines (case-sensitive on the label, value is non-negative int):
        UPDATE: <int>
        CREATE: <int>
        CREATE ADR: <int>
        SKIP: <int>
        user-locked: <int>

    Conflict block:
        ⚠️ VERIFY (...):
          - <conflict line 1>
          - <conflict line 2>
    Conflict collection ends at the first non-blank, non-"- " line.
    """
    res = CompileResult(ok=True, summary_text=text)
    if SUMMARY_HEADER not in text:
        res.ok = False
        res.error = "summary header missing"
        return res

    # Regex anchor on the digit boundary (\b or end-of-string) ensures
    # "CREATE ADR: 1" cannot be matched by the "CREATE" label — correctness
    # comes from the boundary, not the iteration order.
    counts = [
        ("UPDATE", "update_count"),
        ("CREATE ADR", "create_adr_count"),
        ("SKIP", "skip_count"),
        ("user-locked", "user_locked_count"),
        ("CREATE", "create_count"),
    ]
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        for label, attr in counts:
            m = re.match(rf"^{re.escape(label)}:\s*(\d+)(?:\b|$)", stripped)
            if m:
                setattr(res, attr, int(m.group(1)))
                break

    # Collect ⚠️ VERIFY conflict lines (continuation lines starting with "- ").
    in_verify = False
    for line in lines:
        if "⚠️ VERIFY" in line:
            in_verify = True
            continue
        if in_verify:
            stripped = line.strip()
            if stripped.startswith("- "):
                res.verify_conflicts.append(stripped[2:])
            elif not stripped:
                continue
            else:
                in_verify = False

    return res


def run_precheck(
    meta_dir: Path,
    override_git: bool = False,
    runner=subprocess.run,
) -> tuple[int, str]:
    """Run compile-wiki-precheck.sh. Returns (exit_code, stderr)."""
    args = ["bash", str(PRECHECK_SCRIPT), str(meta_dir)]
    if override_git:
        args.append("--override-git-check")
    p = runner(args, capture_output=True, text=True)
    return p.returncode, (p.stderr or "")


def git_rollback(meta_dir: Path, runner=subprocess.run) -> tuple[bool, str]:
    """Run ``git reset --hard HEAD`` in meta_dir.

    Returns ``(success, stderr)`` so callers can surface the underlying
    failure (e.g. detached HEAD, corrupt index) instead of swallowing it.
    """
    p = runner(
        ["git", "reset", "--hard", "HEAD"],
        cwd=str(meta_dir),
        capture_output=True,
        text=True,
    )
    return p.returncode == 0, (p.stderr or "")


async def compile_wiki(
    meta_dir: Path,
    *,
    dry_run: bool = False,
    override_git_check: bool = False,
    timeout_s: int = 300,
    _claude_run=claude_run,           # injected for tests
    _precheck_runner=subprocess.run,  # injected for tests
    _git_runner=subprocess.run,       # injected for tests
) -> CompileResult:
    """Orchestrate one /compile-wiki invocation.

    Steps:
      1. Run precheck script. Non-zero -> return ok=False (claude NOT called).
      2. Build slash-command prompt with optional flags.
      3. Call claude_proc.run(prompt=..., work_dir=meta_dir, ...).
         Crash / non-zero exit / timeout -> roll back (unless dry_run).
      4. Parse summary text. Header missing -> roll back (unless dry_run).
      5. Return parsed CompileResult with dry_run flag mirrored.
    """
    # Step 1: precheck (always runs, even in dry_run).
    ec, err = run_precheck(
        meta_dir, override_git=override_git_check, runner=_precheck_runner
    )
    if ec != 0:
        return CompileResult(
            ok=False,
            error=f"precheck: {err.strip()}",
            dry_run=dry_run,
        )

    # Step 2: build prompt.
    flags: list[str] = []
    if dry_run:
        flags.append("--dry-run")
    if override_git_check:
        flags.append("--override-git-check")
    prompt = "/compile-wiki" + (("" if not flags else " " + " ".join(flags)))

    # Step 3: invoke claude.
    try:
        result: RunResult = await _claude_run(
            work_dir=str(meta_dir),
            prompt=prompt,
            timeout=timeout_s,
        )
    except Exception as e:
        err_msg = f"claude crashed: {e!r}"
        if not dry_run:
            rolled, rb_err = git_rollback(meta_dir, runner=_git_runner)
            if not rolled:
                err_msg = f"{err_msg}; rollback FAILED: {rb_err.strip()}"
            return CompileResult(
                ok=False,
                error=err_msg,
                rolled_back=rolled,
                dry_run=dry_run,
            )
        return CompileResult(
            ok=False,
            error=err_msg,
            dry_run=dry_run,
        )

    if result.exit_code != 0 or result.timed_out:
        # Distinct error message + dedicated discriminator field.
        if result.timed_out:
            err_msg = "claude timeout"
        else:
            err_msg = f"claude exit={result.exit_code}"
        if not dry_run:
            rolled, rb_err = git_rollback(meta_dir, runner=_git_runner)
            if not rolled:
                err_msg = f"{err_msg}; rollback FAILED: {rb_err.strip()}"
            return CompileResult(
                ok=False,
                error=err_msg,
                rolled_back=rolled,
                timed_out=result.timed_out,
                summary_text=result.text,
                dry_run=dry_run,
            )
        return CompileResult(
            ok=False,
            error=err_msg,
            timed_out=result.timed_out,
            summary_text=result.text,
            dry_run=dry_run,
        )

    # Step 4: parse summary.
    parsed = parse_summary(result.text)
    parsed.dry_run = dry_run
    if not parsed.ok and not dry_run:
        # Corrupt summary -> roll back so we don't leave half-written wiki.
        rolled, rb_err = git_rollback(meta_dir, runner=_git_runner)
        parsed.rolled_back = rolled
        if not rolled and parsed.error is not None:
            parsed.error = f"{parsed.error}; rollback FAILED: {rb_err.strip()}"
    return parsed
