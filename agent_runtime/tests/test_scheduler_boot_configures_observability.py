"""Scheduler boot wires observability.configure() exactly once."""
from pathlib import Path
from unittest.mock import patch

from agent_runtime import scheduler


def test_apply_observability_config_calls_configure(tmp_path: Path):
    cfg = {
        "observability": {"enabled": True, "trace_dir": str(tmp_path / "tr")},
    }
    with patch("agent_runtime.observability.configure") as mock:
        scheduler._apply_observability_config(cfg)
        mock.assert_called_once()
        kw = mock.call_args.kwargs
        assert kw["enabled"] is True
        # Path may be str or Path; just compare string forms
        assert str(kw["trace_dir"]) == str(tmp_path / "tr")


def test_apply_observability_config_defaults_when_section_missing(tmp_path: Path):
    cfg = {}  # no observability key at all (defensive)
    with patch("agent_runtime.observability.configure") as mock:
        scheduler._apply_observability_config(cfg)
        mock.assert_called_once()
        kw = mock.call_args.kwargs
        # Defaults from config loader normally fill these; but defensive
        # path should still emit some default rather than crash.
        assert kw["enabled"] is True
        assert kw["trace_dir"] is not None


def test_apply_observability_config_respects_disabled(tmp_path: Path):
    cfg = {"observability": {"enabled": False}}
    with patch("agent_runtime.observability.configure") as mock:
        scheduler._apply_observability_config(cfg)
        kw = mock.call_args.kwargs
        assert kw["enabled"] is False
