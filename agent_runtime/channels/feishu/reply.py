"""lark-cli-based reply sender with newline-aware chunking."""

import asyncio
import logging

from agent_runtime.channels.feishu._env import build_lark_cli_env

log = logging.getLogger(__name__)


def split_reply(text: str, max_len: int = 15000) -> list[str]:
    """Split text into chunks <= max_len, preferring newline boundaries.

    If text length <= max_len, returns [text] unchanged. Otherwise repeatedly
    cuts: look for the last newline in text[:max_len]; cut after it. If no
    newline exists, hard-cut at max_len.
    """
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        head = remaining[:max_len]
        nl_idx = head.rfind("\n")
        cut = nl_idx + 1 if nl_idx > 0 else max_len
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


async def send(
    *,
    lark_cli: str,
    message_id: str,
    text: str,
    as_bot: bool = True,
    chunk_timeout: float = 30.0,
) -> None:
    """Send reply to a feishu message via lark-cli im +messages-reply.

    Splits long text via split_reply and sends each chunk sequentially.
    Stops on the first chunk failure (non-zero exit or timeout) rather than
    sending partial/out-of-order content. Non-fatal: errors are logged, not raised.

    On success (all chunks delivered), emits one INFO log so runtime.log
    shows the reply round-trip — without this, only failure paths logged
    and "did Feishu actually receive the reply?" was invisible.
    """
    chunks = split_reply(text)
    for chunk in chunks:
        args = [lark_cli, "im", "+messages-reply", "--message-id", message_id]
        if as_bot:
            args += ["--as", "bot"]
        # Use --markdown so lark-cli wraps the chunk as Feishu post format
        # with proper headings/bold/list rendering. Plain --text would
        # display raw markdown characters (#, **, -) verbatim in Feishu UI.
        args += ["--reply-in-thread", "--markdown", chunk]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            log.error("lark-cli spawn failed: %s (path=%s)", e, lark_cli)
            return

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=chunk_timeout
            )
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            log.warning(
                "lark-cli reply timeout after %.0fs (msg %s, chunk len %d)",
                chunk_timeout, message_id, len(chunk),
            )
            return

        if proc.returncode != 0:
            log.warning(
                "lark-cli reply failed (exit %d): %s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
            return

    log.info(
        "lark-cli reply sent: msg=%s chunks=%d total_len=%d",
        message_id, len(chunks), len(text),
    )
