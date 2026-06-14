"""Verifier orchestration.

Two-layer design:

1. **Trigger rules** (pure, sync): :func:`should_trigger` decides whether a
   draft answer is worth a second-pair-of-eyes verifier round. User hint
   short-circuits, then content scans (numbers, service/API, TCC keys, ops
   verbs, SQL, money), then concept-question short-circuit, then default skip.

2. **Verify loop** (async): :func:`verify` forks a verifier subagent up to
   ``max_rounds`` times. The caller injects a runner (see the scheduler's
   ``_make_verifier_runner``, which wraps :mod:`agent_runtime.claude_proc`).
   The default runner raises ``NotImplementedError`` so an un-wired call is
   loud rather than silent.

A :class:`CostTracker` provides daily / per-chat budget enforcement with JSON
state persistence. Daily counters reset at calendar-date rollover (checked
lazily on every ``can_trigger`` / ``record`` call).

This module is pure rule + framework — no Claude fork happens here; the
subprocess runner is supplied by the scheduler.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# Trigger rules (pure)
# ---------------------------------------------------------------------------

_USER_CAREFUL_HINTS = ["仔细点", "double check", "确认下", "靠谱点", "别错"]
_USER_FAST_HINTS = ["快速", "随便看下", "大致就行", "急"]
_CONCEPT_PHRASES = ["什么是", "谁负责", "近期"]

# Quantitative-claim pattern: a number (int/decimal) followed by a unit
# that signals a *measurement* worth verifying — perf metric, percentage,
# byte size, time, count. The earlier rule (`\d+` count >= 2) tripped on
# log IDs (`log_id=02177...`), error codes (`errorCode=10005`), trace ids
# (`021778152743723f...`) which are not claims, just labels — that wasted
# ~70% of verifier budget. Bare digits without a unit no longer trigger.
_QUANTITATIVE = re.compile(
    # Group 1 — ASCII word-char units with trailing \b enforcing token end.
    # Distinguishes "10005ms" (match) from "10005msg" (no match — would be
    # a false positive on identifiers).
    r"\b\d+(?:\.\d+)?\s*"
    r"(?:qps|rps|tps|qpm|rpm|"
    r"req[\s/_-]?s|"
    r"ms|us|μs|ns|sec|secs|min|mins|hr|hrs|"
    r"gb|mb|kb|tb|kib|mib|gib|tib)\b"
    # Group 2 — symbol units; \b doesn't fire after a non-word char.
    r"|\b\d+(?:\.\d+)?\s*[%‰]"
    # Group 3 — CJK units; \b doesn't fire between two CJK word chars
    # (e.g. "30 个订单"), so trailing boundary is omitted. Acceptable: the
    # unit kanji is itself the disambiguator vs bare digits.
    r"|\b\d+(?:\.\d+)?\s*"
    r"(?:个|条|次|笔|份|台|节点|分钟|小时)",
    re.IGNORECASE,
)
# Strict service-identifier pattern: lowercase snake_case three-segment
# identifier where each segment is at least 3 characters (e.g.
# ``svc.module.handler``) — easy to hallucinate, so worth verifying.
# The 3-char floor plus lowercase-only rule rejects version strings
# (``1.0.0``), dates (``2026.04.23``), short filenames (``file.tar.gz`` —
# ``gz`` is 2 chars), Java packages (``Foo.Bar.Baz`` — capitals) and CJK.
_SERVICE_ID = re.compile(r"\b[a-z][a-z0-9_]{2,}\.[a-z][a-z0-9_]{2,}\.[a-z][a-z0-9_]{2,}\b")
_API_PATH = re.compile(r"/api/v\d+")
_TCC_KEY = re.compile(r"[a-z_]+\.[a-z_]+\.[a-z_]+")
_OPS_VERBS = ["restart", "重启", "修改配置", "改 db", "改db", "发布", "deploy"]
_SQL_KW = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER)\b", re.IGNORECASE)
_MONEY = re.compile(r"[¥$]\s*\d+|\d+\s*元")


@dataclass
class TriggerDecision:
    """Outcome of :func:`should_trigger`. ``reason`` is a stable enum-like tag."""

    trigger: bool
    reason: str


def should_trigger(
    question: str,
    draft_answer: str,
    user_hint: Optional[str] = None,
) -> TriggerDecision:
    """Decide whether to invoke the verifier on (question, draft_answer).

    Rule order (per spec §5.4):

    1. user_hint careful keywords  → trigger
    2. user_hint fast keywords     → skip
    3. content rules (quantitative claims with units, service/API, TCC+config,
       ops verbs, SQL, money) — content rules **win over concept short-
       circuit** because dangerous specifics deserve verification even when
       the question is conceptual. Bare digits without a unit do NOT
       trigger (log IDs / error codes are not claims worth verifying).
    4. concept-only question phrases → skip
    5. default                     → skip
    """
    if user_hint:
        for kw in _USER_CAREFUL_HINTS:
            if kw in user_hint:
                return TriggerDecision(True, "user_explicit_careful")
        for kw in _USER_FAST_HINTS:
            if kw in user_hint:
                return TriggerDecision(False, "user_explicit_fast")

    # Content-based rules (run before concept short-circuit so specifics win)
    if _QUANTITATIVE.search(draft_answer):
        return TriggerDecision(True, "quantitative_claim")
    if _SERVICE_ID.search(draft_answer) or _API_PATH.search(draft_answer):
        return TriggerDecision(True, "service_or_api")
    if _TCC_KEY.search(draft_answer) and (
        "config" in draft_answer.lower() or "TCC" in draft_answer
    ):
        return TriggerDecision(True, "tcc_config")
    lower_draft = draft_answer.lower()
    if any(v in lower_draft for v in _OPS_VERBS):
        return TriggerDecision(True, "ops_verb")
    if _SQL_KW.search(draft_answer):
        return TriggerDecision(True, "sql_kw")
    if _MONEY.search(draft_answer):
        return TriggerDecision(True, "money")

    if any(p in question for p in _CONCEPT_PHRASES):
        return TriggerDecision(False, "concept_query")

    return TriggerDecision(False, "default")


# ---------------------------------------------------------------------------
# Cost tracker
# ---------------------------------------------------------------------------


@dataclass
class _CostState:
    daily_total: int = 0
    per_chat: dict = field(default_factory=dict)
    last_reset: str = ""  # ISO date


class CostTracker:
    """Enforce daily / per-chat verifier budgets with JSON-backed persistence.

    ``configure()`` must be called once at scheduler startup. Daily reset is
    lazy: every ``can_trigger`` / ``record`` checks the calendar date and
    resets counters when it changes (no background task needed).
    """

    def __init__(self) -> None:
        self._state = _CostState()
        self._state_file: Optional[pathlib.Path] = None
        self._daily_limit = 200
        self._per_chat_limit = 30

    def configure(self, state_file_path, daily_limit: int, per_chat_limit: int) -> None:
        self._state_file = pathlib.Path(state_file_path)
        self._daily_limit = daily_limit
        self._per_chat_limit = per_chat_limit
        self._load()

    def _maybe_reset(self) -> None:
        today = date.today().isoformat()
        if self._state.last_reset != today:
            self._state = _CostState(last_reset=today)
            self._save()

    def _load(self) -> None:
        if not self._state_file or not self._state_file.exists():
            self._state = _CostState(last_reset=date.today().isoformat())
            return
        try:
            data = json.loads(self._state_file.read_text())
            if not isinstance(data, dict):
                raise ValueError(f"expected dict, got {type(data).__name__}")
            self._state = _CostState(
                daily_total=int(data.get("daily_total", 0)),
                per_chat=dict(data.get("per_chat", {})),
                last_reset=str(data.get("last_reset") or date.today().isoformat()),
            )
        except (json.JSONDecodeError, OSError, ValueError, TypeError) as exc:
            # Corrupt/unreadable state — start fresh, log warning so the
            # operator notices but the scheduler still boots.
            import logging
            logging.warning(
                "cost tracker state file %s invalid (%s), starting fresh",
                self._state_file, exc,
            )
            self._state = _CostState(last_reset=date.today().isoformat())
            return
        self._maybe_reset()

    def _save(self) -> None:
        if not self._state_file:
            return
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: serialize to a sibling tmp file then rename, so a
        # crash mid-write can never leave a half-written JSON on disk.
        tmp = self._state_file.with_suffix(self._state_file.suffix + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "daily_total": self._state.daily_total,
                    "per_chat": self._state.per_chat,
                    "last_reset": self._state.last_reset,
                }
            )
        )
        tmp.replace(self._state_file)  # atomic on POSIX

    def can_trigger(self, chat_id: str) -> bool:
        self._maybe_reset()
        if self._state.daily_total >= self._daily_limit:
            return False
        if self._state.per_chat.get(chat_id, 0) >= self._per_chat_limit:
            return False
        return True

    def record(self, chat_id: str) -> None:
        self._maybe_reset()
        self._state.daily_total += 1
        self._state.per_chat[chat_id] = self._state.per_chat.get(chat_id, 0) + 1
        self._save()


# ---------------------------------------------------------------------------
# Verify loop
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    """Outcome of :func:`verify`.

    - ``verified=True``  → verifier said PASS within budget
    - ``verified=False`` → still REVISE after ``max_rounds``; ``concerns`` set
    - ``verified=None``  → verifier process itself crashed; ``error_msg`` set
      (caller falls back to direct send)
    """

    verified: Optional[bool]
    concerns: list = field(default_factory=list)
    rounds_used: int = 0
    error_msg: str = ""


async def verify(
    *,
    work_dir,
    question: str,
    draft_answer: str,
    max_rounds: int = 2,
    parallel_mode: bool = False,
    _runner=None,
) -> VerifyResult:
    """Run verifier loop up to ``max_rounds`` times.

    ``parallel_mode=True`` is reserved for v1.x and raises immediately in MVP
    (fix P1-2: explicit parameter so callers can't silently get the wrong
    behavior).

    ``_runner`` is an injection point for tests. Production callers omit it
    and get :func:`_default_runner` which raises ``NotImplementedError``;
    the real claude-fork runner will be supplied by M7-T03 scheduler wiring.
    """
    if parallel_mode:
        raise RuntimeError("parallel_mode is v1.x feature, not implemented in MVP")

    runner = _runner or _default_runner
    rounds_used = 0
    last_concerns: list = []

    for round_idx in range(1, max_rounds + 1):
        rounds_used = round_idx
        try:
            output = await runner(
                work_dir=work_dir, question=question, draft=draft_answer
            )
        except Exception as exc:  # fork failure
            return VerifyResult(
                verified=None,
                error_msg=str(exc),
                rounds_used=rounds_used,
            )

        verdict, concerns = _parse_output(output)
        if verdict == "PASS":
            return VerifyResult(verified=True, rounds_used=rounds_used)
        last_concerns = concerns
        # Note: between rounds the main agent would re-draft using these
        # concerns. For MVP we iterate the verifier with the same draft;
        # caller (M7-T03 scheduler) orchestrates the re-draft step.

    return VerifyResult(
        verified=False,
        concerns=last_concerns,
        rounds_used=rounds_used,
    )


async def _default_runner(*, work_dir, question, draft):
    """Stub runner — real claude fork wiring lands in M7-T03."""
    raise NotImplementedError(
        "verify() requires _runner to be supplied; "
        "real claude fork wiring deferred to scheduler integration (M7-T03)"
    )


def _parse_output(text: str) -> tuple:
    """Parse verifier output. Returns ``('PASS', [])`` or ``('REVISE', [...])``.

    Contract: the first non-empty line must be exactly ``PASS`` or start with
    ``REVISE`` (case-sensitive). Anything else — including prefix collisions
    like ``PASSPORT`` or ``PASSAGE`` — is treated as REVISE with the raw text
    as a concern, so the caller never silently passes unverified content.
    """
    stripped = text.strip()
    if not stripped:
        return ("REVISE", ["empty verifier output"])

    lines = stripped.splitlines()
    first = lines[0].strip().rstrip(":").strip()

    if first == "PASS":
        return ("PASS", [])
    if first == "REVISE" or first.startswith("REVISE "):
        concerns = []
        for raw in lines[1:]:
            line = raw.strip()
            if line.startswith("-") or line.startswith("*"):
                concerns.append(line.lstrip("-*").strip())
        return ("REVISE", concerns or [stripped])
    # Unparseable → REVISE with raw text as concern
    return ("REVISE", [f"unparseable verifier output: {stripped[:200]}"])
