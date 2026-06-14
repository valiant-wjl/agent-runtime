"""Tests for runtime/claude_proc.py — subprocess wrapper + G3 bash pattern gate."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime import claude_proc


class _AsyncLineIter:
    """Mock ``proc.stdout`` — behaves like async iterator over bytes lines."""

    def __init__(self, lines: list[bytes]):
        self._lines = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


@pytest.mark.asyncio
async def test_run_returns_result_and_session_id():
    """Normal execution: parsed RunResult fields match JSON output."""
    fake_stdout = json.dumps(
        {"result": "ok", "session_id": "s-abc", "num_turns": 2}
    ).encode()
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(fake_stdout, b""))
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as spawn:
        r = await claude_proc.run(
            work_dir="/tmp/x",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=["Edit"],
        )

    assert r.text == "ok"
    assert r.session_id == "s-abc"
    assert r.exit_code == 0
    assert r.num_turns == 2
    assert r.timed_out is False

    # first positional arg to create_subprocess_exec should be "claude"
    positional_args = spawn.call_args.args
    assert positional_args[0] == "claude"


@pytest.mark.asyncio
async def test_run_timeout_terminates():
    """Timeout path: RunResult.timed_out=True and exit_code != 0."""
    mock_proc = AsyncMock()
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock()
    mock_proc.returncode = -15

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            r = await claude_proc.run(
                work_dir="/tmp/x",
                prompt="hi",
                timeout=1,
                session_id=None,
                disallowed_tools=[],
            )

    assert r.timed_out is True
    assert r.exit_code != 0


@pytest.mark.asyncio
async def test_run_passes_resume_when_session_id_given():
    """--resume <session_id> is passed when session_id is provided."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b'{"result":"x","session_id":"s2"}', b"")
    )
    mock_proc.returncode = 0
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await claude_proc.run(
            work_dir="/tmp/x",
            prompt="hi",
            timeout=10,
            session_id="prev-sess",
            disallowed_tools=[],
        )

    assert "--resume" in captured["args"]
    assert "prev-sess" in captured["args"]


