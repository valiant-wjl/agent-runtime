"""Repo sync: periodically `git pull` each project's work_dir.

Two modes (US-005):

- Legacy single-repo: ``work_dir`` itself is a git repo (work_dir/.git
  exists). One status + pull per project. Backward compatible with
  pre-US-005 configs.

- Multi-repo: ``project_cfg.repos = [{name, url}, ...]``. Each repo
  lives at ``work_dir/repos/<name>/``. Missing repos get cloned
  (--depth 50) on first sync; existing repos status+pull. Per-repo
  failures are isolated — one bad pull doesn't stop the rest.
"""

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _git_env() -> dict[str, str]:
    """Build env for git subprocess (per CLAUDE.md: clear proxy + set NO_PROXY)."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"}
    }
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env


async def _run_git(
    args: list[str], cwd: Path, timeout: int = 60
) -> tuple[int, bytes, bytes] | None:
    """Run ``git <args>`` in ``cwd`` with proxy-cleared env.

    Returns ``(returncode, stdout, stderr)``, or ``None`` if the call timed
    out (the process is killed in that case).
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        env=_git_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        return None
    return proc.returncode, stdout, stderr


async def sync_loop(
    projects: dict, interval_seconds: int = 3600, stash_dirty: bool = False
) -> None:
    """Loop: every interval, sync each project (legacy single or multi-repo)."""
    while True:
        for name, cfg in projects.items():
            work_dir = cfg.get("work_dir")
            if not work_dir:
                continue
            repos = cfg.get("repos")
            try:
                await _sync_one(
                    name, Path(work_dir), repos=repos, stash_dirty=stash_dirty
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("repo_sync: unexpected error for project=%s", name)
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


async def sync_once(
    name: str,
    work_dir: Path,
    repos: list | None = None,
    stash_dirty: bool = False,
) -> bool:
    """Public: sync a single project once. Wraps _sync_one.

    ``repos`` is the optional US-005 multi-repo config: a list of
    ``{"name": "<repo>", "url": "<git-remote>"}`` dicts. When given,
    work_dir/.git is ignored and each repo is synced under
    work_dir/repos/<name>/. When None, falls back to legacy single-repo
    semantics. ``stash_dirty`` controls dirty-tree handling (see
    ``_pull_repo``). Returns True if at least one pull/clone ran (even on
    failure); False if everything was skipped.
    """
    return await _sync_one(
        name, work_dir, repos=repos, stash_dirty=stash_dirty
    )


async def _sync_one(
    name: str,
    work_dir: Path,
    repos: list | None = None,
    stash_dirty: bool = False,
) -> bool:
    """Sync one project. See module docstring for the two modes."""
    if repos:
        return await _sync_multi(name, work_dir, repos, stash_dirty=stash_dirty)
    # Legacy single-repo path: work_dir itself is a git repo.
    if not work_dir.exists() or not (work_dir / ".git").exists():
        log.debug("repo_sync: %s not a git repo, skipping", work_dir)
        return False
    return await _pull_repo(name, work_dir, stash_dirty=stash_dirty)


async def _sync_multi(
    name: str, work_dir: Path, repos: list, stash_dirty: bool = False
) -> bool:
    """Iterate repos[]; clone missing ones, pull existing ones.

    Per-repo failures are logged but do not abort the loop. Returns True
    if at least one repo executed a clone or pull (even on failure),
    False if every entry was skipped or invalid.
    """
    base = work_dir / "repos"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("repo_sync: cannot create %s: %s", base, e)
        return False

    any_executed = False
    for r in repos:
        if not isinstance(r, dict):
            continue
        rname = r.get("name")
        url = r.get("url")
        if not rname or not url:
            log.warning(
                "repo_sync: %s skipping malformed repo entry: %r", name, r
            )
            continue
        target = base / rname
        try:
            if not (target / ".git").exists():
                executed = await _clone_repo(name, rname, url, target)
            else:
                executed = await _pull_repo(
                    f"{name}/{rname}", target, stash_dirty=stash_dirty
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "repo_sync: unexpected error syncing %s/%s", name, rname
            )
            executed = False
        any_executed = any_executed or executed
    return any_executed


async def _clone_repo(
    project_name: str, repo_name: str, url: str, target: Path
) -> bool:
    """Shallow-clone ``url`` into ``target`` (first-time sync).

    --depth 50 keeps history bounded while still allowing recent diff/log
    inspection by the agent. Returns True if the clone subprocess ran.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", "clone", "--depth", "50", url, str(target),
        env=_git_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        log.warning(
            "repo_sync: git clone timeout for %s/%s (%s)",
            project_name, repo_name, url,
        )
        return True
    if proc.returncode != 0:
        log.warning(
            "repo_sync: git clone failed for %s/%s (%s): %s",
            project_name, repo_name, url,
            stderr.decode(errors="replace")[:200],
        )
    else:
        log.info(
            "repo_sync: cloned %s/%s into %s", project_name, repo_name, target
        )
    return True


async def _pull_repo(
    name: str, work_dir: Path, stash_dirty: bool = False
) -> bool:
    """Status-check + git pull --ff-only on a single git work tree.

    Dirty-tree handling depends on ``stash_dirty``:

    - ``False`` (default): skip (and log.info) when `git status --porcelain`
      shows a dirty tree — never touch local changes.
    - ``True``: ``git stash push -u`` (incl. untracked) before the pull, then
      ``git stash pop`` after, so the repo still fast-forwards while local
      edits are preserved. A pop conflict is logged loudly; the changes stay
      retrievable via ``git stash list``.

    Then `git pull --ff-only`; non-zero -> log warning (no raise).
    Returns True if pull ran (even if failed), False if skipped.
    """
    # dirty tree check
    res = await _run_git(["status", "--porcelain"], work_dir)
    if res is None:
        log.warning("repo_sync: git status timeout for %s", name)
        return False
    rc, stdout, _ = res
    if rc != 0:
        log.warning("repo_sync: git status failed for %s", name)
        return False

    stashed = False
    if stdout.strip():
        if not stash_dirty:
            log.info("repo_sync: %s has dirty tree, skipping pull", name)
            return False
        # stash local changes (incl. untracked) so the pull can fast-forward
        sres = await _run_git(
            ["stash", "push", "-u", "-m", "repo_sync autostash"], work_dir
        )
        if sres is None or sres[0] != 0:
            log.warning("repo_sync: %s stash failed, skipping pull", name)
            return False
        stashed = True
        log.info("repo_sync: %s stashed dirty tree before pull", name)

    # pull (use --ff-only to avoid main/master branch mismatch;
    # relies on tracking branch configured in the repo)
    pres = await _run_git(["pull", "--ff-only"], work_dir)
    if pres is None:
        log.warning("repo_sync: git pull --ff-only timeout for %s", name)
    elif pres[0] != 0:
        log.warning(
            "repo_sync: git pull --ff-only failed for %s: %s",
            name,
            pres[2].decode(errors="replace")[:200],
        )
    else:
        log.info("repo_sync: %s pulled (ff-only)", name)

    # restore stashed changes
    if stashed:
        popres = await _run_git(["stash", "pop"], work_dir)
        if popres is None or popres[0] != 0:
            log.error(
                "repo_sync: %s stash pop conflict — local changes preserved "
                "in `git stash list`, resolve manually",
                name,
            )
        else:
            log.info("repo_sync: %s restored stashed changes", name)
    return True
