"""US-cmd-001: /alert <text> slash command parser.

Mirror of test_lesson.py for the alert resolver test entry.
"""

from __future__ import annotations

from agent_runtime import alert_cmd


# --- is_alert_command ---


def test_is_alert_command_recognizes_slash_alert():
    assert alert_cmd.is_alert_command("/alert RDS 超时 host=x-prod-01")


def test_is_alert_command_recognizes_slash_alert_no_body():
    """`/alert` with no body is still the command (parse rejects)."""
    assert alert_cmd.is_alert_command("/alert")


def test_is_alert_command_rejects_normal_text():
    assert not alert_cmd.is_alert_command("普通问题，介绍下计费链路")


def test_is_alert_command_rejects_alert_substring():
    """Don't match if /alert appears mid-sentence."""
    assert not alert_cmd.is_alert_command("我想用 /alert 来记")


def test_is_alert_command_strips_leading_whitespace():
    assert alert_cmd.is_alert_command("  /alert RDS 超时")


def test_is_alert_command_does_not_match_alert_resolver_word():
    """/alert_resolver and /alerts must not falsely match — exact /alert
    followed by EOS/whitespace/tab only."""
    assert not alert_cmd.is_alert_command("/alerts something")
    assert not alert_cmd.is_alert_command("/alert_resolver state")


# --- parse_alert ---


def test_parse_alert_extracts_content():
    assert alert_cmd.parse_alert("/alert RDS 超时 host=x-prod-01") == "RDS 超时 host=x-prod-01"


def test_parse_alert_strips_extra_whitespace():
    assert alert_cmd.parse_alert("/alert    短文本  ") == "短文本"


def test_parse_alert_empty_returns_none():
    assert alert_cmd.parse_alert("/alert") is None
    assert alert_cmd.parse_alert("/alert   ") is None


def test_parse_alert_preserves_internal_newlines():
    """Unlike /lesson, /alert keeps multi-line content — alert text is
    typically multi-line (title, fields, error detail)."""
    body = "/alert 标题\n问题：超时\n实例：x-prod-01"
    assert alert_cmd.parse_alert(body) == "标题\n问题：超时\n实例：x-prod-01"


def test_parse_alert_returns_none_for_non_command():
    assert alert_cmd.parse_alert("不是命令") is None
