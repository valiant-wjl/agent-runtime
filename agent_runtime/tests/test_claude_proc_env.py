"""Tests for runtime/claude_proc._build_env — TZ pinning + proxy clearing."""

import os
from unittest.mock import patch

from agent_runtime.claude_proc import _build_env


def test_build_env_pins_tz_to_asia_shanghai():
    """Agent subprocess must run in Beijing time so its reply timestamps
    match the user's wall clock regardless of host TZ."""
    env = _build_env()
    assert env.get("TZ") == "Asia/Shanghai"


def test_build_env_overrides_host_tz_even_if_set():
    """If host happens to have TZ=UTC exported, we still pin to BJT."""
    with patch.dict(os.environ, {"TZ": "UTC"}, clear=False):
        env = _build_env()
    assert env["TZ"] == "Asia/Shanghai"


def test_build_env_still_clears_proxies():
    """Sanity: TZ change must not regress the proxy-clearing contract."""
    with patch.dict(
        os.environ,
        {
            "http_proxy": "http://127.0.0.1:8123",
            "https_proxy": "http://127.0.0.1:8123",
        },
        clear=False,
    ):
        env = _build_env()
    assert "http_proxy" not in env
    assert "https_proxy" not in env
    assert env["NO_PROXY"] == "*"
    assert env["no_proxy"] == "*"
