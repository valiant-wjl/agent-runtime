"""US-cmd-002: /agent <subcommand> slash command parser."""

from __future__ import annotations

from agent_runtime import agent_cmd


def test_is_agent_command_recognizes_slash_agent():
    assert agent_cmd.is_agent_command("/agent show")
    assert agent_cmd.is_agent_command("/agent")
    assert agent_cmd.is_agent_command("  /agent alert list")


def test_is_agent_command_rejects_prefixed_variants():
    assert not agent_cmd.is_agent_command("/agents show")
    assert not agent_cmd.is_agent_command("/agent_admin")
    assert not agent_cmd.is_agent_command("foo /agent show")
    assert not agent_cmd.is_agent_command("")


def test_parse_show():
    cmd = agent_cmd.parse_agent("/agent show")
    assert cmd is not None
    assert cmd.verb == "show"
    assert cmd.sub is None
    assert cmd.args == []
    assert cmd.flags == {}


def test_parse_alert_list():
    cmd = agent_cmd.parse_agent("/agent alert list")
    assert cmd.verb == "alert"
    assert cmd.sub == "list"


def test_parse_alert_remove_with_chat_id():
    cmd = agent_cmd.parse_agent("/agent alert remove oc_abc123")
    assert cmd.verb == "alert"
    assert cmd.sub == "remove"
    assert cmd.args == ["oc_abc123"]


def test_parse_alert_register_with_project_flag():
    cmd = agent_cmd.parse_agent("/agent alert register --project spring_billing")
    assert cmd.verb == "alert"
    assert cmd.sub == "register"
    assert cmd.flags == {"project": "spring_billing"}


def test_parse_alert_register_without_flag():
    cmd = agent_cmd.parse_agent("/agent alert register")
    assert cmd.verb == "alert"
    assert cmd.sub == "register"
    assert cmd.flags == {}


def test_parse_empty_returns_help_sentinel():
    cmd = agent_cmd.parse_agent("/agent")
    assert cmd.verb == "_help"


def test_parse_unknown_verb_returns_help_sentinel():
    cmd = agent_cmd.parse_agent("/agent foo")
    assert cmd.verb == "_help"


def test_parse_alert_without_sub_returns_help_sentinel():
    cmd = agent_cmd.parse_agent("/agent alert")
    assert cmd.verb == "_help"


def test_parse_alert_unknown_sub_returns_help_sentinel():
    cmd = agent_cmd.parse_agent("/agent alert bogus")
    assert cmd.verb == "_help"


def test_parse_alert_remove_missing_chat_id_returns_help():
    cmd = agent_cmd.parse_agent("/agent alert remove")
    assert cmd.verb == "_help"


def test_parse_non_command_returns_none():
    assert agent_cmd.parse_agent("hi there") is None
    assert agent_cmd.parse_agent("/agents foo") is None
