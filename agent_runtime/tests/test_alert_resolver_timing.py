"""US-001 follow-up: alert_resolver self-instruments retriever_ms / judge_ms
so production turn_summary 'branch=unknown is_alert=true total_ms=8000+'
rows are attributable to the right phase.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_runtime.channels import ParsedMsg
from agent_runtime import alert_resolver
from agent_runtime.alert_judge import JudgeResult
from agent_runtime.alert_kb import AlertEntry
from agent_runtime.alert_retriever import Candidate


def _parsed() -> ParsedMsg:
    return ParsedMsg(
        channel="feishu", message_id="om_x", thread_root_id="t_x",
        chat_id="oc_alert", sender_id="ou_bot", sender_name="bot",
        text="alert payload", mentions=[], sender_type="app",
    )


_PROJECT_CFG = {"work_dir": "/tmp/work", "model": "opus"}
_ALERT_CFG = {
    "enabled": True, "top_k": 3, "judge_timeout": 60, "judge_model": "haiku",
}


def _entry(id_: str = "alert-2026-05-01-001") -> AlertEntry:
    return AlertEntry(
        id=id_,
        created_at="2026-05-01T00:00:00Z",
        alert_text="prior alert text",
        conclusion="prior conclusion",
        source_message_id="om_prior",
        status="active",
        hit_count=0,
        last_hit_at=None,
    )


def _find_log(caplog, tag: str) -> dict[str, str] | None:
    matches = [r for r in caplog.records if r.levelno >= logging.INFO and tag in r.getMessage()]
    if not matches:
        return None
    msg = matches[-1].getMessage()
    fields: dict[str, str] = {}
    for tok in msg.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            fields[k] = v
    return fields


@pytest.mark.asyncio
async def test_no_candidates_emits_retriever_only(caplog):
    """Empty retriever results → alert_retriever outcome=no_candidates,
    and judge phase is NOT logged (we never called judge_fn)."""
    retriever = MagicMock()
    retriever.search.return_value = []
    kb = MagicMock()
    channel = AsyncMock()

    caplog.set_level(logging.INFO, logger="agent_runtime.alert_resolver")
    hit = await alert_resolver.try_handle_alert_hit(
        _parsed(), _PROJECT_CFG, _ALERT_CFG, channel, kb=kb, retriever=retriever,
    )
    assert hit is False
    f = _find_log(caplog, "alert_retriever")
    assert f is not None
    assert f.get("outcome") == "no_candidates", f
    assert int(f.get("candidate_count", "-1")) == 0
    assert int(f.get("elapsed_ms", "-2")) >= 0
    # No judge phase emitted because we never reached judge.
    assert _find_log(caplog, "alert_judge_phase") is None


@pytest.mark.asyncio
async def test_judge_hit_emits_both_phases(caplog):
    e = _entry()
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=e, score=0.9)]
    kb = MagicMock()
    channel = AsyncMock()

    judge_fn = AsyncMock(return_value=JudgeResult(
        is_match=True, matched_id=e.id, reason="same class",
        rewritten_conclusion="recycled conclusion",
    ))

    caplog.set_level(logging.INFO, logger="agent_runtime.alert_resolver")
    hit = await alert_resolver.try_handle_alert_hit(
        _parsed(), _PROJECT_CFG, _ALERT_CFG, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert hit is True
    fr = _find_log(caplog, "alert_retriever")
    fj = _find_log(caplog, "alert_judge_phase")
    assert fr and fr.get("outcome") == "ok", fr
    assert int(fr.get("candidate_count", "-1")) == 1
    assert fj and fj.get("outcome") == "hit", fj
    assert int(fj.get("elapsed_ms", "-2")) >= 0


@pytest.mark.asyncio
async def test_judge_miss_emits_outcome_miss(caplog):
    e = _entry()
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=e, score=0.4)]
    kb = MagicMock()
    channel = AsyncMock()
    judge_fn = AsyncMock(return_value=JudgeResult(
        is_match=False, matched_id=None, reason="different class",
        rewritten_conclusion="",
    ))

    caplog.set_level(logging.INFO, logger="agent_runtime.alert_resolver")
    hit = await alert_resolver.try_handle_alert_hit(
        _parsed(), _PROJECT_CFG, _ALERT_CFG, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert hit is False
    fj = _find_log(caplog, "alert_judge_phase")
    assert fj and fj.get("outcome") == "miss", fj


@pytest.mark.asyncio
async def test_judge_crash_emits_outcome_crash(caplog):
    e = _entry()
    retriever = MagicMock()
    retriever.search.return_value = [Candidate(entry=e, score=0.7)]
    kb = MagicMock()
    channel = AsyncMock()
    judge_fn = AsyncMock(side_effect=RuntimeError("judge blew up"))

    caplog.set_level(logging.INFO, logger="agent_runtime.alert_resolver")
    hit = await alert_resolver.try_handle_alert_hit(
        _parsed(), _PROJECT_CFG, _ALERT_CFG, channel,
        kb=kb, retriever=retriever, judge_fn=judge_fn,
    )
    assert hit is False
    fj = _find_log(caplog, "alert_judge_phase")
    assert fj and fj.get("outcome") == "crash", fj
