import asyncio
import pytest
from agent_runtime import concurrency


def test_global_sem_pre_init_raises():
    """Calling global_sem() before init_global() raises."""
    concurrency.reset()
    with pytest.raises((AssertionError, RuntimeError)):
        concurrency.global_sem()


@pytest.mark.asyncio
async def test_global_sem_returns_same_instance_after_init():
    concurrency.reset()
    concurrency.init_global(5)
    assert concurrency.global_sem() is concurrency.global_sem()


@pytest.mark.asyncio
async def test_chat_sem_same_id_returns_same_instance():
    concurrency.reset()
    sem_a = concurrency.chat_sem("chat-1", 2)
    sem_b = concurrency.chat_sem("chat-1", 99)  # limit 被忽略（已存在）
    assert sem_a is sem_b
    sem_c = concurrency.chat_sem("chat-2", 2)
    assert sem_a is not sem_c
