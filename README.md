# agent-runtime

A turn-by-turn **digital agent runtime** that wires an IM channel (e.g. Feishu/Lark)
to a Claude CLI backend, with a verification loop, human-approval gating, multi-project
routing, and OpenTelemetry tracing built in.

You bring a persona (Markdown files) and a config; agent-runtime runs the loop:
receive a message → route to the right project context → draft via Claude → optionally
verify the draft → send (or ask for approval first) → emit a trace span for every turn.

## How it works

```
IM event ─▶ channel adapter ─▶ scheduler ─▶ Claude CLI (claude_proc)
                                   │                │
                                   │           draft answer
                                   │                ▼
                                   │           verifier (should_trigger? → verify loop)
                                   │                │
                                   ▼                ▼
                            approval gate ◀── final answer ──▶ channel reply
                                   │
                                   ▼
                          OTel span per turn (file + optional OTLP)
```

1. **Channel adapter** polls/receives IM events and normalizes them (`agent_runtime/channels/`).
2. **Scheduler** (`scheduler.py`) is the turn-by-turn event loop: dedup, routing, session
   state, then forks the Claude CLI.
3. **claude_proc** (`claude_proc.py`) wraps the local `claude` CLI (`run` / `run_stream`),
   loads the persona (`SOUL.md` / `USER.md`) from the configured work dir, and restricts
   tools per phase.
4. **Verifier** (`verifier.py`) decides — via pure trigger rules — whether a draft is worth
   a second-pair-of-eyes pass (quantitative claims, service identifiers, ops verbs, SQL,
   money…), then runs a verify loop with a cost budget.
5. **Approval** (`approval.py`) gates writes with no downstream safety net behind a
   human-approval state machine.
6. **Observability** (`observability.py`) emits one OTel-shaped span per turn to a local
   JSONL file, and optionally ships to any OTLP/HTTP collector.

## Architecture: extension points

| Seam | Where | How to swap |
|---|---|---|
| **Model runner** | `claude_proc.py` + `verifier.verify(_runner=…)` | The scheduler builds a runner closure that forks the `claude` CLI; inject your own `async runner(*, work_dir, question, draft) -> str`. |
| **Channel** | `agent_runtime/channels/` (`ChannelAdapter` protocol + `registry`) | Implement `ChannelAdapter` for Slack/Discord/etc. and register it. Feishu (via the public `lark-cli`) ships as the reference adapter. |
| **Observability** | `observability.py` | File exporter always on; set `AGENT_RUNTIME_OTLP_*` to also ship to your collector. Swap the backend by editing this one module. |
| **Persona / context** | `templates/meta/*.template` → your work dir | `SOUL.md` / `USER.md` etc. live in a local work dir (never in the repo); `bootstrap.sh` seeds them from the templates. |

## OTel trace contract

Each turn emits a span (OpenTelemetry GenAI semantic conventions):

- Custom attributes under the `digital_agent.*` namespace (e.g. `digital_agent.chat_id`,
  `digital_agent.text_len`, `digital_agent.is_alert`, `digital_agent.tool_use_count`).
- Vendor-neutral GenAI keys: `gen_ai.request.model`, `gen_ai.usage.input_tokens` /
  `output_tokens`.
- Spans are appended as JSON lines to `{state_dir}/traces/YYYY-MM.jsonl`.

Observability env vars (all optional — file export works with none set):

```
AGENT_RUNTIME_OTLP_ENDPOINT      full OTLP/HTTP URL of your collector (enables OTLP)
AGENT_RUNTIME_OTLP_HEADERS_JSON  JSON object of header → value (e.g. auth)
AGENT_RUNTIME_OTLP_SERVICE_NAME  service.name resource attr (default: agent-runtime)
```

## Quickstart

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

cp config.example.yaml config.yaml      # edit: channel, projects, work dir, admin ids
bash scripts/bootstrap.sh               # seed persona files into your work dir (interactive)

agent-runtime --config config.yaml      # run the loop
# or: bash scripts/run.sh
```

Requires Python 3.11+ and the `claude` CLI on PATH (the model backend). The Feishu
channel additionally needs the public `lark-cli`.

## Configuration

`config.example.yaml` is the annotated template. Key sections:

- `channels:` — which IM adapter(s) to enable (`feishu`, …) and their settings.
- `projects:` — named project contexts with `work_dir`, routing keywords, admin users,
  approval timeout, and per-phase tool restrictions.
- `paths.meta_work_dir` — where the persona/context Markdown lives (local, not committed).
- `runtime:` — session file, verifier budgets, timeouts.

The config writer (`config_writer.py`) edits `config.yaml` in place using ruamel.yaml
roundtrip mode, so comments and key order survive `/agent` admin commands.

## Tests

```bash
pip install -e ".[dev]"
pytest -q          # 655 tests
```

## Deployment

`scripts/` ships systemd (`install-systemd.sh` + `agent-runtime.service`) and macOS
launchd (`install-service.sh`, `install-timers-macos.sh`) installers, plus a cron
installer for the scheduled ingest/backup jobs. All assume the repo is cloned at
`~/agent-runtime`; edit the unit paths otherwise.

## License

MIT — see [LICENSE](LICENSE).
