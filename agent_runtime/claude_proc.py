"""Claude CLI subprocess orchestration.

Two entry points:
- `run()`: one-shot synchronous completion. Scheduler uses this for
  normal reads/writes (M2-T13). Returns RunResult after process finishes.
- `run_stream()`: async generator yielding stream-json events. M6 Stream
  Card uses this to update progress in real time.

Stream mode caller contract:
    async for event in run_stream(...):
        ...
        if need_stop:
            break       # finally will terminate subprocess
Do NOT hold a reference to the generator past the async-for loop
(otherwise subprocess may leak until GC).

G3 bash restrictions are implemented via --append-system-prompt (LLM-guided
soft block), not kernel filtering. Pair with disallowed_tools for hardening.

Requires Python 3.11+ (uses asyncio.timeout in run_stream).
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)

# Persona files read from the shared meta dir and injected as the system
# prompt. Order = injection order (SOUL = who I am / how I sound; USER =
# owner prefs + 禁区). These live in meta_work_dir, a SIBLING of the bot's
# cwd (the project work_dir), so Claude Code never auto-loads them — without
# this injection the curated persona/style contract is dead code.
_PERSONA_FILES = ("SOUL.md", "USER.md")
# Cap injected persona size so a runaway SOUL/USER can't blow the system
# prompt (and token budget). 32KB is generous for hand-authored persona docs.
_PERSONA_MAX_BYTES = 32 * 1024

# fleet-mcp.json ships alongside this module. Resolve it relative to
# __file__ so the path is valid on every host. It was previously hardcoded
# to a Linux deploy path (/home/example-user/...), which on any other machine
# made claude exit with "Invalid MCP configuration: file not found" before
# emitting a single stream event — the user just saw "(no answer)".
_FLEET_MCP_CONFIG = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fleet-mcp.json"
)


# Surfaced to the user when claude CLI exits 1 with no stdout/stderr — the
# observed shape when the OAuth access token has expired (token TTL is ~24h;
# CLI does not auto-refresh from non-interactive subprocess invocations).
# Old behavior was "⚠️ Claude 错误: " (empty err) or scheduler fallback
# "(no answer)" — both opaque. This message tells the operator exactly
# what to do.
AUTH_FAILED_TEXT = (
    "⚠️ Claude CLI 未登录或 token 过期，请在开发机执行 `claude /login` 重新登录"
)


def _looks_like_silent_auth_failure(
    returncode: int | None, stdout: bytes, stderr: bytes
) -> bool:
    """Detect the "OAuth token expired" exit pattern.

    Empirically observed in runtime.log: claude CLI exits 1 with both
    stdout and stderr empty when the token in ~/.claude/.credentials.json
    is past `expiresAt`. We treat exit≠0 + both streams empty as auth-failed.
    Any error with stderr content (e.g. flag errors) falls through to the
    normal error path so its message is preserved.
    """
    return (
        returncode is not None
        and returncode != 0
        and not stdout.strip()
        and not stderr.strip()
    )


@dataclass
class RunResult:
    text: str
    session_id: str | None
    exit_code: int
    timed_out: bool = False
    num_turns: int = 0
    # Token accounting (GenAI semantic conventions). Populated on the
    # buffered path from the `usage` and `modelUsage` fields of the
    # `claude --print --output-format=json` envelope. Stream path has
    # equivalent emit inline in scheduler (reads raw final event).
    # 0 / None when unavailable (e.g. on auth-failed shape).
    usage_input_tokens: int = 0
    usage_output_tokens: int = 0
    model: str | None = None


def _load_persona(meta_work_dir: str | None) -> str:
    """Read SOUL.md + USER.md from ``meta_work_dir`` into one prompt block.

    Returns "" when meta_work_dir is falsy or no persona file is present.
    Best-effort: a missing/unreadable file is skipped (logged) rather than
    raised — a persona-read failure must never kill a user's turn. Each file
    is wrapped with a header so the model can tell SOUL from USER.
    """
    if not meta_work_dir:
        return ""
    parts: list[str] = []
    for name in _PERSONA_FILES:
        p = Path(meta_work_dir) / name
        try:
            text = p.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            continue
        except OSError as e:
            log.warning("persona: failed to read %s: %s", p, e)
            continue
        if not text:
            continue
        if len(text.encode("utf-8")) > _PERSONA_MAX_BYTES:
            text = text.encode("utf-8")[:_PERSONA_MAX_BYTES].decode(
                "utf-8", errors="ignore"
            )
            log.warning("persona: %s exceeds %d bytes; truncated", p, _PERSONA_MAX_BYTES)
        parts.append(f"# {name}\n\n{text}")
    if not parts:
        return ""
    return (
        "以下是你的人格与主人偏好设定（你的内部参考，不是要原样复述给用户的素材）。"
        "遵守其中的身份、说话风格、输出契约与禁区约束：\n\n"
        + "\n\n---\n\n".join(parts)
    )


def _build_args(
    *,
    prompt: str,
    session_id: str | None,
    disallowed_tools: list[str] | None,
    disallowed_bash_patterns: list[str] | None,
    model: str | None,
    stream: bool,
    meta_work_dir: str | None = None,
) -> list[str]:
    """Construct the ``claude`` CLI argument list."""
    if stream:
        # Claude CLI ≥2.1.138 enforces --verbose alongside --print
        # --output-format=stream-json. Without it the subprocess exits
        # immediately with "Error: When using --print,
        # --output-format=stream-json requires --verbose" and run_stream
        # yields zero events — the user sees "(no answer)".
        args = [
            "claude",
            "--print",
            "--verbose",
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]
    else:
        args = ["claude", "--print", "--output-format", "json"]

    # Restrict MCP servers to fleet-specific minimum (addresses
    # post-restart CPU storm — each claude session was spawning ~8 MCP
    # servers from user+plugin config; strict mode + minimal config caps at ~2).
    args += [
        "--strict-mcp-config",
        "--mcp-config",
        _FLEET_MCP_CONFIG,
    ]
    if model:
        args += ["--model", model]
    if session_id:
        args += ["--resume", session_id]
    if disallowed_tools:
        args += ["--disallowedTools", ",".join(disallowed_tools)]
    # The CLI accepts --append-system-prompt once; persona (always-on) and the
    # read-phase bash restriction (only when patterns given) are concatenated
    # into a single value so neither clobbers the other.
    system_prompt_blocks: list[str] = []
    persona = _load_persona(meta_work_dir)
    if persona:
        system_prompt_blocks.append(persona)
    if disallowed_bash_patterns:
        patterns_text = "\n".join(f"- {p}" for p in disallowed_bash_patterns)
        system_prompt_blocks.append(
            "写操作策略（当前是读阶段）：你现在没有权限直接执行意图匹配以下 "
            "pattern 的 Bash 命令（按命令意图模糊匹配，非严格正则）：\n"
            f"{patterns_text}\n"
            "这是**读阶段的临时限制，不是永久禁止**。要让这类命令被执行，"
            "在回复末尾输出一个**完整的审批块**：单独一行 [APPROVAL_REQUIRED]，"
            "然后每行一个字段——操作（要跑的确切命令）/ 环境（BOE 或 线上）/ "
            "原因 / 影响 / 回滚——再单独一行 [/APPROVAL_REQUIRED]。"
            "人工确认后，framework 会用完整权限把你重新拉起（写阶段），"
            "由**你自己**把命令跑掉并报告结果。"
            "所以：现在不要跑这条命令，也**不要**把命令贴给用户让他手动执行；"
            "只输出审批块即可。不要说自己永久没权限。"
        )
    if system_prompt_blocks:
        args += ["--append-system-prompt", "\n\n===\n\n".join(system_prompt_blocks)]

    args += ["--dangerously-skip-permissions", prompt]
    return args


def _build_env() -> dict[str, str]:
    """Build env for claude subprocess: inherit os.environ but clear proxy.

    Per CLAUDE.md: must simultaneously unset proxy vars AND set NO_PROXY='*'.
    Just setting NO_PROXY is insufficient — some HTTP libs see both
    http_proxy and NO_PROXY='*' and behavior is inconsistent.

    TZ is pinned to Asia/Shanghai so any timestamp the agent produces in
    its reply (or reads via ``date`` / file mtimes) is Beijing time,
    matching what the user saw on their clock when they sent the
    message. Hosts often run UTC; without this the agent would answer
    "现在是 09:00" when it's actually 17:00 in Beijing.
    """
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"}
    }
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["TZ"] = "Asia/Shanghai"
    return env


async def run(
    *,
    work_dir: str,
    prompt: str,
    timeout: int,
    session_id: str | None = None,
    disallowed_tools: list[str] | None = None,
    disallowed_bash_patterns: list[str] | None = None,
    model: str | None = None,
    stream: bool = False,
    meta_work_dir: str | None = None,
) -> RunResult:
    """Fork ``claude --print`` subprocess; return parsed RunResult.

    G3 wiring (``disallowed_bash_patterns``): LLM-guided soft block via
    ``--append-system-prompt``, not regex-enforced. For hard deny, combine with
    ``disallowed_tools=['Bash']``. Patterns are best-effort intent matching.

    ``meta_work_dir``: when set, SOUL.md + USER.md from that dir are injected
    as the system prompt (the bot's persona/style contract, which Claude Code
    otherwise never loads because the cwd is the project work_dir).
    """
    args = _build_args(
        prompt=prompt,
        session_id=session_id,
        disallowed_tools=disallowed_tools,
        disallowed_bash_patterns=disallowed_bash_patterns,
        model=model,
        stream=False,
        meta_work_dir=meta_work_dir,
    )
    env = _build_env()

    log.debug("Spawning claude subprocess: %s", args[:6])
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=work_dir,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        log.error("claude subprocess timed out after %ds -- terminating.", timeout)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
        # exit_code=-1 is a sentinel for "caller-initiated timeout".
        # Scheduler should check `timed_out` instead of `exit_code < 0`
        # to distinguish from SIGTERM-on-exit (which has negative returncode too).
        return RunResult(
            text="⚠️ 分析超时",
            session_id=session_id,
            exit_code=-1,
            timed_out=True,
        )

    if proc.returncode != 0:
        if _looks_like_silent_auth_failure(proc.returncode, stdout, stderr):
            log.error(
                "claude exited %d with empty stdout/stderr — assuming auth failed",
                proc.returncode,
            )
            return RunResult(
                text=AUTH_FAILED_TEXT,
                session_id=session_id,
                exit_code=proc.returncode,
            )
        err = stderr.decode(errors="replace")[:500]
        log.error("claude exited %d: %s", proc.returncode, err)
        return RunResult(
            text=f"⚠️ Claude 错误: {err}",
            session_id=session_id,
            exit_code=proc.returncode,
        )

    try:
        data = json.loads(stdout)
        # Top-level envelope from `claude --print --output-format=json`:
        #   {"result": str, "session_id": ..., "num_turns": int,
        #    "usage": {"input_tokens": N, "output_tokens": M, ...},
        #    "modelUsage": {"<model_id>": {...}}, ...}
        usage = data.get("usage") or {}
        model_usage = data.get("modelUsage") or {}
        model_id = next(iter(model_usage), None) if model_usage else None
        return RunResult(
            text=data.get("result", ""),
            session_id=data.get("session_id"),
            exit_code=0,
            num_turns=data.get("num_turns", 0),
            usage_input_tokens=int(usage.get("input_tokens", 0)),
            usage_output_tokens=int(usage.get("output_tokens", 0)),
            model=model_id,
        )
    except json.JSONDecodeError:
        log.warning(
            "claude stdout is not JSON (%d bytes); returning raw text",
            len(stdout),
        )
        return RunResult(
            text=f"⚠️ Claude 输出非 JSON，原始文本：\n{stdout.decode(errors='replace')}",
            session_id=session_id,
            exit_code=0,
        )


async def run_stream(
    *,
    work_dir: str,
    prompt: str,
    timeout: int,
    session_id: str | None = None,
    disallowed_tools: list[str] | None = None,
    disallowed_bash_patterns: list[str] | None = None,
    model: str | None = None,
    meta_work_dir: str | None = None,
) -> AsyncIterator[dict]:
    """Yield each stream-json event dict from ``claude --output-format stream-json``.

    Caller consumes with ``async for event in run_stream(...)``.
    Early ``break`` out of the loop is safe — the finally block will terminate
    the subprocess to prevent pipe-buffer deadlock.
    Bad JSON lines are skipped with a warning.

    ``meta_work_dir``: see :func:`run` — injects SOUL.md + USER.md persona.
    """
    args = _build_args(
        prompt=prompt,
        session_id=session_id,
        disallowed_tools=disallowed_tools,
        disallowed_bash_patterns=disallowed_bash_patterns,
        model=model,
        stream=True,
        meta_work_dir=meta_work_dir,
    )
    env = _build_env()

    log.debug("Spawning claude stream subprocess: %s", args[:6])
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        cwd=work_dir,
        env=env,
        # stream-json emits one event per line; a single line (e.g. a large
        # tool_result carrying a full TCC config JSON) can exceed asyncio's
        # default 64KB StreamReader limit, which raises LimitOverrunError ->
        # ValueError mid-stream and kills the whole turn. Raise the cap to 16MB.
        limit=16 * 1024 * 1024,
    )

    yielded_any = False
    timed_out_local = False
    try:
        async with asyncio.timeout(timeout):
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("stream-json: skipping bad line: %r", line[:120])
                    continue
                yielded_any = True
                yield parsed
    except (asyncio.TimeoutError, TimeoutError):
        log.error("claude stream timed out after %ds -- terminating.", timeout)
        timed_out_local = True
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
    except ValueError as e:
        # asyncio StreamReader re-raises LimitOverrunError as ValueError when a
        # single line exceeds the buffer limit (raised above to 16MB). Degrade
        # gracefully instead of bubbling a raw traceback up to the scheduler.
        log.error("claude stream line exceeded buffer limit -- terminating: %s", e)
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            await proc.wait()
    finally:
        # Terminate on early-break (GeneratorExit) to prevent pipe-buffer deadlock.
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
                await proc.wait()

    # Silent-auth-failure detection: stream produced zero events AND exit≠0
    # AND stderr is empty. Drain stderr (small in practice) so we can tell
    # this apart from "real" failures that printed an error message.
    if not yielded_any and not timed_out_local and proc.returncode not in (None, 0):
        try:
            stderr_bytes = await proc.stderr.read() if proc.stderr else b""
        except Exception:
            stderr_bytes = b""
        if _looks_like_silent_auth_failure(proc.returncode, b"", stderr_bytes):
            log.error(
                "claude stream exited %d with no events and empty stderr "
                "— assuming auth failed",
                proc.returncode,
            )
            yield {"type": "_auth_failed"}
