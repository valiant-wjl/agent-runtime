"""LarkChannel: implements ChannelAdapter over `lark-cli`."""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import AsyncIterator

from agent_runtime.channels import ChannelAdapter, ParsedMsg
from agent_runtime.channels.feishu._env import build_lark_cli_env
from agent_runtime.channels.feishu import parser, reply as reply_mod, clean as clean_mod

log = logging.getLogger(__name__)


class StreamCardDegraded(Exception):
    """Raised when stream-card path must be abandoned for the rest of a turn.

    Two trigger paths:
    1. send_card outright failure (single attempt → raise immediately).
    2. update_card consecutive failures hit ``Channel._max_update_failures``.

    Caller (scheduler) is expected to catch this and degrade to text reply
    (channel.reply) for the remainder of the conversation turn.
    """
    pass


class ImageDownloadFailed(Exception):
    """download_image subprocess failed (spawn / non-zero exit / timeout / no file)."""
    pass


class ImageTooLarge(Exception):
    """Downloaded image exceeded max_bytes; file already removed."""
    pass


# How long (seconds) to wait for a single lark-cli card subprocess.
# Cards should round-trip in < 1s; 5s leaves headroom for transient slowness
# without stalling the streaming loop.
_CARD_CMD_TIMEOUT = 30.0

# Default subprocess timeout for image download. Feishu image download is
# normally < 2s; 30s caps unrecoverable network hangs.
_IMAGE_DOWNLOAD_TIMEOUT = 30.0

# Timeout for fetching topic / thread history. Module-level so tests can
# monkey-patch a shorter value without changing the public method signature.
_TOPIC_HISTORY_TIMEOUT = 30.0


def _log_history_fetch(topic_id: str, elapsed_s: float, count: int, outcome: str) -> None:
    """Emit one structured history_fetch INFO line. Best-effort; never raises.

    Called from every return path of ``fetch_topic_history`` so the per-turn
    lark-cli history subprocess shows up in logs as ``history_fetch
    topic_id=... elapsed_ms=... message_count=... outcome=...``. Lets ops
    diagnose whether a "🔄 分析中..." stall is sitting in the history
    fetch (cold lark-cli) vs. the downstream Claude stream.
    """
    try:
        log.info(
            "history_fetch topic_id=%s elapsed_ms=%d message_count=%d outcome=%s",
            topic_id, int(elapsed_s * 1000), count, outcome,
        )
    except Exception:
        pass


def _content_to_plain_text(msg_type, content_raw) -> str:
    """Best-effort plain text extraction from a feishu API message body.

    Mirrors ``parser._extract_text`` for text/post but works on the API
    response shape (where ``body.content`` is a JSON string). No image
    extraction here — history rendering only needs text-ish content.
    """
    if not content_raw:
        return ""
    if isinstance(content_raw, str):
        try:
            content = json.loads(content_raw)
        except json.JSONDecodeError:
            return content_raw
    else:
        content = content_raw
    if not isinstance(content, dict):
        return ""
    if msg_type == "text":
        return content.get("text", "") or ""
    if msg_type in ("post", "interactive"):
        # post: top-level `content` is a list-of-list of {tag,text} nodes.
        # interactive (Feishu card): same node shape but under `elements`,
        # plus widget-style nodes (tag in {div,markdown,plain_text}) with
        # `text: {content}` dicts. Modern cards put title under
        # `header.title.content`; legacy cards use top-level `title`.
        chunks: list[str] = []
        paragraphs = content.get("content") or content.get("elements") or []
        for para in paragraphs:
            nodes = para if isinstance(para, list) else [para]
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                tag = node.get("tag")
                if tag == "text":
                    chunks.append(node.get("text", "") or "")
                elif tag in ("div", "markdown", "plain_text"):
                    txt = node.get("text") or {}
                    if isinstance(txt, dict):
                        chunks.append(txt.get("content", "") or "")
                    elif isinstance(txt, str):
                        chunks.append(txt)
        title = content.get("title") or ""
        if not title:
            header = content.get("header") or {}
            title_obj = header.get("title") or {}
            if isinstance(title_obj, dict):
                title = title_obj.get("content", "") or ""
        return (title + " " + "".join(chunks)).strip()
    return ""


