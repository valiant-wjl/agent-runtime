"""US-poll-001: normalize_card converts feishu interactive card body.content
JSON into a single alert-text string.

Generic contract: depends only on the feishu standard schema
(`{title, elements: [[{tag, text|...}, ...]]}`); does NOT special-case
Aily-specific field names. The same normalizer must work for any other
incoming-webhook source whose payload is a plain interactive card.
"""

from __future__ import annotations

import json
from pathlib import Path


from agent_runtime.channels.feishu.poller import normalize_card

_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "alert_cards"


def _load_card_body(name: str) -> str:
    """Load the body.content JSON string from a saved messages-list fixture."""
    raw = json.loads((_FIXTURE_DIR / name).read_text())
    item = raw["data"]["items"][0]
    return item["body"]["content"]


# ---------------------------------------------------------------------------
# Real Aily fixture
# ---------------------------------------------------------------------------


def test_normalize_real_aily_card_keeps_title_and_key_phrases():
    body = _load_card_body("aily_billing_alert_full.json")
    out = normalize_card(body)

    # Title must be retained (alert_text first line).
    assert out.startswith("Aily商业化报警通知")
    # Key alarm phrases — these are what retriever indexes on.
    for phrase in (
        "问题场景",
        "权益用量变更",
        "环境",
        "online",
        "错误描述",
        "同步权益用量失败",
        "错误详情",
        "invalid connection",
    ):
        assert phrase in out, f"expected {phrase!r} in normalized text:\n{out}"


def test_normalize_real_aily_card_no_extra_whitespace_runs():
    """Multiple consecutive blank lines should collapse — the alert_text has
    a 8KB cap downstream, and Aily emits lots of leading-spaces."""
    body = _load_card_body("aily_billing_alert_full.json")
    out = normalize_card(body)

    # No 3+ consecutive newlines.
    assert "\n\n\n" not in out
    # No trailing whitespace on any line.
    for line in out.splitlines():
        assert line == line.rstrip(), f"trailing whitespace on: {line!r}"


def test_normalize_real_aily_card_under_cap():
    """The alert_text bound (judge truncates to 2000 chars) must not be
    blown up by an over-eager normalizer; real Aily card under 1KB."""
    body = _load_card_body("aily_billing_alert_full.json")
    out = normalize_card(body)
    assert len(out) <= 2000


# ---------------------------------------------------------------------------
# Synthetic schema cases
# ---------------------------------------------------------------------------


def _card(title: str | None, elements: list[list[dict]]) -> str:
    obj: dict = {"elements": elements}
    if title is not None:
        obj["title"] = title
    return json.dumps(obj, ensure_ascii=False)


def test_normalize_simple_two_row_card():
    body = _card("alert", [
        [{"tag": "text", "text": "host: "}, {"tag": "text", "text": "x-prod-01"}],
        [{"tag": "text", "text": "metric: "}, {"tag": "text", "text": "rds.timeout"}],
    ])
    out = normalize_card(body)
    assert "alert" in out
    assert "host: x-prod-01" in out
    assert "metric: rds.timeout" in out


def test_normalize_skips_unknown_tags():
    """The card may carry tags this normalizer doesn't recognise (img, action,
    button, ...). Drop them silently rather than crash."""
    body = _card("hi", [
        [{"tag": "text", "text": "ok"}, {"tag": "img", "image_key": "img_xxx"}],
        [{"tag": "action", "actions": [{"tag": "button"}]}],
        [{"tag": "text", "text": "trailing"}],
    ])
    out = normalize_card(body)
    assert "ok" in out
    assert "trailing" in out
    # No raw schema leakage.
    assert "image_key" not in out
    assert "img_xxx" not in out


def test_normalize_renders_hr_as_separator():
    body = _card(None, [
        [{"tag": "text", "text": "before"}],
        [{"tag": "hr"}],
        [{"tag": "text", "text": "after"}],
    ])
    out = normalize_card(body)
    assert "before" in out and "after" in out
    # hr must NOT collapse the two halves into one line.
    assert out.index("before") < out.index("after")


def test_normalize_no_title_keeps_body():
    body = _card(None, [[{"tag": "text", "text": "anonymous alert"}]])
    out = normalize_card(body)
    assert "anonymous alert" in out


def test_normalize_empty_elements_returns_just_title_or_empty():
    """Pathological: card with no body. Don't crash; return whatever
    title/sentinel — caller can decide whether to skip."""
    out = normalize_card(_card("only-title", []))
    assert "only-title" in out

    out = normalize_card(_card(None, []))
    # Empty card → empty string (caller will skip).
    assert out.strip() == ""


def test_normalize_invalid_json_returns_empty():
    """body.content from a non-card message (or hand-edited bad JSON) must
    not crash — log and degrade to empty so caller skips this message."""
    assert normalize_card("NOT JSON {") == ""
    assert normalize_card("") == ""


def test_normalize_non_object_root_returns_empty():
    """body.content might be a string for non-card msg_types; defend."""
    assert normalize_card(json.dumps(["a", "b"])) == ""
    assert normalize_card(json.dumps("plain string")) == ""


# ---------------------------------------------------------------------------
# Generic-source spirit check
# ---------------------------------------------------------------------------


def test_normalize_does_not_special_case_aily_field_names():
    """Constructed card with NO 'aPaaS' / 'Aily' / 'tenant' fields — the
    normalizer must still produce a usable alert_text. This is the
    generic-source contract: any feishu interactive card works, not just
    Aily's specific layout."""
    body = _card("Generic alert source B", [
        [{"tag": "text", "text": "service: "}, {"tag": "text", "text": "billing-svc"}],
        [{"tag": "text", "text": "incident: "}, {"tag": "text", "text": "queue depth > 1000"}],
    ])
    out = normalize_card(body)
    assert "Generic alert source B" in out
    assert "billing-svc" in out
    assert "queue depth > 1000" in out
