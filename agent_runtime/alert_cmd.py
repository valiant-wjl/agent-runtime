"""`/alert <content>` slash command — alert resolver test entry.

Lets a developer try the alert resolver path from any chat (DM included)
without needing a real Aily webhook to fire. Behaviour:
  - /alert <body>  → run retriever + judge against the alert kb of the
    project handling this conversation, reply with a debug-prefixed
    rewritten conclusion on hit, or a debug message explaining miss.
  - The command is a TEST entry. It does NOT mark_hit, does NOT sink to
    kb, and does NOT trigger deep investigation — it shows what the
    resolver would do without burning Claude quota or polluting kb.

Why a separate file (vs reusing lesson.py): the body parsing rules are
different (lesson collapses newlines for one-line markdown entries;
alert keeps newlines so multi-line cards stay structured for retriever
tokenisation).
"""

from __future__ import annotations

_PREFIX = "/alert"


def is_alert_command(text: str) -> bool:
    """Return True iff `text` starts (after leading whitespace) with
    exactly `/alert` followed by EOS, space, or tab. Avoids false matches
    on `/alerts`, `/alert_resolver`, or substrings mid-sentence.
    """
    if not text:
        return False
    stripped = text.lstrip()
    if stripped == _PREFIX:
        return True
    return stripped.startswith(_PREFIX + " ") or stripped.startswith(_PREFIX + "\t")


def parse_alert(text: str) -> str | None:
    """Extract the body after `/alert`. Returns None when body is empty.

    Internal newlines are preserved so multi-line alert payloads remain
    structurally intact — retriever tokenisation operates on the full
    text and benefits from the original line breaks.
    """
    if not is_alert_command(text):
        return None
    body = text.lstrip()[len(_PREFIX):].strip()
    return body or None
