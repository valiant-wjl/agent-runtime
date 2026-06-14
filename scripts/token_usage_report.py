#!/usr/bin/env python3
"""Aggregate `turn_summary` token usage from runtime.log.

Effective 2026-06-15, `claude -p` usage stops counting toward Claude plan
limits and starts drawing from a separate monthly Agent SDK credit pool
(Pro $20 / Max5x $100 / Max20x $200, see
https://support.claude.com/en/articles/15036540). This script aggregates
the `tokens_in` / `tokens_out` columns added to the structured
turn_summary log line so we can estimate spend before the cutover and
decide whether the daemon needs a separate API key.

Usage:
    python scripts/token_usage_report.py [LOG_PATH] [--days N] [--json]

Default LOG_PATH: <repo>/log/runtime.log.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_LINE_RE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}).*?"
    r"turn_summary\b.*?"
    r"tokens_in=(?P<in>\d+)\s+tokens_out=(?P<out>\d+)\s+model=(?P<model>\S+)"
)


def parse_log(text: str, *, since: datetime | None = None) -> dict:
    daily: dict[str, dict] = defaultdict(
        lambda: {
            "tokens_in": 0,
            "tokens_out": 0,
            "turns": 0,
            "models": defaultdict(int),
        }
    )
    for line in text.splitlines():
        m = _LINE_RE.search(line)
        if not m:
            continue
        ts_str = m.group("ts").replace("T", " ")
        try:
            ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if since is not None and ts < since:
            continue
        day = ts.strftime("%Y-%m-%d")
        bucket = daily[day]
        bucket["tokens_in"] += int(m.group("in"))
        bucket["tokens_out"] += int(m.group("out"))
        bucket["turns"] += 1
        bucket["models"][m.group("model")] += 1
    return {
        day: {
            "tokens_in": b["tokens_in"],
            "tokens_out": b["tokens_out"],
            "turns": b["turns"],
            "models": dict(b["models"]),
        }
        for day, b in daily.items()
    }


def render_text(daily: dict) -> str:
    if not daily:
        return "(no turn_summary records found)"
    lines = [
        f"{'date':<12}  {'tokens_in':>10}  {'tokens_out':>10}  {'turns':>5}  models"
    ]
    total_in = total_out = total_turns = 0
    for day in sorted(daily):
        b = daily[day]
        models = ",".join(f"{m}:{c}" for m, c in sorted(b["models"].items()))
        lines.append(
            f"{day:<12}  {b['tokens_in']:>10}  {b['tokens_out']:>10}  "
            f"{b['turns']:>5}  {models}"
        )
        total_in += b["tokens_in"]
        total_out += b["tokens_out"]
        total_turns += b["turns"]
    lines.append("-" * 60)
    lines.append(
        f"{'TOTAL':<12}  {total_in:>10}  {total_out:>10}  {total_turns:>5}"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log_path", nargs="?", default=None)
    parser.add_argument(
        "--days", type=int, default=None,
        help="Only count records from the last N days.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args(argv)

    if args.log_path:
        path = Path(args.log_path)
    else:
        path = Path(__file__).resolve().parents[1] / "log" / "runtime.log"
    if not path.exists():
        print(f"log not found: {path}", file=sys.stderr)
        return 2

    since = None
    if args.days is not None:
        since = datetime.now() - timedelta(days=args.days)
    daily = parse_log(path.read_text(errors="replace"), since=since)

    if args.json:
        print(json.dumps(daily, indent=2, sort_keys=True))
    else:
        print(render_text(daily))
    return 0


if __name__ == "__main__":
    sys.exit(main())
