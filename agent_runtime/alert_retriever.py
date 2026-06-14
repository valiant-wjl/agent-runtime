"""Alert retrievers: pluggable candidate-ranking layer.

M1 ships KeywordRetriever (Sorensen-Dice over ASCII tokens + CJK
unigrams). EmbeddingRetriever is a stub that fails fast — its purpose is
to lock the interface shape now so the M2 swap is a configuration
change, not a refactor.

Why Sorensen-Dice and not BM25:
  - Per chat_id we expect 10s-100s of entries within the 14d TTL window
  - No need for IDF calibration at this scale
  - Symmetric, bounded in [0,1], easy to reason about
  - Claude judge is the real arbiter; retriever just narrows to top_k
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from agent_runtime.alert_kb import AlertEntry, AlertKB


@dataclass
class Candidate:
    entry: AlertEntry
    score: float


class Retriever(Protocol):
    def search(
        self, *, chat_id: str, alert_text: str, top_k: int
    ) -> list[Candidate]: ...


# ---------------------------------------------------------------------------
# Tokenizer — ASCII words + CJK unigrams
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    # Common CJK ranges; covers Chinese (sufficient for current scope).
    return (
        0x4E00 <= code <= 0x9FFF
        or 0x3400 <= code <= 0x4DBF
        or 0xF900 <= code <= 0xFAFF
    )


def _tokenize(text: str) -> set[str]:
    """Return a set of tokens: lowercased ASCII words + each CJK char.

    Set semantics (not multiset) keeps the dice score symmetric and
    matches the "do these alerts mention the same things" intuition for
    typically-short alerts. Multiset would over-weight repeated tokens
    that often appear by chance (timestamps, IDs).
    """
    tokens: set[str] = set()
    if not text:
        return tokens
    # CJK chars first as standalone unigrams.
    for ch in text:
        if _is_cjk(ch):
            tokens.add(ch)
    # ASCII words — \w would also catch CJK in Python 3 unicode mode,
    # so use an explicit ASCII-only class instead.
    for m in _WORD_RE.findall(text):
        tokens.add(m.lower())
    return tokens


def _dice(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return 2.0 * inter / (len(a) + len(b))


# ---------------------------------------------------------------------------
# KeywordRetriever
# ---------------------------------------------------------------------------


class KeywordRetriever:
    def __init__(self, *, kb: AlertKB, ttl_seconds: int) -> None:
        self.kb = kb
        self.ttl_seconds = ttl_seconds

    def search(
        self, *, chat_id: str, alert_text: str, top_k: int
    ) -> list[Candidate]:
        entries = self.kb.list_active(chat_id=chat_id, ttl_seconds=self.ttl_seconds)
        if not entries:
            return []
        q = _tokenize(alert_text)
        scored: list[Candidate] = []
        for e in entries:
            d = _tokenize(e.alert_text)
            scored.append(Candidate(entry=e, score=_dice(q, d)))
        # score desc, tie-break by created_at desc (more recent first)
        scored.sort(key=lambda c: (c.score, c.entry.created_at), reverse=True)
        return scored[:top_k]


# ---------------------------------------------------------------------------
# EmbeddingRetriever — M2 placeholder
# ---------------------------------------------------------------------------


class EmbeddingRetriever:
    """M2 placeholder. ``__init__`` always raises so no instance exists —
    the class is kept solely to lock the type / config-validation surface
    in place ahead of the embedding backend landing."""

    def __init__(self, *, kb: AlertKB, ttl_seconds: int) -> None:
        raise NotImplementedError(
            "EmbeddingRetriever is an M2 placeholder; switch alert_resolver."
            "retriever back to 'keyword' until the embedding backend lands."
        )