@pytest.mark.asyncio
async def test_disallowed_bash_patterns_appended_to_prompt():
    """G3 gate: bash patterns must be injected via --append-system-prompt."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b'{"result":"x"}', b""))
    mock_proc.returncode = 0
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        return mock_proc

    patterns = ["platform-cli tcc.*--write", "platform-cli rds.*--execute"]
    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await claude_proc.run(
            work_dir="/tmp/x",
            prompt="do it",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
            disallowed_bash_patterns=patterns,
        )

    args_list = list(captured["args"])
    assert "--append-system-prompt" in args_list
    idx = args_list.index("--append-system-prompt")
    system_prompt_value = args_list[idx + 1]
    assert "platform-cli tcc.*--write" in system_prompt_value
    assert "platform-cli rds.*--execute" in system_prompt_value
    # The restriction must be framed as read-phase only (not a permanent ban)
    # and must steer toward a structured approval block whose write phase the
    # bot itself runs — NOT toward refusing forever / handing commands to the
    # user (regression 2026-05-25).
    assert "[APPROVAL_REQUIRED]" in system_prompt_value
    assert "操作" in system_prompt_value  # full block fields, not a bare token
    assert "写阶段" in system_prompt_value or "write phase" in system_prompt_value.lower()
    assert "NEVER run it directly" not in system_prompt_value


# ---------------------------------------------------------------------------
# run_stream tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stream_includes_partial_messages_flag():
    """Spec §5.5: stream mode must include --include-partial-messages."""
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter([])
        mock.returncode = 0
        mock.wait = AsyncMock(return_value=0)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        async for _ in claude_proc.run_stream(
            work_dir="/tmp",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
        ):
            pass

    args_list = list(captured["args"])
    assert "--output-format" in args_list
    assert "stream-json" in args_list
    assert "--include-partial-messages" in args_list
    # Claude CLI ≥2.1.138 hard-rejects `--print --output-format stream-json`
    # without --verbose: "Error: When using --print, --output-format=stream-json
    # requires --verbose". The subprocess exits before emitting any event,
    # so run_stream silently yields nothing and the caller falls back to
    # "(no answer)". Asserting --verbose here pins the fix.
    assert "--verbose" in args_list


@pytest.mark.asyncio
async def test_mcp_config_path_is_module_relative_and_exists():
    """--mcp-config must point at the fleet-mcp.json shipped with this module.

    Regression: the path was hardcoded to a Linux deploy path
    (/home/example-user/...), so on any other host claude exited with
    "Invalid MCP configuration: file not found", emitted zero events, and
    the user saw "(no answer)". Pin it to a module-relative, existing path.
    """
    import os

    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter([])
        mock.returncode = 0
        mock.wait = AsyncMock(return_value=0)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        async for _ in claude_proc.run_stream(
            work_dir="/tmp",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
        ):
            pass

    args_list = list(captured["args"])
    assert "--mcp-config" in args_list
    mcp_path = args_list[args_list.index("--mcp-config") + 1]
    assert not mcp_path.startswith("/home/"), f"hardcoded deploy path: {mcp_path}"
    assert mcp_path.endswith(os.path.join("runtime", "fleet-mcp.json"))
    assert os.path.exists(mcp_path), f"fleet-mcp.json missing at {mcp_path}"


@pytest.mark.asyncio
async def test_run_stream_yields_parsed_events():
    """run_stream yields each JSON line as a dict."""
    lines = [
        b'{"type": "message_start", "message_id": "m1"}\n',
        b'{"type": "content_block_delta", "delta": "hello"}\n',
        b'{"type": "message_stop"}\n',
    ]

    async def fake_spawn(*args, **kwargs):
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter(lines)
        mock.returncode = 0
        mock.wait = AsyncMock(return_value=0)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        events = [
            e
            async for e in claude_proc.run_stream(
                work_dir="/tmp",
                prompt="hi",
                timeout=10,
                session_id=None,
                disallowed_tools=[],
            )
        ]

    assert len(events) == 3
    assert events[0]["type"] == "message_start"
    assert events[1]["delta"] == "hello"
    assert events[2]["type"] == "message_stop"


@pytest.mark.asyncio
async def test_run_returns_auth_failed_on_empty_exit1():
    """exit=1 with empty stdout AND empty stderr → auth-failed message.

    Claude CLI ≥2.1.x silently exits 1 (no stderr, no stdout) when the
    OAuth access token is expired. The old code surfaced "⚠️ Claude 错误: "
    with nothing after the colon — opaque. Now we detect the shape and
    return a clear actionable message so the user knows to re-login.
    """
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        r = await claude_proc.run(
            work_dir="/tmp/x",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
        )

    assert r.exit_code == 1
    assert claude_proc.AUTH_FAILED_TEXT in r.text


@pytest.mark.asyncio
async def test_run_keeps_stderr_message_when_present():
    """exit=1 with real stderr → keep original Claude error template.

    Only the empty-stderr+empty-stdout shape is treated as auth failure;
    other failures (e.g. malformed flags) should still surface their stderr.
    """
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(
        return_value=(b"", b"Error: unknown flag --foo")
    )
    mock_proc.returncode = 1

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        r = await claude_proc.run(
            work_dir="/tmp/x",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
        )

    assert "unknown flag" in r.text
    assert claude_proc.AUTH_FAILED_TEXT not in r.text


@pytest.mark.asyncio
async def test_run_stream_yields_auth_failed_sentinel_on_silent_exit1():
    """Stream path: zero events + exit=1 + empty stderr → synthetic auth event.

    Without this, the scheduler's stream loop sees only an empty stream and
    falls through to "(no answer)". The sentinel lets the scheduler swap in
    the auth-failed message and trigger a self-push.
    """
    async def fake_spawn(*args, **kwargs):
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter([])
        mock.stderr = _AsyncLineIter([])
        mock.returncode = 1
        mock.wait = AsyncMock(return_value=1)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        events = [
            e
            async for e in claude_proc.run_stream(
                work_dir="/tmp",
                prompt="hi",
                timeout=10,
                session_id=None,
                disallowed_tools=[],
            )
        ]

    assert len(events) == 1
    assert events[0]["type"] == "_auth_failed"


@pytest.mark.asyncio
async def test_run_stream_no_sentinel_when_events_present():
    """If stream yielded normal events, do NOT inject auth-failed sentinel
    even if exit code is non-zero — it's not the silent-auth shape."""
    lines = [b'{"type":"message_start"}\n']

    async def fake_spawn(*args, **kwargs):
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter(lines)
        mock.stderr = _AsyncLineIter([])
        mock.returncode = 1
        mock.wait = AsyncMock(return_value=1)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        events = [
            e
            async for e in claude_proc.run_stream(
                work_dir="/tmp",
                prompt="hi",
                timeout=10,
                session_id=None,
                disallowed_tools=[],
            )
        ]

    assert len(events) == 1
    assert events[0]["type"] == "message_start"


@pytest.mark.asyncio
async def test_run_stream_skips_bad_json_line():
    """Bad JSON line is silently skipped, not raised."""
    lines = [
        b'{"type": "a"}\n',
        b'NOT JSON!\n',
        b'{"type": "b"}\n',
    ]

    async def fake_spawn(*args, **kwargs):
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter(lines)
        mock.returncode = 0
        mock.wait = AsyncMock(return_value=0)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        events = [
            e
            async for e in claude_proc.run_stream(
                work_dir="/tmp",
                prompt="hi",
                timeout=10,
                session_id=None,
                disallowed_tools=[],
            )
        ]

    # bad json line skipped, only 2 events
    assert len(events) == 2
    assert events[0]["type"] == "a"
    assert events[1]["type"] == "b"


