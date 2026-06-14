"""Tests for runtime/config.py — schema validation."""

import re
from pathlib import Path

import pytest
import yaml

from agent_runtime.config import load_config, ConfigError


def _write_yaml(tmp_path, data):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True))
    return p


def test_read_phase_guard_only_gates_writes_without_downstream_control(tmp_path):
    """Policy (2026-05-25): platform writes (TCC/RDS/TCE) run directly — they
    have their own downstream controls (BOE = auto sandbox, PROD = lark-devops
    SSO). The read-phase soft-block only gates writes with NO downstream gate
    (git push). TCC publish must NOT be flagged; git push MUST be flagged.
    """
    example = Path("config.example.yaml").read_text()
    p = tmp_path / "config.yaml"
    p.write_text(example)
    cfg = load_config(p)
    patterns = cfg["projects"]["billing"]["read_phase"]["disallowed_bash_patterns"]

    # Platform writes have downstream control → run directly, not gated here.
    must_run_directly = [
        "platform-cli --site cn tcc update-config svc.module.handler entitlement_scene_config_v1 --value @/tmp/x.json",
        "platform-cli --site cn tcc deploy-config svc.module.handler entitlement_scene_config_v1",
        "bash scripts/publish.sh --env boe --scene Spark --file /tmp/spark_scene.json",
        "bash scripts/fetch_tcc.sh --env boe --scene Spark",
    ]
    for cmd in must_run_directly:
        assert not any(re.search(pat, cmd) for pat in patterns), (
            f"a disallowed_bash_pattern wrongly gates a direct-run command: {cmd!r}"
        )

    # Writes with no downstream gate still require approval.
    must_be_gated = [
        "git push origin main",
        "git push -f origin feat/x",
    ]
    for cmd in must_be_gated:
        assert any(re.search(pat, cmd) for pat in patterns), (
            f"no disallowed_bash_pattern gates ungated write: {cmd!r}"
        )


def test_load_config_happy_path(tmp_path):
    """Load the shipped config.example.yaml and verify key structure."""
    example = Path("config.example.yaml").read_text()
    p = tmp_path / "config.yaml"
    p.write_text(example)
    cfg = load_config(p)
    assert cfg["version"] == 1
    assert "feishu" in cfg["channels"]
    assert "billing" in cfg["projects"]
    assert cfg["projects"]["billing"]["admin_users"] == ["ou_REPLACE_ME"]
    assert cfg["runtime"]["session_file"].endswith("sessions.json")


def test_meta_work_dir_injected_into_each_project(tmp_path):
    """paths.meta_work_dir is copied onto every project_cfg so it flows to the
    claude_proc call sites (which only receive project_cfg, not top-level cfg)."""
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {"feishu": {"enabled": True}},
        "paths": {"meta_work_dir": "/home/u/work/agent-repos/meta"},
        "projects": {
            "example_project": {
                "work_dir": "/home/u/work/agent-repos/example_project",
                "admin_users": ["ou_x"],
                "read_phase": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"]},
            },
        },
        "runtime": {"session_file": "x/sessions.json"},
    })
    cfg = load_config(p)
    assert cfg["projects"]["example_project"]["meta_work_dir"] == (
        "/home/u/work/agent-repos/meta"
    )


def test_meta_work_dir_absent_is_tolerated(tmp_path):
    """No paths.meta_work_dir → project_cfg simply has no meta_work_dir key
    (claude_proc treats missing/None as 'no persona to inject')."""
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {"feishu": {"enabled": True}},
        "projects": {
            "x": {
                "work_dir": "/tmp",
                "admin_users": ["ou_x"],
                "read_phase": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"]},
            },
        },
        "runtime": {"session_file": "x"},
    })
    cfg = load_config(p)
    assert cfg["projects"]["x"].get("meta_work_dir") is None


def test_missing_version_raises(tmp_path):
    p = _write_yaml(tmp_path, {"channels": {}, "projects": {"x": {"work_dir": "/tmp", "admin_users": []}}, "runtime": {}})
    with pytest.raises(ConfigError, match="version"):
        load_config(p)


def test_missing_projects_raises(tmp_path):
    p = _write_yaml(tmp_path, {"version": 1, "channels": {"feishu": {}}, "runtime": {}})
    with pytest.raises(ConfigError, match="projects"):
        load_config(p)


def test_project_missing_work_dir_raises(tmp_path):
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {"feishu": {"enabled": True}},
        "projects": {"billing": {"display_name": "X", "admin_users": []}},   # 缺 work_dir
        "runtime": {"session_file": "x"},
    })
    with pytest.raises(ConfigError, match="work_dir"):
        load_config(p)


def test_project_missing_read_phase_raises(tmp_path):
    """G2 gate: missing read_phase.disallowed_tools → ConfigError."""
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {"feishu": {}},
        "projects": {"billing": {
            "work_dir": "/t", "admin_users": ["ou_x"],
            # 缺 read_phase
        }},
        "runtime": {"session_file": "x"},
    })
    with pytest.raises(ConfigError, match="read_phase"):
        load_config(p)


def test_empty_channels_raises(tmp_path):
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {},
        "projects": {"b": {"work_dir": "/t", "admin_users": []}},
        "runtime": {"session_file": "x"},
    })
    with pytest.raises(ConfigError, match="channels"):
        load_config(p)


def test_empty_projects_raises(tmp_path):
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {"feishu": {}},
        "projects": {},
        "runtime": {"session_file": "x"},
    })
    with pytest.raises(ConfigError, match="projects"):
        load_config(p)


def test_runtime_missing_session_file_raises(tmp_path):
    p = _write_yaml(tmp_path, {
        "version": 1,
        "channels": {"feishu": {}},
        "projects": {"b": {"work_dir": "/t", "admin_users": []}},
        "runtime": {},  # 缺 session_file
    })
    with pytest.raises(ConfigError, match="session_file"):
        load_config(p)


def test_version_string_type_raises(tmp_path):
    """version: '1' 带引号（字符串）也应报错."""
    p = tmp_path / "config.yaml"
    p.write_text("version: '1'\nchannels: {feishu: {}}\nprojects: {b: {work_dir: /t, admin_users: []}}\nruntime: {session_file: x}\n")
    with pytest.raises(ConfigError, match="version"):
        load_config(p)
