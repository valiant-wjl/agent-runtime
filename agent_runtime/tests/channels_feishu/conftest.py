"""Common fixtures for channels/feishu tests."""

import pytest

from agent_runtime import dedup


@pytest.fixture(autouse=True)
def _reset_dedup():
    dedup.reset()
    yield
    dedup.reset()
