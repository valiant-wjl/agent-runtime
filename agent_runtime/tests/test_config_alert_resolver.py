"""Tests for runtime/config.py — alert_resolver section validation (US-006)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_runtime import config


def _base_cfg() -> dict:
    return {
        "version": 1,
        "channels": {"feishu": {"enabled": True}},
        "projects": {
            "example_project": {
                "work_dir": "/tmp/sb",
                "admin_users": ["ou_a"],
                "read_phase": {
                    "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
                    "disallowed_bash_patterns": [],
                },
            },
        },
        "runtime": {"session_file": "./.state/sessions.json"},
    }


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


# Absent: legacy configs without alert_resolver still load.


def test_config_without_alert_resolver_section_loads(tmp_path):
    cfg = _base_cfg()
    config.load_config(_write(tmp_path, cfg))


# Disabled: presence of section without enabled=True is OK.


def test_config_alert_resolver_disabled_loads(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {"enabled": False}
    config.load_config(_write(tmp_path, cfg))


# Enabled + valid: passes.


def test_config_alert_resolver_enabled_full_valid(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "top_k": 3,
        "judge_timeout": 60,
        "judge_model": "haiku",
        "alert_chats": [{"chat_id": "oc_x", "project": "example_project"}],
        "sweep": {"enabled": True, "hour": 4},
    }
    config.load_config(_write(tmp_path, cfg))


# Enabled but missing top_k.


def test_config_alert_resolver_missing_top_k_errors(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "alert_chats": [{"chat_id": "oc_x", "project": "example_project"}],
    }
    with pytest.raises(config.ConfigError, match="top_k"):
        config.load_config(_write(tmp_path, cfg))


def test_config_alert_resolver_missing_ttl_days_errors(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "retriever": "keyword",
        "top_k": 3,
        "alert_chats": [{"chat_id": "oc_x", "project": "example_project"}],
    }
    with pytest.raises(config.ConfigError, match="ttl_days"):
        config.load_config(_write(tmp_path, cfg))


# Bad retriever value.


def test_config_alert_resolver_bogus_retriever_errors(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "bogus",
        "top_k": 3,
        "alert_chats": [{"chat_id": "oc_x", "project": "example_project"}],
    }
    with pytest.raises(config.ConfigError, match="retriever"):
        config.load_config(_write(tmp_path, cfg))


# alert_chats project not in projects.


def test_config_alert_resolver_unknown_project_errors(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "top_k": 3,
        "alert_chats": [{"chat_id": "oc_x", "project": "nonexistent"}],
    }
    with pytest.raises(config.ConfigError, match="nonexistent"):
        config.load_config(_write(tmp_path, cfg))


def test_config_alert_resolver_empty_alert_chats_errors(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "top_k": 3,
        "alert_chats": [],
    }
    with pytest.raises(config.ConfigError, match="alert_chats"):
        config.load_config(_write(tmp_path, cfg))


# Sweep hour out of range.


def test_config_alert_resolver_sweep_hour_out_of_range_errors(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "top_k": 3,
        "alert_chats": [{"chat_id": "oc_x", "project": "example_project"}],
        "sweep": {"enabled": True, "hour": 24},
    }
    with pytest.raises(config.ConfigError, match="sweep.hour"):
        config.load_config(_write(tmp_path, cfg))


def test_config_alert_resolver_sweep_section_optional(tmp_path):
    """When sweep section is absent under alert_resolver, defaults apply."""
    cfg = _base_cfg()
    cfg["alert_resolver"] = {
        "enabled": True,
        "ttl_days": 14,
        "retriever": "keyword",
        "top_k": 3,
        "alert_chats": [{"chat_id": "oc_x", "project": "example_project"}],
    }
    config.load_config(_write(tmp_path, cfg))
