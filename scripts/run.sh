#!/bin/bash
# Run agent-runtime in foreground (for local dev / smoke test).
#
# Usage: bash scripts/run.sh [--config <path>]
# Default config: ./config.yaml
set -euo pipefail

cd "$(dirname "$0")/.."

# Clear proxy vars per CLAUDE.md
export http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY=
export NO_PROXY='*' no_proxy='*'

# Prefer local .venv if exists
if [ -x ".venv/bin/agent-runtime" ]; then
    exec .venv/bin/agent-runtime "$@"
else
    exec agent-runtime "$@"
fi
