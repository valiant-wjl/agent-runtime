"""Common fixtures: reset module-level state between tests."""

import importlib
import importlib.util

import pytest

_RESETABLE_MODULES = (
    "agent_runtime.dedup",
    "agent_runtime.session",
    "agent_runtime.approval",
    "agent_runtime.concurrency",
    "agent_runtime.health",  # M2-T14
)


def _reset_all() -> None:
    for mod_name in _RESETABLE_MODULES:
        if importlib.util.find_spec(mod_name) is None:
            continue  # 模块还没实现，跳过
        mod = importlib.import_module(mod_name)
        if hasattr(mod, "reset"):
            mod.reset()


@pytest.fixture(autouse=True)
def _reset_module_state():
    _reset_all()  # 测试前
    yield
    _reset_all()  # 测试后
