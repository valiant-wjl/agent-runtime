"""`/agent <verb> [sub] [args] [--flags]` slash command parser.

Pure-function parser; no IO, no state. Mirrors `runtime/alert_cmd.py`
and `runtime/lesson.py` shape so scheduler integration is symmetric.

Command tree:
  /agent show
  /agent alert list
  /agent alert remove <chat_id>
  /agent alert register [--project <name>]
  /agent project list
  /agent project add <name> <work_dir> --group "<群名>"
  /agent project rm <name>

Anything else returns AgentCommand(verb="_help", ...) so agent_admin
can render a usage hint without each caller re-implementing the check.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field

_PREFIX = "/agent"
_HELP = "_help"

_KNOWN_VERBS = {"show", "alert", "project"}
_ALERT_SUBS = {"list", "remove", "register"}
_PROJECT_SUBS = {"add", "rm", "list"}


@dataclass(frozen=True)
class AgentCommand:
    verb: str
    sub: str | None = None
    args: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)


def is_agent_command(text: str) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    if stripped == _PREFIX:
        return True
    return stripped.startswith(_PREFIX + " ") or stripped.startswith(_PREFIX + "\t")


def parse_agent(text: str) -> AgentCommand | None:
    if not is_agent_command(text):
        return None
    stripped = text.lstrip()
    rest = stripped[len(_PREFIX):].strip()
    if not rest:
        return AgentCommand(verb=_HELP)
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return AgentCommand(verb=_HELP)

    verb = tokens[0]
    if verb not in _KNOWN_VERBS:
        return AgentCommand(verb=_HELP)

    if verb == "show":
        return AgentCommand(verb="show")

    if verb == "project":
        if len(tokens) < 2:
            return AgentCommand(verb=_HELP)
        sub = tokens[1]
        if sub not in _PROJECT_SUBS:
            return AgentCommand(verb=_HELP)
        args, flags = _split_args_flags(tokens[2:])
        if sub == "add":
            # Need name + work_dir positional args and a --group flag.
            if len(args) < 2 or not flags.get("group"):
                return AgentCommand(verb=_HELP)
        if sub == "rm" and not args:
            return AgentCommand(verb=_HELP)
        return AgentCommand(verb="project", sub=sub, args=args, flags=flags)

    # verb == "alert"
    if len(tokens) < 2:
        return AgentCommand(verb=_HELP)
    sub = tokens[1]
    if sub not in _ALERT_SUBS:
        return AgentCommand(verb=_HELP)

    args, flags = _split_args_flags(tokens[2:])

    if sub == "remove" and not args:
        return AgentCommand(verb=_HELP)

    return AgentCommand(verb="alert", sub=sub, args=args, flags=flags)


def _split_args_flags(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    args: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.startswith("--"):
            key = t[2:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[key] = tokens[i + 1]
                i += 2
            else:
                flags[key] = ""
                i += 1
        else:
            args.append(t)
            i += 1
    return args, flags
