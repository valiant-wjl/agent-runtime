"""Safe ruamel.yaml roundtrip writer for ``config.yaml``.

Scope: ``alert_resolver`` section, synchronised
``projects.<name>.chat_ids``, and add/remove of whole ``projects.<name>``
read-only Q&A blocks. Refuses to be a general-purpose YAML editor to keep
the blast radius small.

Guarantees:
  - Comments and key order preserved (ruamel.yaml roundtrip mode)
  - Atomic replace via os.rename of a tmp file
  - Pre-write .bak (timestamped) in ``backup_dir``; latest 5 retained
  - fcntl LOCK_EX serialises concurrent writers (5 s default timeout)
  - Post-write schema validation; rollback from .bak on failure

Public API:
  add_alert_chat(cfg, chat_id, project, *, backup_dir, lock_timeout_s=5)
  remove_alert_chat(cfg, chat_id, *, backup_dir, lock_timeout_s=5)
  add_project(cfg, *, name, work_dir, chat_id, display_name=None,
              backup_dir, lock_timeout_s=5)
  remove_project(cfg, *, name, backup_dir, lock_timeout_s=5)
  update_config(cfg, *, mutator, backup_dir, lock_timeout_s=5)  # power-user
"""

from __future__ import annotations

import fcntl
import logging
import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from agent_runtime.config import ConfigError, load_config

log = logging.getLogger(__name__)


_MAX_BAKS = 5


class ConfigWriteError(Exception):
    """Writer failed; file restored from .bak."""


class ConfigLockTimeout(ConfigWriteError):
    """Could not acquire the config lock within the timeout."""


class ChatIdNotFound(ConfigWriteError):
    """Requested chat_id is not present in alert_chats."""


class ProjectNotFound(ConfigWriteError):
    """Requested project name is not present in the projects map."""