class Channel:
    """ChannelAdapter implementation for Feishu/Lark via `lark-cli`.

    Wires up parser/reply/clean into the Protocol contract. Stream card
    methods (send_card/update_card) call real feishu interactive card APIs
    via ``lark-cli im +messages-reply --card-content`` /
    ``lark-cli im +messages-patch``; consecutive update_card failures raise
    ``StreamCardDegraded`` so the caller can fall back to text reply.

    fetch_thread_history returns empty list in M2; v1.x (`/lark-cli im
    +threads-messages-list`) integration deferred.
    """

    name = "feishu"

    # Hardcoded for MVP M6 — will become a configurable knob in M9.
    _max_update_failures: int = 3

    def __init__(self, config: dict) -> None:
        self._config = config
        self._lark_cli = config.get("lark_cli", "lark-cli")
        self._event_types = config.get("event_types", "im.message.receive_v1")
        self._bot_mention_key = config.get("bot_mention_key")
        self._dedup_window = int(config.get("dedup_window", 300))
        self._proc: asyncio.subprocess.Process | None = None
        # Per-card consecutive update_card failure counter. Reset on success;
        # >= _max_update_failures triggers StreamCardDegraded so the caller
        # can switch to a text-reply fallback for the remaining stream.
        self._update_fail_streak: dict[str, int] = {}

    async def subscribe(self) -> AsyncIterator[dict]:
        """Spawn `lark-cli event +subscribe` and yield parsed JSON events.

        Caller consumes with `async for event in channel.subscribe()`.
        On break/return, the subprocess is terminated by close() (which
        caller is expected to await). Reconnect logic lives in
        runtime/scheduler (exponential backoff around each subscribe()
        call), not here.
        """
        if self._proc is not None and self._proc.returncode is None:
            raise RuntimeError(
                "LarkChannel already subscribed; call close() before re-subscribing"
            )

        args = [
            self._lark_cli,
            "event", "+subscribe",
            "--event-types", self._event_types,
            "--as", "bot",
            "--quiet",
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            log.error("lark-cli event spawn failed: %s (path=%s)", e, self._lark_cli)
            return

        log.info("lark-cli subscribed (pid=%s, event_types=%s)",
                 self._proc.pid, self._event_types)

        assert self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    log.warning("lark-cli event line is not JSON: %s (line=%r)", e, line[:200])
        finally:
            # If caller break'd out, terminate the subprocess.
            if self._proc is not None and self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5)
                except (asyncio.TimeoutError, TimeoutError):
                    self._proc.kill()
                    try:
                        await asyncio.wait_for(self._proc.wait(), timeout=2)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass

    async def parse(self, event: dict) -> ParsedMsg | None:
        """Parse a raw lark-cli event into ParsedMsg."""
        return parser.parse(event, dedup_window=self._dedup_window)

    async def reply(self, parsed: ParsedMsg, text: str) -> None:
        """Reply plain text (cleaned for feishu markdown)."""
        cleaned = clean_mod.clean_for_feishu(text)
        await reply_mod.send(
            lark_cli=self._lark_cli,
            message_id=parsed.message_id,
            text=cleaned,
        )

    async def send_card(self, parsed: ParsedMsg, card: dict) -> str:
        """Send an interactive card as a reply to ``parsed.message_id``.

        Calls ``lark-cli im +messages-reply --content <json> --msg-type
        interactive`` and parses the returned JSON to extract the new card
        message id (om_xxx) for subsequent ``update_card`` calls.

        Note: lark-cli upstream renamed ``--card-content`` to
        ``--content`` + ``--msg-type interactive`` (with msg_type defaulting
        to text). Older code paths using ``--card-content`` will fail with
        "unknown flag" on current lark-cli.

        On any failure (spawn error, non-zero exit, timeout, malformed
        stdout, missing message_id) raises ``StreamCardDegraded`` so the
        caller can fall back to plain text reply for THIS turn. There is
        no per-message failure-streak counter on send: the card path is
        either available or it is not.

        Compatibility: legacy callers pass ``{"fallback_text": "..."}``
        as a placeholder card shape. Older lark-cli accepted that and
        produced a plain-text card; current lark-cli forwards as-is to
        Feishu which rejects with 200621 ("parse card json error").
        Translate the placeholder into a minimal valid v1 card here so
        callers don't need to know the schema.
        """
        if set(card.keys()) == {"fallback_text"}:
            card = {
                "config": {"wide_screen_mode": True},
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": str(card["fallback_text"]),
                        },
                    }
                ],
            }
        card_json = json.dumps(card, ensure_ascii=False)
        args = [
            self._lark_cli, "im", "+messages-reply",
            "--message-id", parsed.message_id,
            "--as", "bot",
            "--reply-in-thread",
            "--content", card_json,
            "--msg-type", "interactive",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            log.error("send_card: lark-cli spawn failed: %s (path=%s)", e, self._lark_cli)
            raise StreamCardDegraded(f"lark-cli spawn failed: {e}") from e

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CARD_CMD_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError) as e:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            log.warning(
                "send_card: timeout after %.1fs (msg_id=%s)",
                _CARD_CMD_TIMEOUT, parsed.message_id,
            )
            raise StreamCardDegraded("send_card timeout") from e

        if proc.returncode != 0:
            log.warning(
                "send_card: lark-cli exit %d: %s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
            raise StreamCardDegraded(
                f"send_card lark-cli exit {proc.returncode}"
            )

        try:
            payload = json.loads(stdout.decode(errors="replace") or "{}")
        except json.JSONDecodeError as e:
            log.warning(
                "send_card: stdout is not JSON: %s (head=%r)",
                e, stdout[:200],
            )
            raise StreamCardDegraded("send_card stdout not JSON") from e

        # Modern lark-cli wraps the API response: {ok, identity, data: {message_id, ...}}.
        # Older lark-cli emitted message_id at the top level. Accept both for
        # forward/backward compatibility.
        data = payload.get("data") or {}
        new_msg_id = (
            payload.get("message_id")
            or payload.get("msg_id")
            or data.get("message_id")
            or data.get("msg_id")
        )
        if not new_msg_id:
            log.warning(
                "send_card: response missing message_id (keys=%s, data_keys=%s)",
                list(payload.keys()),
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
            )
            raise StreamCardDegraded("send_card response missing message_id")
        return new_msg_id

    async def update_card(self, card_msg_id: str, card: dict) -> bool:
        """Patch an existing card via the Feishu Open API.

        Upstream lark-cli removed the convenience subcommand
        ``im +messages-patch`` (production log evidence 2026-05-* shows
        every call emitted ``Error: unknown flag: --message-id`` → exit 1
        → cards stuck at progress state forever). We now go through the
        generic ``api PATCH /open-apis/im/v1/messages/<id>`` route with
        the card serialized into a JSON-encoded ``content`` body — the
        same shape Feishu's REST API expects.

        Returns True on success (and resets the per-card failure streak),
        False on a single failure. Raises ``StreamCardDegraded`` once
        consecutive failures for ``card_msg_id`` reach
        ``_max_update_failures`` so the caller can stop trying and fall
        back to text replies for the rest of the stream.
        """
        # Feishu PATCH body shape: {"content": "<json-stringified card>"}
        body = json.dumps(
            {"content": json.dumps(card, ensure_ascii=False)},
            ensure_ascii=False,
        )
        args = [
            self._lark_cli, "api", "PATCH",
            f"/open-apis/im/v1/messages/{card_msg_id}",
            "--data", body,
            "--as", "bot",
        ]

        success = False
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    env=build_lark_cli_env(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except (FileNotFoundError, PermissionError) as e:
                log.error(
                    "update_card: lark-cli spawn failed: %s (path=%s)",
                    e, self._lark_cli,
                )
                success = False
            else:
                try:
                    _stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=_CARD_CMD_TIMEOUT,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2)
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                    log.warning(
                        "update_card: timeout after %.1fs (card=%s)",
                        _CARD_CMD_TIMEOUT, card_msg_id,
                    )
                    success = False
                else:
                    if proc.returncode == 0:
                        success = True
                    else:
                        log.warning(
                            "update_card: lark-cli exit %d (card=%s): %s",
                            proc.returncode, card_msg_id,
                            stderr.decode(errors="replace")[:200],
                        )
                        success = False
        finally:
            # Update streak counter regardless of which branch failed.
            if success:
                self._update_fail_streak.pop(card_msg_id, None)
            else:
                self._update_fail_streak[card_msg_id] = (
                    self._update_fail_streak.get(card_msg_id, 0) + 1
                )

        if not success and self._update_fail_streak[card_msg_id] >= self._max_update_failures:
            # Drop the streak entry — caller will degrade to text reply for
            # this card, so the failure record is dead weight after this raise.
            # Prevents unbounded dict growth in long-running schedulers.
            streak = self._update_fail_streak.pop(card_msg_id)
            raise StreamCardDegraded(
                f"update_card failed {streak} times in a row for {card_msg_id}"
            )
        return success

    async def fetch_thread_history(self, root_id: str) -> list[str]:
        """M2 stub: returns []. v1.x will call `im +threads-messages-list`."""
        return []

    async def download_image(
        self,
        *,
        message_id: str,
        image_key: str,
        dest_dir: Path,
        max_bytes: int = 10_000_000,
        timeout: float = _IMAGE_DOWNLOAD_TIMEOUT,
    ) -> Path:
        """Download a Feishu message image to ``dest_dir/<image_key>.<ext>``.

        Spawns ``lark-cli im +messages-resources-download`` with cwd=dest_dir
        because lark-cli's ``--output`` flag is relative-only and rejects
        traversal. basename equals image_key (no extension); lark-cli infers
        and appends the extension from Content-Type. We glob to recover the
        resolved Path.

        Raises ImageDownloadFailed on spawn failure, non-zero exit, timeout,
        or no file produced. Raises ImageTooLarge if the result exceeds
        ``max_bytes`` (file is removed).
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        args = [
            self._lark_cli,
            "im", "+messages-resources-download",
            "--message-id", message_id,
            "--file-key", image_key,
            "--type", "image",
            "--output", image_key,
            # Match the rest of the codebase (reply.py / subscribe / send_card):
            # IM ops run as bot. Default --as user lacks im: scopes on the
            # deployment lark-cli login (verified via auth scopes 2026-05-06).
            "--as", "bot",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(dest_dir),
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            raise ImageDownloadFailed(f"spawn lark-cli failed: {e}") from e

        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as e:
            log.warning("download_image timed out after %ss (key=%s)", timeout, image_key)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
            raise ImageDownloadFailed(f"download timed out after {timeout}s") from e

        if proc.returncode != 0:
            err = stderr.decode(errors="replace")[:500]
            raise ImageDownloadFailed(
                f"lark-cli exit {proc.returncode}: {err.strip() or '<empty stderr>'}"
            )

        matches = sorted(dest_dir.glob(f"{image_key}*"))
        if not matches:
            raise ImageDownloadFailed(
                f"no file produced for image_key={image_key!r} in {dest_dir}"
            )
        path = matches[-1]
        size = path.stat().st_size
        if size > max_bytes:
            try:
                path.unlink()
            except OSError as e:
                log.warning("download_image: failed to remove oversized file %s: %s", path, e)
            raise ImageTooLarge(
                f"image {size} bytes exceeds limit {max_bytes} (key={image_key})"
            )
        log.info(
            "download_image OK: key=%s path=%s size=%d", image_key, path, size,
        )
        return path

    async def fetch_message_text(self, message_id: str) -> str | None:
        """Fetch a single message and render it as a `<sender_id>: <text>`
        history line, mirroring fetch_topic_history's row format.

        Returns None on subprocess / API / parse failure. Used to prepend
        a thread anchor (which threads-messages-list intentionally
        excludes via `thread_message_position == -1`) — typically an alarm
        card whose body carries the actual incident the user wants
        analysed.
        """
        args = [
            self._lark_cli, "api", "GET",
            f"/open-apis/im/v1/messages/{message_id}",
            "--as", "user",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            log.warning("fetch_message_text spawn failed: %s", e)
            return None
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_TOPIC_HISTORY_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("fetch_message_text timed out (msg=%s)", message_id)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
            return None
        if proc.returncode != 0:
            log.warning(
                "fetch_message_text lark-cli exit %d: %s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
            return None
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            log.warning("fetch_message_text: bad JSON output: %s", e)
            return None
        items = ((payload or {}).get("data") or {}).get("items") or []
        if not items:
            return None
        item = items[0]
        sender = item.get("sender") or {}
        sender_id = (
            sender.get("id")
            or sender.get("user_id")
            or sender.get("open_id")
            or "?"
        )
        msg_type = item.get("msg_type") or item.get("message_type")
        content_raw = (item.get("body") or {}).get("content") or item.get("content") or ""
        text = _content_to_plain_text(msg_type, content_raw)
        if not text:
            return f"{sender_id}: [{msg_type or 'unknown'}]"
        return f"{sender_id}: {text}"

    async def fetch_topic_history(
        self, topic_id: str, limit: int = 20,
    ) -> list[str]:
        """Fetch up to ``limit`` recent messages in a Feishu topic, asc order.

        Returns ``["<sender_id>: <text>", ...]`` for text/post; non-text types
        render as ``"<sender_id>: [<msg_type>]"``. On any subprocess or
        parsing error, returns ``[]`` and logs a warning — history is
        non-critical context and must not block the read flow.

        Calls ``lark-cli im +threads-messages-list --thread <id> --sort asc
        --page-size <limit>``. The ``--thread`` flag accepts both ``om_`` and
        ``omt_`` prefixes; lark-cli auto-resolves ``om_`` → ``omt_``.

        Also emits one structured ``history_fetch`` INFO log line on every
        path so per-turn lark-cli latency and outcome are visible.
        """
        _fetch_t0 = time.monotonic()
        _fetch_outcome = "ok"
        _fetch_count = 0
        args = [
            self._lark_cli,
            "im", "+threads-messages-list",
            "--thread", topic_id,
            "--sort", "asc",
            "--page-size", str(limit),
            # Run as user: lark-cli's user OAuth token (granted via device
            # flow during `lark-cli auth login`) carries
            # im:message.group_msg:get_as_user + im:message.p2p_msg:get_as_user
            # which the threads-messages-list endpoint requires. Bot
            # identity needs the equivalent app-level scope provisioned in
            # the open-platform admin console + a published version; absent
            # that, --as bot returns 230027 Permission denied. Subscribing
            # to im.message.receive_v1 events ≠ permission to read history.
            "--as", "user",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                env=build_lark_cli_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (FileNotFoundError, PermissionError) as e:
            log.warning("fetch_topic_history spawn failed: %s", e)
            _fetch_outcome = "spawn_error"
            _log_history_fetch(topic_id, time.monotonic() - _fetch_t0, 0, _fetch_outcome)
            return []

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_TOPIC_HISTORY_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("fetch_topic_history timed out (topic=%s)", topic_id)
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, TimeoutError):
                proc.kill()
            _fetch_outcome = "timeout"
            _log_history_fetch(topic_id, time.monotonic() - _fetch_t0, 0, _fetch_outcome)
            return []

        if proc.returncode != 0:
            log.warning(
                "fetch_topic_history lark-cli exit %d: %s",
                proc.returncode,
                stderr.decode(errors="replace")[:200],
            )
            _fetch_outcome = "exit_nonzero"
            _log_history_fetch(topic_id, time.monotonic() - _fetch_t0, 0, _fetch_outcome)
            return []

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as e:
            log.warning("fetch_topic_history: bad JSON output: %s", e)
            _fetch_outcome = "bad_json"
            _log_history_fetch(topic_id, time.monotonic() - _fetch_t0, 0, _fetch_outcome)
            return []

        # Lark-cli wraps API response: {ok, data: {messages, has_more}}.
        # Older lark-cli used "items"; current uses "messages". Accept both.
        data = payload.get("data") if isinstance(payload, dict) else None
        items = (data or {}).get("messages") or (data or {}).get("items") or []
        formatted: list[str] = []
        for item in items:
            sender = item.get("sender") or {}
            sender_id = (
                sender.get("id")
                or sender.get("user_id")
                or sender.get("open_id")
                or "?"
            )
            msg_type = item.get("msg_type") or item.get("message_type")
            content_raw = (item.get("body") or {}).get("content") or item.get("content") or ""
            text = _content_to_plain_text(msg_type, content_raw)
            if text:
                formatted.append(f"{sender_id}: {text}")
            else:
                formatted.append(f"{sender_id}: [{msg_type or 'unknown'}]")
        # Defensive trim if API returned more than asked for.
        trimmed = formatted[-limit:]
        _fetch_count = len(trimmed)
        _fetch_outcome = "ok" if _fetch_count > 0 else "empty"
        _log_history_fetch(topic_id, time.monotonic() - _fetch_t0, _fetch_count, _fetch_outcome)
        return trimmed

    async def close(self) -> None:
        """Terminate the subscribe subprocess, if any."""
        if self._proc is not None and self._proc.returncode is None:
            log.info("closing lark-cli subscribe (pid=%s)", self._proc.pid)
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except (asyncio.TimeoutError, TimeoutError):
                self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2)
                except (asyncio.TimeoutError, TimeoutError):
                    pass
