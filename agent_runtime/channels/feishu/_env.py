"""Internal helpers shared by channels/feishu components."""

import os


def build_lark_cli_env() -> dict[str, str]:
    """Build env for lark-cli subprocess.

    Per CLAUDE.md: must simultaneously unset proxy vars AND set NO_PROXY='*'.
    Just setting NO_PROXY is insufficient — some HTTP libs see both
    http_proxy and NO_PROXY='*' and behavior is inconsistent.
    """
    env = {
        k: v for k, v in os.environ.items()
        if k not in {"http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"}
    }
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    return env
