"""Tests for runtime/alert_retriever.py — Sorensen-Dice keyword retrieval.

Covers (US-002):
  - Candidate dataclass shape
  - KeywordRetriever: empty kb / identical text 1.0 / disjoint 0.0 /
    top_k truncation / tie-break / expired filtered / Chinese token support
  - EmbeddingRetriever stub raises NotImplementedError with 'M2 placeholder'
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_runtime.alert_kb import AlertKB
from agent_runtime.alert_retriever import (
    Candidate,
    EmbeddingRetriever,
    KeywordRetriever,
)


def _seed(root: Path, chat_id: str, *rows: dict) -> None:
    f = root / f"{chat_id}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def _row(
    *,
    id: str,
    text: str,
    conclusion: str = "c",
    age_days: float = 0,
    status: str = "active",
) -> dict:
    now = datetime.now(timezone.utc)
    created = now - timedelta(days=age_days)
    return {
        "id": id,
        "created_at": created.isoformat(),
        "alert_text": text,
        "conclusion": conclusion,
        "source_message_id": "m",
        "status": status,
        "hit_count": 0,
        "last_hit_at": None,
    }


# --- Candidate dataclass ----------------------------------------------------


def test_candidate_dataclass_has_entry_and_score(tmp_path: Path):
    """Smoke: Candidate must be (entry, score)."""
    kb = AlertKB(tmp_path)
    e = kb.add(chat_id="oc_a", alert_text="x", conclusion="y", source_message_id="m")
    c = Candidate(entry=e, score=0.5)
    assert c.entry.id == e.id
    assert c.score == 0.5


# --- KeywordRetriever -------------------------------------------------------


def test_empty_kb_returns_empty_list(tmp_path: Path):
    kb = AlertKB(tmp_path)
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    assert r.search(chat_id="oc_a", alert_text="anything", top_k=3) == []


def test_identical_text_scores_one(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(tmp_path, "oc_a", _row(id="alert-001", text="rds timeout in spring billing"))
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="rds timeout in spring billing", top_k=3)
    assert len(out) == 1
    assert out[0].score == pytest.approx(1.0)


def test_disjoint_tokens_score_zero(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(tmp_path, "oc_a", _row(id="alert-001", text="aaa bbb ccc"))
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="zzz yyy", top_k=3)
    # Expired? no. Just no overlap.
    assert len(out) == 1
    assert out[0].score == 0.0


def test_partial_overlap_scores_between(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-001", text="rds timeout aily billing"),
        _row(id="alert-002", text="redis connection refused production"),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="rds timeout aily", top_k=3)
    # alert-001 should outrank alert-002
    assert out[0].entry.id == "alert-001"
    assert out[0].score > out[1].score


def test_top_k_truncates(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-001", text="alpha beta"),
        _row(id="alert-002", text="alpha"),
        _row(id="alert-003", text="alpha beta gamma"),
        _row(id="alert-004", text="alpha"),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="alpha beta", top_k=2)
    assert len(out) == 2


def test_tie_break_by_created_at_desc(tmp_path: Path):
    """When two entries score identically, the more recent wins."""
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-old", text="alpha", age_days=5),
        _row(id="alert-new", text="alpha", age_days=1),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="alpha", top_k=2)
    assert out[0].entry.id == "alert-new"
    assert out[1].entry.id == "alert-old"
    # Same score, ordered by recency.
    assert out[0].score == out[1].score


def test_expired_entries_excluded(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-fresh", text="alpha", age_days=1),
        _row(id="alert-old", text="alpha", age_days=20),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="alpha", top_k=3)
    assert [c.entry.id for c in out] == ["alert-fresh"]


def test_rejected_entries_excluded(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-rej", text="alpha", status="rejected"),
        _row(id="alert-ok", text="alpha"),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="alpha", top_k=3)
    assert [c.entry.id for c in out] == ["alert-ok"]


def test_chinese_unigram_matching(tmp_path: Path):
    """Chinese alerts must be searchable; tokenizer should fall back to
    char-level grams so '账单超时' overlaps with '账单超时报错'."""
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-zh", text="账单超时报错 spring_billing 服务"),
        _row(id="alert-en", text="completely unrelated english text"),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="账单超时", top_k=2)
    assert out[0].entry.id == "alert-zh"
    assert out[0].score > 0.0


def test_score_descending_order(tmp_path: Path):
    kb = AlertKB(tmp_path)
    _seed(
        tmp_path, "oc_a",
        _row(id="alert-low", text="alpha xx"),
        _row(id="alert-high", text="alpha beta"),
        _row(id="alert-mid", text="alpha beta xx yy"),
    )
    r = KeywordRetriever(kb=kb, ttl_seconds=14 * 86400)
    out = r.search(chat_id="oc_a", alert_text="alpha beta", top_k=3)
    scores = [c.score for c in out]
    assert scores == sorted(scores, reverse=True)


# --- EmbeddingRetriever stub ------------------------------------------------


def test_embedding_retriever_raises_not_implemented(tmp_path: Path):
    """M2 placeholder: must not be silently usable."""
    kb = AlertKB(tmp_path)
    with pytest.raises(NotImplementedError) as ei:
        EmbeddingRetriever(kb=kb, ttl_seconds=14 * 86400)
    assert "M2 placeholder" in str(ei.value)
