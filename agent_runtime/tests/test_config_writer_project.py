"""US-cfgw-project-001: add_project / remove_project read-only Q&A blocks."""

from __future__ import annotations

from pathlib import Path

import pytest
from ruamel.yaml import YAML

from agent_runtime import config_writer


SAMPLE_CONFIG = """\
version: 1
# top-level comment should survive
channels:
  feishu:
    enabled: true
projects:
  spring_billing:
    work_dir: /tmp/wd
    display_name: Spring 计费
    model: opus
    admin_users:
      - ou_admin
    chat_ids:
      - oc_existing      # keep this comment
    read_phase:
      disallowed_tools: [Edit, Write, NotebookEdit]
runtime:
  session_file: ./.state/sessions.json
"""


def _write_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_CONFIG)
    return p


def test_add_project_writes_template_fields(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/autumn",
        chat_id="oc_autumn",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    proj = data["projects"]["autumn_qa"]
    assert proj["work_dir"] == "/tmp/autumn"
    assert proj["display_name"] == "autumn_qa"  # defaults to name
    assert proj["model"] == "opus"
    assert list(proj["chat_ids"]) == ["oc_autumn"]
    assert list(proj["routing_keywords"]) == []
    assert list(proj["admin_users"]) == []
    assert proj["approval_timeout"] == 1800
    assert list(proj["read_phase"]["disallowed_tools"]) == [
        "Edit", "Write", "NotebookEdit",
    ]
    assert proj["write_phase"]["timeout"] == 600
    assert list(proj["supported_msg_types"]) == ["text", "post", "image"]
    assert "暂不支持" in proj["unsupported_msg_reply"]


def test_add_project_uses_explicit_display_name(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/autumn",
        chat_id="oc_autumn",
        display_name="秋季答疑",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    assert data["projects"]["autumn_qa"]["display_name"] == "秋季答疑"


def test_add_project_preserves_comments_and_other_projects(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/autumn",
        chat_id="oc_autumn",
        backup_dir=tmp_path / "bak",
    )
    text = cfg_path.read_text()
    assert "# top-level comment should survive" in text
    assert "# keep this comment" in text
    assert "spring_billing" in text
    assert "autumn_qa" in text


def test_add_project_is_idempotent_overwrite(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/old",
        chat_id="oc_autumn",
        backup_dir=tmp_path / "bak",
    )
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/new",
        chat_id="oc_autumn",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    assert data["projects"]["autumn_qa"]["work_dir"] == "/tmp/new"
    # No duplicate key — still a single autumn_qa entry.
    assert list(data["projects"].keys()).count("autumn_qa") == 1


def test_add_project_chat_id_conflict_raises(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    with pytest.raises(config_writer.ConfigWriteError) as exc:
        config_writer.add_project(
            cfg_path,
            name="autumn_qa",
            work_dir="/tmp/autumn",
            chat_id="oc_existing",  # already owned by spring_billing
            backup_dir=tmp_path / "bak",
        )
    assert "spring_billing" in str(exc.value)


def test_remove_project_deletes_only_target(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/autumn",
        chat_id="oc_autumn",
        backup_dir=tmp_path / "bak",
    )
    config_writer.remove_project(
        cfg_path,
        name="autumn_qa",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    assert "autumn_qa" not in data["projects"]
    assert "spring_billing" in data["projects"]


def test_remove_project_unknown_raises(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    with pytest.raises(config_writer.ProjectNotFound):
        config_writer.remove_project(
            cfg_path,
            name="does_not_exist",
            backup_dir=tmp_path / "bak",
        )


def test_add_project_passes_schema_validation(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    # add_project goes through update_config which reloads via load_config;
    # if the written block were schema-invalid, this would raise.
    config_writer.add_project(
        cfg_path,
        name="autumn_qa",
        work_dir="/tmp/autumn",
        chat_id="oc_autumn",
        backup_dir=tmp_path / "bak",
    )
    from agent_runtime.config import load_config
    cfg = load_config(cfg_path)
    assert "autumn_qa" in cfg["projects"]
