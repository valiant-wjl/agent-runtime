"""Tests for scheduler._setup_health → health.configure wiring.

Covers the M9-T05 watchdog activation contract: when paths.meta_work_dir is
configured, scheduler.main() must propagate it to health.configure() so the
watchdog (ingest/backup mtime checks) actually runs in production.
"""

from pathlib import Path
from unittest.mock import patch

from agent_runtime import scheduler


def _resolver(base: Path):
    def _resolve(p):
        if not p:
            return None
        path = Path(p)
        return path if path.is_absolute() else (base / path).resolve()

    return _resolve


def test_setup_health_passes_meta_dir_when_configured(tmp_path):
    """paths.meta_work_dir set → health.configure receives meta_dir kwarg."""
    cfg = {
        "runtime": {
            "status_file": ".state/status.json",
            "status_history_file": ".state/status-history.jsonl",
        },
        "paths": {"meta_work_dir": str(tmp_path / "meta")},
    }
    with patch("agent_runtime.scheduler.health.configure") as spy:
        scheduler._setup_health(cfg, _resolver(tmp_path))

    assert spy.call_count == 1
    args, kwargs = spy.call_args
    assert args[0] == (tmp_path / ".state/status.json").resolve()
    assert kwargs["history_file"] == (tmp_path / ".state/status-history.jsonl").resolve()
    assert kwargs["meta_dir"] == (tmp_path / "meta").resolve()


def test_setup_health_meta_dir_none_when_paths_missing(tmp_path):
    """No paths section → meta_dir kwarg is None (watchdog stays dormant)."""
    cfg = {"runtime": {"status_file": ".state/status.json"}}
    with patch("agent_runtime.scheduler.health.configure") as spy:
        scheduler._setup_health(cfg, _resolver(tmp_path))

    assert spy.call_count == 1
    _, kwargs = spy.call_args
    assert kwargs["meta_dir"] is None
    assert kwargs["history_file"] is None


def test_setup_health_noop_when_status_file_missing(tmp_path):
    """No runtime.status_file → health.configure not called at all."""
    cfg = {"runtime": {}, "paths": {"meta_work_dir": str(tmp_path / "meta")}}
    with patch("agent_runtime.scheduler.health.configure") as spy:
        scheduler._setup_health(cfg, _resolver(tmp_path))

    assert spy.call_count == 0
