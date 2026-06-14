"""Global and per-chat semaphore holders. Caller-managed lifetimes."""

import asyncio

_global_sem: asyncio.Semaphore | None = None
_chat_sems: dict[str, asyncio.Semaphore] = {}


def init_global(limit: int) -> None:
    global _global_sem
    _global_sem = asyncio.Semaphore(limit)


def global_sem() -> asyncio.Semaphore:
    if _global_sem is None:
        raise RuntimeError("call init_global() first")
    return _global_sem


def chat_sem(chat_id: str, limit: int) -> asyncio.Semaphore:
    if chat_id not in _chat_sems:
        _chat_sems[chat_id] = asyncio.Semaphore(limit)
    return _chat_sems[chat_id]


def reset() -> None:
    global _global_sem
    _global_sem = None
    _chat_sems.clear()
