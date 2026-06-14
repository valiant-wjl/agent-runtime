"""config.yaml loader + minimal schema sanity checks.

Checks only presence of required top-level sections and a few critical
fields (version/work_dir/admin_users/session_file). Does NOT validate
field types beyond `version`, nor does it validate business semantics
(e.g., REPLACE_ME placeholders). Those are bootstrap.sh's responsibility.
"""

from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when config.yaml fails schema checks."""


def load_config(path: str | Path) -> dict[str, Any]:
    """Load config.yaml and run minimal schema validation."""
    text = Path(path).read_text()
    cfg = yaml.safe_load(text) or {}
    _validate(cfg)
    _apply_observability_defaults(cfg)
    _propagate_meta_work_dir(cfg)
    return cfg


def _propagate_meta_work_dir(cfg: dict) -> None:
    """Copy ``paths.meta_work_dir`` onto every project_cfg.

    WHY: the scheduler hands ``project_cfg`` (not the top-level cfg) to
    ``claude_proc.run`` / ``run_stream``. The persona contract (SOUL.md +
    USER.md) lives in the shared meta dir, which Claude Code never auto-loads
    because the bot's cwd is the project work_dir (a sibling of meta). Stamping
    meta_work_dir onto each project lets claude_proc read + inject the persona
    via --append-system-prompt, decoupled from the project dir layout (which
    may change). Absent paths.meta_work_dir → no key added (treated as 'no
    persona to inject' downstream)."""
    meta_work_dir = (cfg.get("paths") or {}).get("meta_work_dir")
    if not meta_work_dir:
        return
    for proj in (cfg.get("projects") or {}).values():
        proj.setdefault("meta_work_dir", meta_work_dir)


def _apply_observability_defaults(cfg: dict) -> None:
    """Inject observer-friendly defaults; spec § 7.1.

    The section is fully optional. enabled=true by default means trace
    emission is on out of the box; users disable explicitly if they need
    to silence the daemon.
    """
    obs = cfg.setdefault("observability", {})
    obs.setdefault("enabled", True)
    obs.setdefault("trace_dir", "./.state/traces")
    obs.setdefault("retention_months", 6)


def _validate(cfg: dict) -> None:
    if cfg.get("version") != 1:
        got_type = type(cfg.get("version")).__name__
        raise ConfigError(
            f"missing or invalid `version` (must be integer 1, got {got_type}: {cfg.get('version')!r})"
        )
    for required in ("channels", "projects", "runtime"):
        if required not in cfg:
            raise ConfigError(f"missing top-level `{required}`")
    if not cfg.get("channels"):
        raise ConfigError("`channels` must contain at least one channel entry")
    projects = cfg.get("projects") or {}
    if not projects:
        raise ConfigError("`projects` must contain at least one entry")
    if "session_file" not in cfg["runtime"]:
        raise ConfigError("`runtime.session_file` is required (path for session persistence)")
    for name, proj in projects.items():
        if "work_dir" not in proj:
            raise ConfigError(f"project `{name}` missing `work_dir`")
        if "admin_users" not in proj:
            raise ConfigError(f"project `{name}` missing `admin_users`")
        # G2 gate: read_phase.disallowed_tools is mandatory to prevent accidental
        # write access during the read phase (MVP minimum: [Edit, Write, NotebookEdit])
        read_phase = proj.get("read_phase")
        if not read_phase or not read_phase.get("disallowed_tools"):
            raise ConfigError(
                f"project `{name}` missing `read_phase.disallowed_tools` "
                "(G2 gate: MVP requires at least [Edit, Write, NotebookEdit])"
            )
    _validate_alert_resolver(cfg.get("alert_resolver"), projects)


_ALLOWED_RETRIEVERS = ("keyword", "embedding")


def _validate_alert_resolver(alert_cfg: dict | None, projects: dict) -> None:
    """alert_resolver is optional; when enabled, required fields must be sane.

    Schema (when enabled=True):
      ttl_days: positive int
      retriever: 'keyword' | 'embedding'
      top_k: positive int
      alert_chats: non-empty list[{chat_id: str, project: str}], each
        project must exist in `projects`
      sweep (optional): {enabled: bool, hour: int in [0,23]}
    """
    if not alert_cfg:
        return
    if not alert_cfg.get("enabled"):
        return

    ttl = alert_cfg.get("ttl_days")
    if not isinstance(ttl, int) or ttl <= 0:
        raise ConfigError(
            f"alert_resolver.ttl_days must be positive int, got {ttl!r}"
        )

    retriever = alert_cfg.get("retriever")
    if retriever not in _ALLOWED_RETRIEVERS:
        raise ConfigError(
            f"alert_resolver.retriever must be one of {_ALLOWED_RETRIEVERS}, "
            f"got {retriever!r}"
        )

    top_k = alert_cfg.get("top_k")
    if not isinstance(top_k, int) or top_k <= 0:
        raise ConfigError(
            f"alert_resolver.top_k must be positive int, got {top_k!r}"
        )

    chats = alert_cfg.get("alert_chats")
    if not isinstance(chats, list) or not chats:
        raise ConfigError(
            "alert_resolver.alert_chats must be non-empty list of "
            "{chat_id, project} entries"
        )
    for i, entry in enumerate(chats):
        if not isinstance(entry, dict):
            raise ConfigError(
                f"alert_resolver.alert_chats[{i}] must be a dict"
            )
        if not entry.get("chat_id"):
            raise ConfigError(
                f"alert_resolver.alert_chats[{i}] missing `chat_id`"
            )
        proj_name = entry.get("project")
        if not proj_name:
            raise ConfigError(
                f"alert_resolver.alert_chats[{i}] missing `project`"
            )
        if proj_name not in projects:
            raise ConfigError(
                f"alert_resolver.alert_chats[{i}].project={proj_name!r} "
                f"not found in projects map"
            )

    sweep = alert_cfg.get("sweep")
    if sweep is not None:
        if not isinstance(sweep, dict):
            raise ConfigError("alert_resolver.sweep must be a dict")
        if sweep.get("enabled"):
            hour = sweep.get("hour", 4)
            if not isinstance(hour, int) or not 0 <= hour <= 23:
                raise ConfigError(
                    f"alert_resolver.sweep.hour must be int in [0,23], "
                    f"got {hour!r}"
                )

    _validate_polling(alert_cfg.get("polling"))


_ALLOWED_COLD_STARTS = ("skip_history", "last_24h")
# Feishu IM messages-list API caps page_size at 50.
_MAX_PAGE_SIZE = 50


def _validate_polling(polling: dict | None) -> None:
    """alert_resolver.polling — incoming-webhook fallback. Optional; when
    enabled, fields are validated. Defaults applied at use site, not
    here, so missing fields are not errors."""
    if not polling:
        return
    if not isinstance(polling, dict):
        raise ConfigError("alert_resolver.polling must be a dict")
    if not polling.get("enabled"):
        return

    if "interval_seconds" in polling:
        v = polling["interval_seconds"]
        if not isinstance(v, int) or v <= 0:
            raise ConfigError(
                f"alert_resolver.polling.interval_seconds must be positive int, "
                f"got {v!r}"
            )

    if "page_size" in polling:
        v = polling["page_size"]
        if not isinstance(v, int) or not 1 <= v <= _MAX_PAGE_SIZE:
            raise ConfigError(
                f"alert_resolver.polling.page_size must be int in "
                f"[1, {_MAX_PAGE_SIZE}], got {v!r}"
            )

    if "cursor_file" in polling:
        v = polling["cursor_file"]
        if not isinstance(v, str) or not v:
            raise ConfigError(
                f"alert_resolver.polling.cursor_file must be non-empty str, "
                f"got {v!r}"
            )

    if "cold_start" in polling:
        v = polling["cold_start"]
        if v not in _ALLOWED_COLD_STARTS:
            raise ConfigError(
                f"alert_resolver.polling.cold_start must be one of "
                f"{_ALLOWED_COLD_STARTS}, got {v!r}"
            )

    if "max_initial_ingest" in polling:
        v = polling["max_initial_ingest"]
        if not isinstance(v, int) or v <= 0:
            raise ConfigError(
                f"alert_resolver.polling.max_initial_ingest must be positive int, "
                f"got {v!r}"
            )
