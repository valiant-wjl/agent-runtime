"""observability section: optional, defaults injected by load_config."""
import yaml
from pathlib import Path

from agent_runtime.config import load_config


def _minimal_valid_cfg() -> dict:
    """Minimum config that passes existing _validate; observability optional."""
    return {
        "version": 1,
        "channels": {"feishu": {"enabled": False}},
        "projects": {
            "p1": {
                "work_dir": "/tmp/p1",
                "admin_users": ["ou_x"],
                "read_phase": {"disallowed_tools": ["Edit", "Write", "NotebookEdit"]},
            },
        },
        "runtime": {"session_file": "/tmp/s.json"},
    }


def test_observability_defaults_when_section_absent(tmp_path: Path):
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(_minimal_valid_cfg()))
    cfg = load_config(cfg_path)
    obs = cfg["observability"]
    assert obs["enabled"] is True
    assert obs["trace_dir"] == "./.state/traces"
    assert obs["retention_months"] == 6


def test_observability_explicit_values_preserved(tmp_path: Path):
    data = _minimal_valid_cfg()
    data["observability"] = {"enabled": False, "trace_dir": "/var/tmp/x"}
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(data))
    cfg = load_config(cfg_path)
    assert cfg["observability"]["enabled"] is False
    assert cfg["observability"]["trace_dir"] == "/var/tmp/x"
    # retention_months is filled with default when missing from explicit block
    assert cfg["observability"]["retention_months"] == 6


def test_observability_partial_override(tmp_path: Path):
    """Only some keys overridden; rest get defaults."""
    data = _minimal_valid_cfg()
    data["observability"] = {"trace_dir": "./alt/traces"}
    cfg_path = tmp_path / "c.yaml"
    cfg_path.write_text(yaml.dump(data))
    cfg = load_config(cfg_path)
    assert cfg["observability"]["enabled"] is True  # default
    assert cfg["observability"]["trace_dir"] == "./alt/traces"  # explicit
    assert cfg["observability"]["retention_months"] == 6  # default
