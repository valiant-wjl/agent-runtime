"""US-cfgw-001: ruamel.yaml roundtrip writer with atomic rename + .bak."""

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
    admin_users:
      - ou_admin
    chat_ids:
      - oc_existing
    read_phase:
      disallowed_tools: [Edit, Write, NotebookEdit]
runtime:
  session_file: ./.state/sessions.json
alert_resolver:
  enabled: true
  ttl_days: 14          # 14 day TTL
  retriever: keyword
  top_k: 3
  alert_chats:
    - chat_id: oc_existing
      project: spring_billing
    - chat_id: oc_keep
      project: spring_billing
"""


def _write_cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE_CONFIG)
    return p


def test_add_alert_chat_preserves_comments(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_alert_chat(
        cfg_path,
        chat_id="oc_new",
        project="spring_billing",
        backup_dir=tmp_path / "bak",
    )
    text = cfg_path.read_text()
    assert "# top-level comment should survive" in text
    assert "# 14 day TTL" in text
    assert "oc_new" in text


def test_add_alert_chat_updates_project_chat_ids(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_alert_chat(
        cfg_path,
        chat_id="oc_new",
        project="spring_billing",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    chat_ids = list(data["projects"]["spring_billing"]["chat_ids"])
    assert "oc_existing" in chat_ids
    assert "oc_new" in chat_ids
    alert_chats = list(data["alert_resolver"]["alert_chats"])
    assert any(e["chat_id"] == "oc_new" for e in alert_chats)


def test_add_alert_chat_is_idempotent(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.add_alert_chat(
        cfg_path,
        chat_id="oc_existing",
        project="spring_billing",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    chat_ids = list(data["projects"]["spring_billing"]["chat_ids"])
    assert chat_ids.count("oc_existing") == 1
    # Sample seeds 2 entries; idempotent re-add must leave count unchanged.
    assert len(data["alert_resolver"]["alert_chats"]) == 2


def test_remove_alert_chat(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    config_writer.remove_alert_chat(
        cfg_path,
        chat_id="oc_existing",
        backup_dir=tmp_path / "bak",
    )
    y = YAML(typ="rt")
    data = y.load(cfg_path)
    chat_ids = [e["chat_id"] for e in data["alert_resolver"]["alert_chats"]]
    assert "oc_existing" not in chat_ids
    assert "oc_keep" in chat_ids


def test_remove_unknown_chat_id_raises(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    with pytest.raises(config_writer.ChatIdNotFound):
        config_writer.remove_alert_chat(
            cfg_path,
            chat_id="oc_does_not_exist",
            backup_dir=tmp_path / "bak",
        )


def test_writer_creates_bak_file(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    backup_dir = tmp_path / "bak"
    config_writer.add_alert_chat(
        cfg_path, chat_id="oc_new", project="spring_billing",
        backup_dir=backup_dir,
    )
    baks = list(backup_dir.glob("config.yaml.*.bak"))
    assert len(baks) == 1


def test_writer_keeps_only_latest_5_baks(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    backup_dir = tmp_path / "bak"
    for i in range(7):
        config_writer.add_alert_chat(
            cfg_path, chat_id=f"oc_new_{i}", project="spring_billing",
            backup_dir=backup_dir,
        )
    baks = sorted(backup_dir.glob("config.yaml.*.bak"))
    assert len(baks) == 5


def test_writer_rolls_back_on_schema_failure(tmp_path):
    """If the post-write reload fails schema validation, the writer must
    restore the file from the backup so we never leave a corrupt config
    on disk."""
    cfg_path = _write_cfg(tmp_path)
    original = cfg_path.read_text()

    # Mutator that breaks schema (sets retriever to forbidden value)
    def break_schema(data):
        data["alert_resolver"]["retriever"] = "bogus"

    with pytest.raises(config_writer.ConfigWriteError):
        config_writer.update_config(
            cfg_path,
            mutator=break_schema,
            backup_dir=tmp_path / "bak",
        )
    assert cfg_path.read_text() == original


def test_lock_timeout(tmp_path, monkeypatch):
    """When the lock cannot be acquired within timeout, raise ConfigLockTimeout.

    We force the timeout to 0 and pre-hold the lock from a side fd so the
    main path observes contention immediately.
    """
    import fcntl

    cfg_path = _write_cfg(tmp_path)
    lock_path = cfg_path.with_suffix(cfg_path.suffix + ".lock")
    lock_path.touch()
    holder = open(lock_path, "w")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(config_writer.ConfigLockTimeout):
            config_writer.add_alert_chat(
                cfg_path,
                chat_id="oc_x",
                project="spring_billing",
                backup_dir=tmp_path / "bak",
                lock_timeout_s=0.1,
            )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
