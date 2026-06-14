#!/bin/bash
# Read status.json and emit a brief health summary, OR generate a 7-day
# quantified health report.
#
# Usage:
#   bash scripts/check-health.sh                              # current status (default)
#   bash scripts/check-health.sh --status-file <path>         # current status, custom file
#   bash scripts/check-health.sh --report-7d                  # 7-day quantified report
#   bash scripts/check-health.sh --report-7d \
#        --state-dir <path> --log-dir <path>                  # report with custom dirs (tests)
#
# Exit codes:
#   current mode:  0=running, 1=not-running, 2=stale heartbeat
#   report-7d:     0=all targets met (no N/A, no miss)
#                  1=at least one target missed
#                  2=insufficient data (any N/A; distinct signal for cron)
set -euo pipefail
cd "$(dirname "$0")/.."

MODE="current"
STATUS_FILE=".state/status.json"
STATE_DIR=".state"
LOG_DIR="log"

# Backward-compat: a single positional arg (a path) is interpreted as
# --status-file (preserves the old `bash check-health.sh some.json` form).
if [ $# -eq 1 ] && [[ "$1" != --* ]]; then
    STATUS_FILE="$1"
    shift
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --report-7d) MODE="report-7d" ;;
        --current) MODE="current" ;;
        --status-file) shift; STATUS_FILE="$1" ;;
        --state-dir)   shift; STATE_DIR="$1" ;;
        --log-dir)     shift; LOG_DIR="$1" ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if [ "$MODE" = "current" ]; then
    if [ ! -f "$STATUS_FILE" ]; then
        echo "status.json not found: $STATUS_FILE"
        exit 1
    fi
    python3 -c "
import json, sys, time
with open('$STATUS_FILE') as f:
    data = json.load(f)
age = int(time.time()) - data.get('timestamp', 0)
status = data.get('status', 'unknown')
uptime = data.get('uptime_seconds', 0)
print(f'status:        {status}')
print(f'version:       {data.get(\"version\")}')
print(f'uptime:        {uptime}s')
print(f'heartbeat age: {age}s')
if status != 'running':
    sys.exit(1)
if age > 120:
    print('heartbeat is stale (>2 min)', file=sys.stderr)
    sys.exit(2)
"
    exit 0
fi

# --- report-7d mode ---
exec python3 - "$STATE_DIR" "$LOG_DIR" <<'PY'
"""7-day quantified health report.

Inputs:
  - {state_dir}/status-history*.jsonl   (M9-T05 schema)
  - {log_dir}/runtime.log               (RotatingFileHandler, M9 logging.py)
  - {state_dir}/stream_card_throttled_count   (M6 cumulative counter)
  - {state_dir}/approval_state.json     (optional; see notes)

Targets (per docs/plans M10 §11.1):
  uptime ratio          >= 99%
  ERROR per day         <  5/day  (avg)
  approval stuck        == 0
  stream_card throttled <  10%
  feishu reply p95      <  180s simple / 300s with verifier

Exit codes:
  0  all targets met AND every check had real data (no N/A)
  1  at least one target missed
  2  insufficient data — at least one check is N/A (distinct cron signal)

Each check resolves to one of three states: "met", "missed", "na".
The summary distinguishes all three so a fresh deploy with no data is
NOT mis-reported as ship-ready.

p95 method: nearest-rank — index = ceil(0.95 * n) (1-indexed) on the
sorted ascending latency list. For n=10 this gives index 10 (the max).
"""
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from math import ceil
from pathlib import Path

state_dir = Path(sys.argv[1])
log_dir = Path(sys.argv[2])

WINDOW_DAYS = 7
now = datetime.now()
window_start = now - timedelta(days=WINDOW_DAYS)
window_start_ts = window_start.timestamp()

print(
    f"agent-runtime · 7-day health report "
    f"(window: {window_start.strftime('%Y-%m-%d')} → {now.strftime('%Y-%m-%d')})"
)
print("=" * 64)

