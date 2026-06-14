import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agent_runtime import repo_sync


@pytest.mark.asyncio
async def test_sync_one_clean_tree_pulls(tmp_path):
    """Clean tree -> git pull executed."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    (work_dir / ".git").mkdir()

    spawn_calls = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append(args)
        mock = AsyncMock()
        # git status returns empty (clean tree)
        if "status" in args:
            mock.communicate = AsyncMock(return_value=(b"", b""))
        else:  # git pull
            mock.communicate = AsyncMock(return_value=(b"Already up to date.\n", b""))
        mock.returncode = 0
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await repo_sync._sync_one("proj1", work_dir)
    assert result is True
    # 两次 subprocess 调用：status + pull
    assert len(spawn_calls) == 2
    assert any("pull" in a for a in spawn_calls[1])


@pytest.mark.asyncio
async def test_sync_one_dirty_tree_skipped(tmp_path):
    """Dirty tree -> no git pull."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    (work_dir / ".git").mkdir()

    spawn_calls = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append(args)
        mock = AsyncMock()
        # git status returns dirty
        mock.communicate = AsyncMock(return_value=(b" M some_file.py\n", b""))
        mock.returncode = 0
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await repo_sync._sync_one("proj1", work_dir)
    assert result is False
    # 只调了 git status，没调 git pull
    assert len(spawn_calls) == 1
    assert "status" in spawn_calls[0]


@pytest.mark.asyncio
async def test_sync_one_pull_failure_not_abort(tmp_path):
    """git pull 非零退出 -> log warning，不抛异常，返回 True（pull 执行了但失败）."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    (work_dir / ".git").mkdir()

    call_count = [0]

    async def fake_spawn(*args, **kwargs):
        mock = AsyncMock()
        call_count[0] += 1
        if "status" in args:
            mock.communicate = AsyncMock(return_value=(b"", b""))
            mock.returncode = 0
        else:
            mock.communicate = AsyncMock(return_value=(b"", b"fatal: remote unreachable\n"))
            mock.returncode = 1
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        # 不抛异常
        result = await repo_sync._sync_one("proj1", work_dir)
    assert result is True  # pull 执行了（即使失败）
    assert call_count[0] == 2


# ---------------------------------------------------------------------------
# US-005: multi-repo support — projects.<name>.repos = [{name, url}, ...]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_multi_each_repo_status_and_pulls(tmp_path):
    """Two cloned repos under work_dir/repos/ → each gets its own status+pull."""
    work_dir = tmp_path / "project"
    (work_dir / "repos" / "billing").mkdir(parents=True)
    (work_dir / "repos" / "billing" / ".git").mkdir()
    (work_dir / "repos" / "app_manager").mkdir(parents=True)
    (work_dir / "repos" / "app_manager" / ".git").mkdir()

    spawn_calls = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append((args, kwargs.get("cwd", "")))
        mock = AsyncMock()
        if "status" in args:
            mock.communicate = AsyncMock(return_value=(b"", b""))
        else:
            mock.communicate = AsyncMock(return_value=(b"Already up to date.\n", b""))
        mock.returncode = 0
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await repo_sync._sync_one(
            "spring_billing",
            work_dir,
            repos=[
                {"name": "billing", "url": "git@example/billing.git"},
                {"name": "app_manager", "url": "git@example/app_manager.git"},
            ],
        )

    assert result is True
    # 4 subprocess calls: 2 (status+pull) per repo.
    assert len(spawn_calls) == 4
    pull_count = sum(1 for c in spawn_calls if "pull" in c[0])
    assert pull_count == 2
    # Each pull should be cwd-scoped to the right repo dir.
    pull_cwds = {str(c[1]) for c in spawn_calls if "pull" in c[0]}
    assert any("billing" in cwd and "app_manager" not in cwd for cwd in pull_cwds)
    assert any("app_manager" in cwd for cwd in pull_cwds)


@pytest.mark.asyncio
async def test_sync_multi_one_repo_fail_other_continues(tmp_path):
    """When one repo's pull fails (non-zero exit), the other still pulls.

    Per-repo failures must not abort the loop — that's the whole point of
    multi-repo: a flaky remote on one repo shouldn't stall the rest.
    """
    work_dir = tmp_path / "project"
    (work_dir / "repos" / "ok").mkdir(parents=True)
    (work_dir / "repos" / "ok" / ".git").mkdir()
    (work_dir / "repos" / "fail").mkdir(parents=True)
    (work_dir / "repos" / "fail" / ".git").mkdir()

    pulls_seen = []

    async def fake_spawn(*args, **kwargs):
        cwd = str(kwargs.get("cwd", ""))
        mock = AsyncMock()
        if "status" in args:
            mock.communicate = AsyncMock(return_value=(b"", b""))
            mock.returncode = 0
        else:
            pulls_seen.append(cwd)
            if "fail" in cwd:
                mock.communicate = AsyncMock(return_value=(b"", b"fatal\n"))
                mock.returncode = 1
            else:
                mock.communicate = AsyncMock(return_value=(b"OK\n", b""))
                mock.returncode = 0
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await repo_sync._sync_one(
            "spring_billing",
            work_dir,
            repos=[
                {"name": "ok", "url": "x"},
                {"name": "fail", "url": "y"},
            ],
        )

    assert result is True
    # Both repos must have attempted a pull (failure isolation).
    assert len(pulls_seen) == 2
    assert any("/ok" in c for c in pulls_seen)
    assert any("/fail" in c for c in pulls_seen)


@pytest.mark.asyncio
async def test_sync_multi_clones_missing_repo(tmp_path):
    """If a repo's .git doesn't exist yet, git clone is invoked instead of pull."""
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    # NOTE: don't pre-create work_dir/repos/billing — it's the missing case.

    spawn_calls = []

    async def fake_spawn(*args, **kwargs):
        spawn_calls.append(args)
        mock = AsyncMock()
        mock.communicate = AsyncMock(return_value=(b"Cloning into ...\n", b""))
        mock.returncode = 0
        return mock

    with patch("asyncio.create_subprocess_exec", side_effect=fake_spawn):
        result = await repo_sync._sync_one(
            "spring_billing",
            work_dir,
            repos=[{"name": "billing", "url": "git@example/billing.git"}],
        )

    assert result is True
    # Exactly one git clone call (no status/pull because there's no .git yet).
    clone_calls = [a for a in spawn_calls if "clone" in a]
    assert len(clone_calls) == 1
    assert "git@example/billing.git" in clone_calls[0]
    # The target dir must be passed (last positional after URL).
    assert any(
        "billing" in part and "repos" in part
        for part in clone_calls[0]
        if isinstance(part, str)
    )
