"""Beijing time (UTC+8) helpers — everything user-visible runs in BJT.

The runtime may be deployed on hosts with arbitrary system TZ (often UTC
in containers). All timestamps shown to users or written into knowledge
files must be Beijing time so the date headers / HH:MM markers match
what the user actually saw on the clock when they sent the message.

Single source of truth: import ``BJT`` and ``now_bjt`` here, do not
re-create ``timezone(timedelta(hours=8))`` inline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

BJT = timezone(timedelta(hours=8), name="Asia/Shanghai")


def now_bjt() -> datetime:
    return datetime.now(BJT)
