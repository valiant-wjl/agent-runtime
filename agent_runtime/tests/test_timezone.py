"""Tests for runtime/timezone.py — BJT (UTC+8) helpers."""

from datetime import datetime, timedelta, timezone

from agent_runtime.timezone import BJT, now_bjt


def test_bjt_is_utc_plus_8():
    assert BJT.utcoffset(None) == timedelta(hours=8)


def test_now_bjt_is_aware_and_offset_matches_utc():
    """now_bjt() should be tz-aware and 8 hours ahead of UTC at the same instant."""
    bjt = now_bjt()
    utc = datetime.now(timezone.utc)
    assert bjt.tzinfo is not None
    delta = abs((bjt - utc).total_seconds())
    assert delta < 2, f"now_bjt vs utc drifted by {delta}s"
    assert bjt.hour == (utc.hour + 8) % 24
