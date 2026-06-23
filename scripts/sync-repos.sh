#!/bin/bash
# One-shot manual repo sync (reads config.yaml, iterates projects).
#
# Usage: bash scripts/sync-repos.sh
set -euo pipefail

cd "$(dirname "$0")/.."
python3 -c "
import asyncio
import sys
from agent_runtime import config as cfg_mod, repo_sync
from pathlib import Path

cfg = cfg_mod.load_config('config.yaml')
# Honor the same dirty-tree policy the long-running sync_loop uses.
stash_dirty = bool((cfg['runtime'].get('repo_sync') or {}).get('stash_dirty', False))
async def main():
    for name, p in cfg['projects'].items():
        wd = p.get('work_dir')
        if wd:
            # Forward the project's multi-repo list so multi-repo projects
            # (work_dir/repos/<name>/) actually sync — without this the
            # legacy single-repo path sees work_dir is not a git repo and
            # skips, which silently no-ops every multi-repo project.
            await repo_sync.sync_once(name, Path(wd), repos=p.get('repos'), stash_dirty=stash_dirty)
asyncio.run(main())
"