results: list[str] = []  # one of "met" | "missed" | "na" per check


def mark(state: str) -> str:
    return {"met": "OK", "missed": "MISS", "na": "N/A"}.get(state, state)


# ----------------------------------------------------------------------
# 1. Uptime ratio
# ----------------------------------------------------------------------
history_files = sorted(state_dir.glob("status-history*.jsonl"))
running = 0
total = 0
for hf in history_files:
    try:
        with hf.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = row.get("ts", 0)
                if ts < window_start_ts:
                    continue
                total += 1
                if row.get("status") == "running":
                    running += 1
    except OSError:
        continue

if total == 0:
    uptime_state = "na"
    print(f"uptime: no history data   target: >= 99%   {mark(uptime_state)}")
else:
    pct = 100.0 * running / total
    uptime_state = "met" if pct >= 99.0 else "missed"
    print(
        f"status=running uptime: {pct:.1f}%   target: >= 99%   "
        f"{mark(uptime_state)}"
    )
results.append(uptime_state)

# ----------------------------------------------------------------------
# 2. ERROR per day
# ----------------------------------------------------------------------
log_file = log_dir / "runtime.log"
errors_per_day: dict[str, int] = {}
log_lines: list[str] = []
if log_file.is_file():
    try:
        with log_file.open(errors="replace") as f:
            log_lines = f.readlines()
    except OSError:
        log_lines = []

date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})")
window_dates = {
    (window_start + timedelta(days=i)).strftime("%Y-%m-%d")
    for i in range(WINDOW_DAYS + 1)
}
for line in log_lines:
    if "ERROR" not in line:
        continue
    m = date_re.match(line)
    if not m:
        continue
    d = m.group(1)
    if d not in window_dates:
        continue
    errors_per_day[d] = errors_per_day.get(d, 0) + 1

if not log_file.is_file():
    errors_state = "na"
    print(f"ERROR per day: no log data   target: < 5/day   {mark(errors_state)}")
else:
    counts = list(errors_per_day.values())
    if counts:
        avg = sum(counts) / len(counts)
        mx = max(counts)
    else:
        avg = 0.0
        mx = 0
    errors_state = "met" if avg < 5.0 else "missed"
    print(
        f"ERROR per day: avg {avg:.1f}, max {mx}   "
        f"target: < 5/day   {mark(errors_state)}"
    )
    if errors_state == "missed":
        print("  hint: check runtime.log for repeated traceback patterns")
results.append(errors_state)

# ----------------------------------------------------------------------
# 3. Approval stuck count
# ----------------------------------------------------------------------
approval_file = state_dir / "approval_state.json"
stuck = 0
approval_timeout = 1800
if approval_file.is_file():
    try:
        with approval_file.open() as f:
            data = json.load(f)
        # data may be {request_id: {state, created_at, ...}, ...}
        if isinstance(data, dict):
            for _, entry in data.items():
                if not isinstance(entry, dict):
                    continue
                state = entry.get("state") or entry.get("status")
                created_at = entry.get("created_at", 0)
                if state == "PENDING" and created_at:
                    age = now.timestamp() - float(created_at)
                    if age > approval_timeout:
                        stuck += 1
    except (OSError, json.JSONDecodeError):
        pass
# Also scan log for approval timeout lines as a secondary signal
approval_timeout_re = re.compile(r"approval.*TIMEOUT", re.IGNORECASE)
log_stuck_count = 0
for line in log_lines:
    m = date_re.match(line)
    if not m or m.group(1) not in window_dates:
        continue
    if approval_timeout_re.search(line):
        log_stuck_count += 1
