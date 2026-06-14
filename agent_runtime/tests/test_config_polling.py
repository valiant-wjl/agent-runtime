"""US-poll-004: alert_resolver.polling sub-section validation.

The polling sub-section is optional; when present and enabled, all
fields must be sane. Tests piggyback on the existing
``test_config_alert_resolver`` style.
"""

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
            "spring_billing": {
                "work_dir": "/tmp/sb",
                "admin_users": ["ou_a"],
                "read_phase": {
                    "disallowed_tools": ["Edit", "Write", "NotebookEdit"],
                    "disallowed_bash_patterns": [],
                },
            },
        },
        "runtime": {"session_file": "./.state/sessions.json"},
        "alert_resolver": {
            "enabled": True,
            "ttl_days": 14,
            "retriever": "keyword",
            "top_k": 3,
            "alert_chats": [{"chat_id": "oc_x", "project": "spring_billing"}],
        },
    }


def _write(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(cfg))
    return p


# ---------------------------------------------------------------------------


def test_polling_section_absent_is_ok(tmp_path):
    """Polling is opt-in; absence keeps the legacy 'event-only' behaviour."""
    config.load_config(_write(tmp_path, _base_cfg()))


def test_polling_disabled_is_ok(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {"enabled": False}
    config.load_config(_write(tmp_path, cfg))


def test_polling_enabled_full_valid(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {
        "enabled": True,
        "interval_seconds": 30,
        "page_size": 20,
        "cursor_file": "./.state/poller_cursor.json",
        "cold_start": "skip_history",
        "max_initial_ingest": 30,
    }
    config.load_config(_write(tmp_path, cfg))


def test_polling_minimal_valid_uses_defaults(tmp_path):
    """Most fields have sensible defaults; only `enabled: true` is mandatory."""
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {"enabled": True}
    config.load_config(_write(tmp_path, cfg))


def test_polling_interval_seconds_must_be_positive(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {"enabled": True, "interval_seconds": 0}
    with pytest.raises(config.ConfigError, match="interval_seconds"):
        config.load_config(_write(tmp_path, cfg))


def test_polling_page_size_out_of_range(tmp_path):
    """Feishu IM API caps page_size at 50; reject overshoot early."""
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {"enabled": True, "page_size": 100}
    with pytest.raises(config.ConfigError, match="page_size"):
        config.load_config(_write(tmp_path, cfg))


def test_polling_page_size_zero_rejected(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {"enabled": True, "page_size": 0}
    with pytest.raises(config.ConfigError, match="page_size"):
        config.load_config(_write(tmp_path, cfg))


def test_polling_cold_start_unknown_value(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {
        "enabled": True, "cold_start": "future_history"
    }
    with pytest.raises(config.ConfigError, match="cold_start"):
        config.load_config(_write(tmp_path, cfg))


def test_polling_max_initial_ingest_negative(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {
        "enabled": True, "max_initial_ingest": -5
    }
    with pytest.raises(config.ConfigError, match="max_initial_ingest"):
        config.load_config(_write(tmp_path, cfg))


def test_polling_cursor_file_must_be_string(tmp_path):
    cfg = _base_cfg()
    cfg["alert_resolver"]["polling"] = {
        "enabled": True, "cursor_file": 123,
    }
    with pytest.raises(config.ConfigError, match="cursor_file"):
        config.load_config(_write(tmp_path, cfg))