# ---------------------------------------------------------------------------
# persona injection tests (SOUL.md + USER.md from meta_work_dir)
#
# WHY: the bot runs with cwd=project work_dir; meta/SOUL.md + meta/USER.md
# live in a SIBLING meta dir that Claude Code never auto-loads. Without
# framework-level injection the curated persona/style contract is dead code
# (it never reaches the model). These tests pin the wiring: persona text from
# meta_work_dir must land in --append-system-prompt.
# ---------------------------------------------------------------------------


def _write_meta(tmp_path, soul="", user=""):
    if soul:
        (tmp_path / "SOUL.md").write_text(soul, encoding="utf-8")
    if user:
        (tmp_path / "USER.md").write_text(user, encoding="utf-8")
    return str(tmp_path)


def _captured_append_system_prompt(args_list):
    if "--append-system-prompt" not in args_list:
        return None
    return args_list[args_list.index("--append-system-prompt") + 1]


@pytest.mark.asyncio
async def test_persona_injected_from_meta_work_dir(tmp_path):
    """SOUL.md + USER.md content must be appended to the system prompt."""
    meta = _write_meta(
        tmp_path,
        soul="我是 lbp-growth-agent，团队知识库助手。",
        user="禁区：不谈薪资 / 人事。",
    )
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b'{"result":"x"}', b""))
    mock_proc.returncode = 0
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await claude_proc.run(
            work_dir="/tmp/x",
            prompt="你是谁",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
            meta_work_dir=meta,
        )

    sp = _captured_append_system_prompt(list(captured["args"]))
    assert sp is not None
    assert "lbp-growth-agent" in sp
    assert "不谈薪资" in sp


@pytest.mark.asyncio
async def test_persona_combined_with_bash_restriction(tmp_path):
    """Persona and the read-phase bash restriction share one append value."""
    meta = _write_meta(tmp_path, soul="我是 lbp-growth-agent。")
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b'{"result":"x"}', b""))
    mock_proc.returncode = 0
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await claude_proc.run(
            work_dir="/tmp/x",
            prompt="do it",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
            disallowed_bash_patterns=["git push"],
            meta_work_dir=meta,
        )

    args_list = list(captured["args"])
    # exactly one --append-system-prompt flag (we concatenate, not pass twice)
    assert args_list.count("--append-system-prompt") == 1
    sp = _captured_append_system_prompt(args_list)
    assert "lbp-growth-agent" in sp          # persona present
    assert "[APPROVAL_REQUIRED]" in sp        # bash restriction present
    assert "git push" in sp


@pytest.mark.asyncio
async def test_no_append_when_no_persona_and_no_patterns():
    """meta_work_dir=None and no bash patterns → no --append-system-prompt."""
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b'{"result":"x"}', b""))
    mock_proc.returncode = 0
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await claude_proc.run(
            work_dir="/tmp/x",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
            meta_work_dir=None,
        )

    assert "--append-system-prompt" not in list(captured["args"])


@pytest.mark.asyncio
async def test_persona_graceful_when_meta_files_missing(tmp_path):
    """meta_work_dir set but SOUL/USER absent → no crash, no persona text."""
    meta = str(tmp_path)  # empty dir, no SOUL.md / USER.md
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(b'{"result":"x"}', b""))
    mock_proc.returncode = 0
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        await claude_proc.run(
            work_dir="/tmp/x",
            prompt="hi",
            timeout=10,
            session_id=None,
            disallowed_tools=[],
            meta_work_dir=meta,
        )

    # no SOUL/USER → nothing to inject → no flag
    assert "--append-system-prompt" not in list(captured["args"])


@pytest.mark.asyncio
async def test_persona_injected_in_run_stream(tmp_path):
    """run_stream path also injects persona (the primary user-facing path)."""
    meta = _write_meta(tmp_path, soul="我是 lbp-growth-agent。")
    captured: dict = {}

    async def fake_spawn(*args, **kwargs):
        captured["args"] = args
        mock = AsyncMock()
        mock.stdout = _AsyncLineIter([b'{"type":"a"}\n'])
        mock.returncode = 0
        mock.wait = AsyncMock(return_value=0)
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        _ = [
            e
            async for e in claude_proc.run_stream(
                work_dir="/tmp",
                prompt="hi",
                timeout=10,
                session_id=None,
                disallowed_tools=[],
                meta_work_dir=meta,
            )
        ]

    sp = _captured_append_system_prompt(list(captured["args"]))
    assert sp is not None and "lbp-growth-agent" in sp
