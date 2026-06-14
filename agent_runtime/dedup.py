"""Sliding-window message deduplication. Single-process in-memory.

Not thread-safe and not multi-loop safe. All calls must happen from a
single asyncio event loop (or otherwise externally serialized). This is
fine for MVP where runtime is single-process asyncio, but pytest-xdist
or thread pools would break state consistency.
"""

import time
from collections import OrderedDict

_seen_messages: OrderedDict[str, float] = OrderedDict()


def is_duplicate(message_id: str, window: int = 300) -> bool:
    """Return True if message_id was seen within the last `window` seconds."""
    now = time.time()
    # Evict expired entries from the front
    while _seen_messages:
        oldest_id, oldest_t = next(iter(_seen_messages.items()))
        if now - oldest_t > window:
            _seen_messages.popitem(last=False)
        else:
            break
    if message_id in _seen_messages:
        return True
    _seen_messages[message_id] = now
    return False


def reset() -> None:
    _seen_messages.clear()
