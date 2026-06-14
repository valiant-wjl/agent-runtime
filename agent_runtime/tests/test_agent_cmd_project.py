"""US-cmd-project-001: /agent project add|rm|list parser."""

from __future__ import annotations

from agent_runtime import agent_cmd


def test_parse_project_list():
    cmd = agent_cmd.parse_agent("/agent project list")
    assert cmd.verb == "project"
    assert cmd.sub == "list"
    assert cmd.args == []
    assert cmd.flags == {}


def test_parse_project_add_full():
    cmd = agent_cmd.parse_agent(
        '/agent project add autumn_qa /tmp/autumn --group "答疑群"',
    )
    assert cmd.verb == "project"
    assert cmd.sub == "add"
    assert cmd.args == ["autumn_qa", "/tmp/autumn"]
    assert cmd.flags == {"group": "答疑群"}


def test_parse_project_rm():
    cmd = agent_cmd.parse_agent("/agent project rm autumn_qa")
    assert cmd.verb == "project"
    assert cmd.sub == "rm"
    assert cmd.args == ["autumn_qa"]


def test_parse_project_without_sub_returns_help():
    cmd = agent_cmd.parse_agent("/agent project")
    assert cmd.verb == "_help"


def test_parse_project_unknown_sub_returns_help():
    cmd = agent_cmd.parse_agent("/agent project bogus")
    assert cmd.verb == "_help"


def test_parse_project_add_missing_name_returns_help():
    cmd = agent_cmd.parse_agent('/agent project add --group "答疑群"')
    assert cmd.verb == "_help"


def test_parse_project_add_missing_work_dir_returns_help():
    cmd = agent_cmd.parse_agent('/agent project add autumn_qa --group "答疑群"')
    assert cmd.verb == "_help"


def test_parse_project_add_missing_group_returns_help():
    cmd = agent_cmd.parse_agent("/agent project add autumn_qa /tmp/autumn")
    assert cmd.verb == "_help"


def test_parse_project_rm_missing_name_returns_help():
    cmd = agent_cmd.parse_agent("/agent project rm")
    assert cmd.verb == "_help"