stuck_total = stuck + log_stuck_count
# approval has hard data sources (the file + log scan); absence of stuck
# entries IS valid evaluable data ("met"), not N/A.
approval_state = "met" if stuck_total == 0 else "missed"
print(
    f"approval stuck count: {stuck_total}   "
    f"target: 0   {mark(approval_state)}"
)
if approval_state == "missed":
    print("  hint: inspect approval_state.json for orphaned PENDING entries")
results.append(approval_state)

# ----------------------------------------------------------------------
# 4. stream_card_throttled ratio (cumulative approximation)
# ----------------------------------------------------------------------
counter_file = state_dir / "stream_card_throttled_count"
throttled = 0
if counter_file.is_file():
    try:
        throttled = int(counter_file.read_text().strip() or "0")
    except (OSError, ValueError):
        throttled = 0
# total handled messages (best-effort): grep handle_message in log
msg_count = sum(1 for line in log_lines if "handle_message" in line)
if not counter_file.is_file() and msg_count == 0:
    # No counter file AND no message activity in log -> nothing to evaluate.
    throttled_state = "na"
    print(
        "stream_card_throttled ratio: no data   "
        f"target: < 10%   {mark(throttled_state)}"
    )
else:
    if msg_count > 0:
        ratio = 100.0 * throttled / msg_count
    else:
        ratio = 0.0 if throttled == 0 else 100.0
    throttled_state = "met" if ratio < 10.0 else "missed"
    print(
        f"stream_card_throttled ratio: {ratio:.1f}% "
        f"({throttled}/{msg_count})   target: < 10%   "
        f"{mark(throttled_state)}   "
        "(approx: counter is cumulative since last reset)"
    )
results.append(throttled_state)

# ----------------------------------------------------------------------
# 5. Feishu reply p95 latency
# ----------------------------------------------------------------------
# Expected log format (defined by THIS implementation; emit-side is M-future):
#   YYYY-MM-DD HH:MM:SS LEVEL ... msg_id=<id> start
#   YYYY-MM-DD HH:MM:SS LEVEL ... msg_id=<id> end (<n>s)
# We pair on msg_id and read the trailing "(<n>s)" as the latency.
end_re = re.compile(r"msg_id=(\S+)\s+end\s+\((\d+(?:\.\d+)?)s\)")
latencies: list[float] = []
for line in log_lines:
    m = date_re.match(line)
    if not m or m.group(1) not in window_dates:
        continue
    em = end_re.search(line)
    if em:
        try:
            latencies.append(float(em.group(2)))
        except ValueError:
            continue

if not latencies:
    latency_state = "na"
    print(
        "feishu reply p95 latency: n/a (logging not instrumented)   "
        f"target: <180s / <300s   {mark(latency_state)}"
    )
else:
    s = sorted(latencies)
    n = len(s)
    idx = max(1, ceil(0.95 * n)) - 1  # 1-indexed -> 0-indexed
    p95 = s[idx]
    latency_state = "met" if p95 < 180.0 else "missed"
    print(
        f"feishu reply p95 latency: {p95:.0f}s   "
        f"target: <180s / <300s   {mark(latency_state)}"
    )
    if latency_state == "missed":
        print("  hint: investigate slow upstream calls or verifier timeouts")
results.append(latency_state)

# ----------------------------------------------------------------------
# Summary — distinguish met / missed / N/A
# ----------------------------------------------------------------------
print("")
met_count = sum(1 for s in results if s == "met")
missed_count = sum(1 for s in results if s == "missed")
na_count = sum(1 for s in results if s == "na")

if missed_count > 0:
    verdict = "NOT READY — see MISS targets above."
    exit_code = 1
elif na_count > 0:
    verdict = (
        "INSUFFICIENT DATA — collect at least 7 days of runtime "
        "before assessing ship readiness."
    )
    exit_code = 2
else:
    verdict = "SHIP CANDIDATE."
    exit_code = 0

print(
    f"Overall: {met_count} met / {missed_count} missed / "
    f"{na_count} N/A. {verdict}"
)

sys.exit(exit_code)
PY
