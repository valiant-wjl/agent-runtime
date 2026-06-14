#!/usr/bin/env python3
"""End-to-end self-test harness for agent-runtime.

Bypasses the feishu lark-cli channel (which needs an interactive scope grant)
and feeds a ParsedMsg straight into scheduler._handle_message_inner with an
in-memory CaptureChannel. Exercises the FULL real pipeline:
  - claude_proc.run_stream (real claude subprocess, real OAuth token)
  - alert_resolver / alert_judge (per project_cfg.alert_chats whitelist)
  - approval flow / verifier / etc.
  - runtime/observability.py span emission

After the turn completes, prints:
  - what the bot would have replied
  - the trace span(s) just written to .state/traces/*.jsonl
  - waits 8s, then dumps issue ledger + fingerprints (observer-side)

Usage:
    python3 scripts/e2e_self_test.py "你想问 bot 什么"
    python3 scripts/e2e_self_test.py --chat oc_69f78... --is-alert "告警原文"
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import secrets
import sys
import time
from pathlib import Path

# Make repo root importable when run as `python3 scripts/e2e_self_test.py`
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_runtime.channels import ParsedMsg  # noqa: E402
from agent_runtime import config as config_mod, scheduler, session  # noqa: E402


class CaptureChannel:
    """Records all outbound calls; no real I/O."""
    name = "e2e-capture"

    def __init__(self):
        self.replies: list[str] = []
        self.cards_sent: list[dict] = []
        self.cards_updated: list[tuple[str, dict]] = []

    async def reply(self, parsed, text):
        self.replies.append(text)
        print(f"\n[capture] reply ({len(text)} chars):\n{text[:500]}" + ("…" if len(text) > 500 else ""))

    async def send_card(self, parsed, card):
        cid = f"om_capture_{secrets.token_hex(4)}"
        self.cards_sent.append(card)
        return cid

    async def update_card(self, card_msg_id, card):
        self.cards_updated.append((card_msg_id, card))
        return True

    async def fetch_thread_history(self, root_id):
        return []

    async def fetch_topic_history(self, *args, **kwargs):
        return []

    async def download_image(self, **kwargs):
        raise NotImplementedError


def _make_parsed(text: str, chat_id: str, is_alert: bool) -> ParsedMsg:
    msg_id = f"om_e2e_{secrets.token_hex(6)}"
    sender_type = "app" if is_alert else "user"
    sender_id = "ou_e2e_sender" if not is_alert else "ou_REPLACE_ME"
    raw_msg_type = "interactive" if is_alert else "text"
    return ParsedMsg(
        channel="feishu",
        message_id=msg_id,
        thread_root_id=msg_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name="e2e-self-test",
        text=text,
        mentions=[],
        raw_event={"event": {"message": {"message_type": raw_msg_type},
                              "sender": {"sender_type": sender_type}}},
        chat_type="p2p" if not is_alert else "group",
        sender_type=sender_type,
    )


async def _run(args):
    cfg = config_mod.load_config(args.config)
    scheduler._apply_observability_config(cfg)
    # session module is a singleton; daemon's run_forever() configures it
    # but our harness must call it explicitly. Use a throwaway file so the
    # harness doesn't trample the production sessions.json.
    session.configure(Path("/tmp") / f"e2e_sessions_{secrets.token_hex(4)}.json")
    project_name = args.project
    project_cfg = cfg["projects"][project_name]
    runtime_cfg = {**cfg["runtime"], "channels": cfg["channels"]}
    alert_cfg = cfg.get("alert_resolver")

    parsed = _make_parsed(args.text, args.chat, args.alert)
    channel = CaptureChannel()

    # Snapshot trace file size pre-call (so we know what's new)
    trace_dir = Path(cfg["observability"]["trace_dir"])
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_file = trace_dir / f"{dt.datetime.utcnow():%Y-%m}.jsonl"
    pre_size = trace_file.stat().st_size if trace_file.exists() else 0

    t0 = time.monotonic()
    print(f"[harness] dispatching project={project_name} chat={args.chat} is_alert={args.alert}")
    await scheduler._handle_message_inner(
        channel=channel, parsed=parsed,
        project_name=project_name, project_cfg=project_cfg,
        runtime_cfg=runtime_cfg, alert_cfg=alert_cfg,
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    print(f"\n[harness] turn complete in {elapsed_ms}ms; replies={len(channel.replies)} cards={len(channel.cards_sent)}")

    # Dump new spans only
    if trace_file.exists():
        with open(trace_file) as f:
            f.seek(pre_size)
            new_spans = [json.loads(l) for l in f.read().splitlines() if l.strip()]
        print(f"\n[harness] trace spans emitted ({len(new_spans)}):")
        for s in new_spans:
            kind = s["name"]
            attrs = s.get("attributes", {})
            line = f"  {kind:10} parent={(s.get('parent_span_id') or '-')[:8]} status={s.get('status', {}).get('code')}"
            print(line)
            for k, v in attrs.items():
                vstr = repr(v)
                if len(vstr) > 80:
                    vstr = vstr[:77] + "…"
                print(f"      {k} = {vstr}")
    else:
        print("\n[harness] WARNING: no trace file produced — observability disabled?")

    # Give observer worker time to process this trace
    print("\n[harness] waiting 8s for observer worker to consume…")
    await asyncio.sleep(8)
    state = trace_dir.parent  # .state/
    issues_file = state / "issues" / f"{dt.datetime.utcnow():%Y-%m}.jsonl"
    fp_file = state / "fingerprints.json"
    print("\n[harness] issue ledger:")
    if issues_file.exists() and issues_file.stat().st_size > 0:
        for line in issues_file.read_text().splitlines():
            if line.strip():
                print("  ", json.dumps(json.loads(line), ensure_ascii=False)[:300])
    else:
        print("  (empty — no judge-flagged failures)")
    print("\n[harness] fingerprints:")
    if fp_file.exists():
        print(" ", json.dumps(json.loads(fp_file.read_text()), ensure_ascii=False)[:500])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text", help="message text to feed bot")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--project", default="example_project")
    ap.add_argument("--chat", default="oc_E2E_HARNESS",
                    help="chat_id (default: synthetic). Use real alert chat to trigger alert path.")
    ap.add_argument("--alert", action="store_true",
                    help="treat as alert (sender_type=app, msg_type=interactive)")
    args = ap.parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
