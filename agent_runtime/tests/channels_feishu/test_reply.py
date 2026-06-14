"""Reply tests migrated from feishu-agent-gateway/tests/test_reply.py."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.channels.feishu.reply import send, split_reply


def test_short_reply_single_chunk():
    text = "hello world"
    assert split_reply(text) == [text]


def test_long_reply_splits():
    text = "x" * 30000
    chunks = split_reply(text, max_len=15000)
    assert len(chunks) >= 2
    # 重组后完全相等（无信息丢失）
    assert "".join(chunks) == text
    for chunk in chunks:
        assert len(chunk) <= 15000


def test_split_prefers_newline_boundary():
    # 14990 'a' + '\n' + 100 'b'：期望在 \n 处切，chunk 0 以 \n 结尾
    text = ("a" * 14990) + "\n" + ("b" * 100)
    chunks = split_reply(text, max_len=15000)
    assert len(chunks) == 2
    assert chunks[0].endswith("\n")
    # 组合起来仍相等
    assert "".join(chunks) == text


# ---------------------------------------------------------------------------
# Reply send observability (US-006): success path must emit an INFO log so
# runtime.log shows when a reply actually went out. Pre-fix, only the
# failure branch logged (WARNING), making "did Feishu receive my reply?"
# debug invisible from the log.
# ---------------------------------------------------------------------------


def _ok_proc():
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


@pytest.mark.asyncio
async def test_send_logs_success_short_message(caplog):
    """Single-chunk success → INFO 'reply sent' with msg/chunks/total_len."""
    caplog.set_level(logging.DEBUG, logger="agent_runtime.channels.feishu.reply")
    with patch(
        "agent_runtime.channels.feishu.reply.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_ok_proc()),
    ):
        await send(lark_cli="/usr/bin/lark-cli", message_id="om-abc", text="hello")
    sent = [r for r in caplog.records if "reply sent" in r.message]
    assert sent, (
        f"expected INFO 'reply sent' log on success; got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
    assert sent[0].levelname == "INFO"
    msg = sent[0].message
    assert "msg=om-abc" in msg
    assert "chunks=1" in msg
    assert "total_len=5" in msg


@pytest.mark.asyncio
async def test_send_logs_success_multi_chunk(caplog):
    """Multi-chunk success → 'reply sent' reports the total chunk count."""
    text = "x" * 30000  # 2 chunks @ default max_len=15000
    caplog.set_level(logging.DEBUG, logger="agent_runtime.channels.feishu.reply")
    with patch(
        "agent_runtime.channels.feishu.reply.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_ok_proc()),
    ):
        await send(lark_cli="/usr/bin/lark-cli", message_id="om-multi", text=text)
    sent = [r for r in caplog.records if "reply sent" in r.message]
    assert sent
    msg = sent[0].message
    assert "msg=om-multi" in msg
    assert "chunks=2" in msg
    assert "total_len=30000" in msg


@pytest.mark.asyncio
async def test_send_does_not_log_success_on_failure(caplog):
    """Non-zero exit → WARNING (existing) but NOT 'reply sent' INFO."""
    bad = MagicMock()
    bad.returncode = 2
    bad.communicate = AsyncMock(return_value=(b"", b'{"error":"x"}'))
    caplog.set_level(logging.DEBUG, logger="agent_runtime.channels.feishu.reply")
    with patch(
        "agent_runtime.channels.feishu.reply.asyncio.create_subprocess_exec",
        AsyncMock(return_value=bad),
    ):
        await send(lark_cli="/usr/bin/lark-cli", message_id="om-bad", text="hi")
    sent = [r for r in caplog.records if "reply sent" in r.message]
    assert not sent, (
        "must not claim 'reply sent' when lark-cli exited non-zero; got: "
        f"{[(r.levelname, r.message) for r in caplog.records]}"
    )
