"""飞书 push bot 消息封装。Used by health watchdog + decay loop for self-alerts."""
import asyncio
import logging
import os
import shutil

log = logging.getLogger(__name__)


async def push_to_self(
    text: str,
    *,
    open_id: str | None = None,
    runner=asyncio.create_subprocess_exec,
) -> bool:
    """Push a text message to user's own feishu via lark-cli bot.

    Returns True on success, False on failure (logs but never raises).
    `open_id` defaults to env LARK_SELF_OPEN_ID; if unset, returns False.
    `runner` injectable for tests.
    """
    oid = open_id or os.environ.get("LARK_SELF_OPEN_ID")
    if not oid:
        log.warning("push_to_self: no open_id (set LARK_SELF_OPEN_ID env)")
        return False
    if not shutil.which("lark-cli"):
        log.warning("push_to_self: lark-cli not on PATH")
        return False
    try:
        proc = await runner(
            "lark-cli",
            "im",
            "+messages-send",
            "--user-id",
            oid,
            "--as",
            "bot",
            "--text",
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode != 0:
            log.warning("push_to_self: lark-cli exit=%s", proc.returncode)
            return False
        return True
    except Exception as e:
        log.warning("push_to_self: %r", e)
        return False