@contextmanager
def _config_lock(cfg_path: Path, timeout_s: float):
    # We poll fcntl with LOCK_NB rather than blocking on bare LOCK_EX
    # because flock has no native timeout option short of SIGALRM hacks.
    # 50 ms poll keeps wake-up latency tight while staying cheap for the
    # uncontended case (one attempt + immediate succeed).
    lock_path = cfg_path.with_suffix(cfg_path.suffix + ".lock")
    lock_path.touch(exist_ok=True)
    fd = open(lock_path, "w")
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ConfigLockTimeout(
                        f"could not lock {lock_path} within {timeout_s}s",
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            fd.close()


def _make_bak(cfg_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    bak = backup_dir / f"{cfg_path.name}.{stamp}.bak"
    shutil.copy2(cfg_path, bak)
    # Rotate: keep newest _MAX_BAKS
    existing = sorted(backup_dir.glob(f"{cfg_path.name}.*.bak"))
    for old in existing[:-_MAX_BAKS]:
        try:
            old.unlink()
        except OSError:
            log.warning("config_writer: failed to prune old bak %s", old)
    return bak


def _atomic_dump(cfg_path: Path, data) -> None:
    """Write `data` to a tmp file, fsync it + the parent dir, then rename.

    Durability discipline: a crash between the data write and the rename
    leaves the original config intact (rename is atomic on POSIX). A
    crash after rename but before the parent-directory fsync would let
    the new file's *contents* survive but the rename itself disappear —
    that's why we fsync the parent inode too.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    tmp = cfg_path.with_suffix(cfg_path.suffix + ".tmp")

    # Write + fsync on the writing fd so dirty pages are flushed before rename.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", closefd=False) as f:
            yaml.dump(data, f)
            f.flush()
        os.fsync(fd)
    finally:
        os.close(fd)

    os.rename(tmp, cfg_path)

    # fsync the parent directory so the rename entry is durable.
    dir_fd = os.open(str(cfg_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def update_config(
    cfg_path: Path,
    *,
    mutator: Callable[[dict], None],
    backup_dir: Path,
    lock_timeout_s: float = 5.0,
) -> None:
    """Apply ``mutator`` to the parsed YAML tree and atomically rewrite.

    On schema-validation failure post-rewrite, restore from .bak and
    raise ConfigWriteError.
    """
    cfg_path = Path(cfg_path)
    backup_dir = Path(backup_dir)
    with _config_lock(cfg_path, lock_timeout_s):
        bak = _make_bak(cfg_path, backup_dir)
        yaml = YAML(typ="rt")
        yaml.preserve_quotes = True
        with cfg_path.open() as f:
            data = yaml.load(f)
        mutator(data)
        _atomic_dump(cfg_path, data)
        try:
            load_config(cfg_path)
        except ConfigError as e:
            shutil.copy2(bak, cfg_path)
            raise ConfigWriteError(f"schema validation failed: {e}") from e


def add_alert_chat(
    cfg_path: Path,
    *,
    chat_id: str,
    project: str,
    backup_dir: Path,
    lock_timeout_s: float = 5.0,
) -> None:
    def mutator(data):
        alert = data.setdefault("alert_resolver", {})
        chats = alert.setdefault("alert_chats", [])
        for entry in chats:
            if entry.get("chat_id") == chat_id:
                return  # idempotent
        chats.append({"chat_id": chat_id, "project": project})
        proj = data.get("projects", {}).get(project)
        if proj is None:
            raise ConfigWriteError(f"project {project!r} not in projects map")
        chat_ids = proj.setdefault("chat_ids", [])
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)

    update_config(
        cfg_path, mutator=mutator, backup_dir=backup_dir,
        lock_timeout_s=lock_timeout_s,
    )


def _flow_seq(items: list) -> CommentedSeq:
    """A CommentedSeq rendered inline ([a, b, c]) to match the read-only
    Q&A template's compact look (e.g. disallowed_tools / chat_ids)."""
    seq = CommentedSeq(items)
    seq.fa.set_flow_style()
    return seq


def add_project(
    cfg_path: Path,
    *,
    name: str,
    work_dir: str,
    chat_id: str,
    display_name: str | None = None,
    backup_dir: Path,
    lock_timeout_s: float = 5.0,
) -> None:
    """Add (or overwrite) a read-only Q&A project block under projects.<name>.

    Idempotent on name: re-adding an existing name replaces its block.
    Uniqueness: if ``chat_id`` already appears in ANOTHER project's
    chat_ids, raises ConfigWriteError naming the occupant.

    NOTE: the field defaults below (model / read_phase / write_phase /
    supported_msg_types / unsupported_msg_reply) hardcode the read-only Q&A
    template. They MUST stay in sync with the project schema in
    templates/project/ and config.example.yaml; if those defaults evolve,
    update here too (this is a second source of truth).
    """
    def mutator(data):
        projects = data.setdefault("projects", CommentedMap())
        for other_name, other in projects.items():
            if other_name == name:
                continue
            if chat_id in (other.get("chat_ids") or []):
                raise ConfigWriteError(
                    f"chat_id {chat_id!r} already used by project "
                    f"{other_name!r}",
                )
        block = CommentedMap()
        block["work_dir"] = work_dir
        block["display_name"] = display_name or name
        block["model"] = "opus"
        block["chat_ids"] = _flow_seq([chat_id])
        block["routing_keywords"] = _flow_seq([])
        block["admin_users"] = _flow_seq([])
        block["approval_timeout"] = 1800
        read_phase = CommentedMap()
        read_phase["disallowed_tools"] = _flow_seq(
            ["Edit", "Write", "NotebookEdit"],
        )
        block["read_phase"] = read_phase
        write_phase = CommentedMap()
        write_phase["timeout"] = 600
        block["write_phase"] = write_phase
        block["supported_msg_types"] = _flow_seq(["text", "post", "image"])
        block["unsupported_msg_reply"] = (
            "暂不支持该类型消息, 请用文字或图片描述问题"
        )
        projects[name] = block

    update_config(
        cfg_path, mutator=mutator, backup_dir=backup_dir,
        lock_timeout_s=lock_timeout_s,
    )


def remove_project(
    cfg_path: Path,
    *,
    name: str,
    backup_dir: Path,
    lock_timeout_s: float = 5.0,
) -> None:
    """Delete projects.<name>. Raises ProjectNotFound when absent.

    Does NOT touch any other section (alert_chats etc.) — narrow blast
    radius, same as remove_alert_chat.
    """
    def mutator(data):
        projects = data.get("projects") or {}
        if name not in projects:
            raise ProjectNotFound(name)
        del projects[name]

    update_config(
        cfg_path, mutator=mutator, backup_dir=backup_dir,
        lock_timeout_s=lock_timeout_s,
    )


def remove_alert_chat(
    cfg_path: Path,
    *,
    chat_id: str,
    backup_dir: Path,
    lock_timeout_s: float = 5.0,
) -> None:
    def mutator(data):
        alert = data.get("alert_resolver") or {}
        chats = alert.get("alert_chats") or []
        idx = next(
            (i for i, e in enumerate(chats) if e.get("chat_id") == chat_id),
            None,
        )
        if idx is None:
            raise ChatIdNotFound(chat_id)
        del chats[idx]
        # NOTE: we do NOT prune projects.<name>.chat_ids on remove — that
        # list is also the routing whitelist; removing it could surprise
        # admins by silently dropping @-mention routing for that chat.
        # An explicit /agent route remove command can prune it later.

    update_config(
        cfg_path, mutator=mutator, backup_dir=backup_dir,
        lock_timeout_s=lock_timeout_s,
    )
