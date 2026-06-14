"""Feishu IM message poller — incoming-webhook fallback for alert chats.

Why this exists:
  Feishu's `im.message.receive_v1` event subscription does NOT fire for
  messages posted by **incoming webhooks / custom bots** (e.g. Aily's
  alert webhook). Those messages exist in the chat but never reach the
  lark-cli subscriber. We fall back to polling the IM messages-list API
  with the user access token (which carries `im:message:readonly`) and
  manually feed the discovered messages into the existing scheduler
  pipeline so the alert_resolver branch can pick them up.

Public surface:
  - normalize_card(body_content_json) -> str
      Convert a feishu interactive card's `body.content` (a JSON-encoded
      object `{title?, elements: [[{tag, ...}]]}`) to a single
      whitespace-trimmed alert text suitable for retriever / judge.
      Generic over the standard schema; no Aily-specific field handling.
  - PollerCursor (next story)
  - poll_chat (next story)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent_runtime.channels import ParsedMsg
from agent_runtime.channels.feishu._env import build_lark_cli_env

log = logging.getLogger(__name__)

# Subprocess timeout for one lark-cli messages-list call. The IM API is
# fast (sub-second under healthy conditions); 30s gives enough slack for
# a slow node fork on a busy box without letting a true hang block the
# polling loop forever.
_LARK_CLI_TIMEOUT = 30

# Subprocess runner contract: callable(args, env, timeout) -> stdout str.
# Tests inject a stub; production passes ``_default_runner``.
LarkCliRunner = Callable[..., Awaitable[str]]

_BLANK_LINES_RE = re.compile(r"\n{3,}")
_ALERT_TEXT_CAP = 2000  # mirrors judge's _MAX_QUERY_CHARS so we never grow past
                       # what the judge can read anyway


def normalize_card(body_content: str) -> str:
    """Render a feishu interactive card body into a flat alert text.

    Schema handled (best-effort, fail-soft):
        {"title": str?, "elements": [[{tag, text}|{tag:"hr"}|{tag, ...}, ...], ...]}

    Rules:
      - title (if any) becomes the first line
      - each row of `elements` joins its text-tagged children with no
        separator (preserving any explicit spacing already in the text)
      - `hr` tags become a blank-line break between rows
      - unknown tags are dropped silently — don't leak schema noise
      - 3+ consecutive newlines collapse to 2; trailing whitespace stripped
      - returns "" on any decode / shape error so caller skips the message

    Bounded to ``_ALERT_TEXT_CAP`` chars; the judge truncates to the same
    bound downstream, this is just defence-in-depth so the poller itself
    can't be the source of an oversized alert_text.
    """
    if not body_content:
        return ""
    try:
        data = json.loads(body_content)
    except json.JSONDecodeError:
        log.warning("poller.normalize_card: bad JSON body.content (len=%d)",
                    len(body_content))
        return ""
    if not isinstance(data, dict):
        return ""

    out_lines: list[str] = []

    title = data.get("title")
    if isinstance(title, str) and title.strip():
        out_lines.append(title.strip())

    elements = data.get("elements")
    if isinstance(elements, list):
        for row in elements:
            if not isinstance(row, list):
                continue
            chunks: list[str] = []
            saw_hr = False
            for node in row:
                if not isinstance(node, dict):
                    continue
                tag = node.get("tag")
                if tag == "text":
                    text = node.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
                elif tag == "hr":
                    saw_hr = True
                # other tags (img, action, button, ...) ignored
            line = "".join(chunks).rstrip()
            if line:
                out_lines.append(line)
            elif saw_hr:
                # blank line as visual separator
                out_lines.append("")

    body = "\n".join(out_lines)
    body = _BLANK_LINES_RE.sub("\n\n", body).strip()
    if len(body) > _ALERT_TEXT_CAP:
        body = body[:_ALERT_TEXT_CAP]
    return body


# ---------------------------------------------------------------------------
# Cursor — persists last-seen create_time per chat_id
# ---------------------------------------------------------------------------


class PollerCursor:
    """Persistent map ``{chat_id: last_create_time_ms}``.

    Single-process asyncio runtime — no in-process locking; relies on the
    serialized event loop. The atomic-rename write guards against a crash
    leaving a half-written JSON. A corrupt cursor file is treated as
    empty (warning logged) so a bad row never blocks startup.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._cache: dict[str, int] | None = None

    # ------------------------------------------------------------------

    def _load(self) -> dict[str, int]:
        if self._cache is not None:
            return self._cache
        if not self.path.is_file():
            self._cache = {}
            return self._cache
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning(
                "poller cursor: could not load %s (%s); treating as empty",
                self.path, e,
            )
            self._cache = {}
            return self._cache
        if not isinstance(data, dict):
            log.warning(
                "poller cursor: %s top-level is %s, not object; ignoring",
                self.path, type(data).__name__,
            )
            self._cache = {}
            return self._cache
        # Coerce values to int — older formats / hand edits may carry strings.
        out: dict[str, int] = {}
        for k, v in data.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                log.warning("poller cursor: dropping bad entry %r=%r", k, v)
        self._cache = out
        return out

    def _flush(self) -> None:
        assert self._cache is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Use a per-call tmp suffix so concurrent (cross-process) writes
        # don't clobber each other's tmp file. In-process is single-loop.
        tmp = self.path.with_suffix(self.path.suffix + f".tmp.{os.getpid()}")
        tmp.write_text(
            json.dumps(self._cache, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # Public

    def get(self, chat_id: str) -> int | None:
        return self._load().get(chat_id)

    def set(self, chat_id: str, create_time_ms: int) -> None:
        cache = self._load()
        cache[chat_id] = int(create_time_ms)
        self._flush()

    def get_all(self) -> dict[str, int]:
        # Defensive copy — callers iterating the snapshot must not affect
        # the persisted state.
        return dict(self._load())


# ---------------------------------------------------------------------------
# poll_chat — fork lark-cli, filter, normalize
# ---------------------------------------------------------------------------


async def _default_runner(*, args: list[str], env: dict[str, str], timeout: int) -> str:
    """Production lark-cli subprocess runner. argv form (no shell), so
    arguments are passed as discrete strings to the child — no command
    injection surface even if chat_id ever contained shell metacharacters.
    Raises on non-zero exit / timeout so the caller fail-opens to []."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(
            f"lark-cli exited {proc.returncode}: "
            f"{stderr.decode(errors='replace')[:300]}"
        )
    return stdout.decode(errors="replace")


def _normalize_message(item: dict, chat_id: str) -> ParsedMsg | None:
    """Convert a single messages-list `data.items[]` entry into ParsedMsg.

    Returns None for unsupported types or unparseable payloads — caller
    drops silently. The ParsedMsg shape mirrors what
    ``channels/feishu/parser.parse`` produces for a webhook event so the
    downstream scheduler can treat both paths identically.
    """
    msg_id = item.get("message_id")
    if not msg_id:
        return None
    msg_type = item.get("msg_type")
    body = item.get("body") or {}
    content_raw = body.get("content")
    if not isinstance(content_raw, str):
        return None

    if msg_type == "interactive":
        text = normalize_card(content_raw)
    elif msg_type == "text":
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(content, dict):
            return None
        text = (content.get("text") or "").strip()
    elif msg_type == "post":
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            return None
        text = _flatten_post(content)
    else:
        # audio / file / sticker / image-only / unknown — drop
        return None

    if not text.strip():
        return None

    sender = item.get("sender") or {}
    sender_id = sender.get("id") or ""
    sender_type = sender.get("sender_type")
    return ParsedMsg(
        channel="feishu",
        message_id=msg_id,
        thread_root_id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_id,  # messages-list payload has no display name
        text=text,
        mentions=[],
        raw_event={"event": {"message": {"message_type": msg_type}}},
        chat_type="group",
        sender_type=sender_type,
    )


def _flatten_post(content: Any) -> str:
    """Best-effort post -> text. Mirrors parser._extract_text but minimal:
    we only care that retrievers/judge see the words."""
    if not isinstance(content, dict):
        return ""
    title = content.get("title") or ""
    chunks: list[str] = []
    for para in content.get("content") or []:
        if not isinstance(para, list):
            continue
        for node in para:
            if not isinstance(node, dict):
                continue
            tag = node.get("tag")
            if tag == "text":
                chunks.append(node.get("text") or "")
            elif tag == "a":
                chunks.append(node.get("text") or node.get("href") or "")
    body = ("".join(chunks)).strip()
    if title and body:
        return f"{title}\n{body}"
    return title or body


def _peel_envelope(parsed: dict) -> dict | None:
    """Validate the lark-cli stdout envelope; return the inner Feishu API
    payload or None if the call clearly failed.

    Two failure shapes are common:
      - lark-cli wrapper error: ``{"ok": false, "error": {...}}``
      - Feishu API error: ``{"code": <non-zero>, "msg": "..."}``
    """
    if not isinstance(parsed, dict):
        return None
    if parsed.get("ok") is False:
        log.warning(
            "poller: lark-cli error envelope: %s", parsed.get("error"),
        )
        return None
    code = parsed.get("code")
    if code not in (0, None):
        log.warning(
            "poller: feishu API error code=%s msg=%s",
            code, parsed.get("msg"),
        )
        return None
    return parsed


async def poll_chat(
    *,
    chat_id: str,
    since_ms: int,
    page_size: int,
    runner: LarkCliRunner = _default_runner,
    lark_cli_path: str = "lark-cli",
    timeout: int = _LARK_CLI_TIMEOUT,
) -> list[ParsedMsg]:
    """Poll a feishu chat for messages newer than ``since_ms``.

    Returns ParsedMsg list in **ascending** create_time order so the
    scheduler dispatches the oldest unseen alert first. All errors are
    swallowed and logged — caller (the polling loop) keeps running.
    """
    params = json.dumps({
        "container_id_type": "chat",
        "container_id": chat_id,
        "page_size": int(page_size),
        "sort_type": "ByCreateTimeDesc",
    }, ensure_ascii=False)
    args = [
        lark_cli_path, "api", "GET", "/open-apis/im/v1/messages",
        "--as", "user",
        "--params", params,
    ]
    env = build_lark_cli_env()

    try:
        stdout = await runner(args=args, env=env, timeout=timeout)
    except Exception as e:
        log.warning("poller.poll_chat(%s): runner failed: %s", chat_id, e)
        return []

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        log.warning(
            "poller.poll_chat(%s): stdout not JSON (%d bytes)",
            chat_id, len(stdout),
        )
        return []

    payload = _peel_envelope(parsed)
    if payload is None:
        return []
    items = ((payload.get("data") or {}).get("items")) or []

    out: list[ParsedMsg] = []
    for item in items:
        try:
            ts = int(item.get("create_time") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= since_ms:
            continue
        msg = _normalize_message(item, chat_id=chat_id)
        if msg is None:
            continue
        # Stash the create_time on the parsed message so the loop can
        # advance the cursor without re-reading raw items.
        msg._poll_create_time_ms = ts  # type: ignore[attr-defined]
        out.append(msg)

    out.sort(key=lambda m: m._poll_create_time_ms)  # type: ignore[attr-defined]
    return out
