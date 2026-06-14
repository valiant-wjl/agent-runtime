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
async def main():
    for name, p in cfg['projects'].items():
        wd = p.get('work_dir')
        if wd:
            await repo_sync.sync_once(name, Path(wd))
asyncio.run(main())
"
