"""OpenTelemetry SDK wrapper for digital-agent.

Other modules NEVER import opentelemetry directly — this is the only
seam. Lets us swap the backend (Langfuse / Phoenix / Jaeger) by editing
only this file later.

Failure policy (spec § 6.1): emit failures silent-drop + log.error.
Trace emission MUST NEVER raise into scheduler — main reply path stays
unaffected by observability problems.

Spec: docs/specs/2026-05-12-digital-agent-observer-design.md § 3.1
"""
from __future__ import annotations

import contextvars
import datetime as _dt
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Span record + file exporter (no SDK integration; we own the schema)
# ---------------------------------------------------------------------------


@dataclass
class SpanRecord:
    """Serialization-ready snapshot of one OTel-shaped span.

    Conforms to OpenTelemetry GenAI semantic conventions for `attributes`
    namespacing: `gen_ai.*` for vendor-neutral keys, `digital_agent.*` for
    project-specific keys.
    """
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start_time_ns: int
    end_time_ns: int
    status_code: str  # "OK" | "ERROR" | "UNSET"
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "start_time_ns": self.start_time_ns,
            "end_time_ns": self.end_time_ns,
            "status": {"code": self.status_code},
            "attributes": self.attributes,
        }


class FileSpanExporter:
    """Append spans to trace_dir/YYYY-MM.jsonl using O_APPEND.

    POSIX O_APPEND is atomic for writes ≤ PIPE_BUF (4 KiB on Linux). Each
    span record is well under that, so concurrent writers from multiple
    processes are safe without explicit locking. Threads inside one
    process are also safe because each export() call does one os.write.

    All errors are swallowed: spec § 6.1 demands trace emission never
    raise into the scheduler. Dropped span count is exposed via
    `drop_count` and surfaced in the daily report.
    """

    def __init__(self, trace_dir: Path | str):
        self.trace_dir = Path(trace_dir)
        self.drop_count = 0
        self._lock = threading.Lock()  # guards drop_count

    def _current_file(self) -> Path:
        # Monthly rotation per spec § 5.1
        return self.trace_dir / f"{_dt.datetime.utcnow():%Y-%m}.jsonl"

    def export(self, spans: list[SpanRecord]) -> None:
        """Best-effort write; never raises."""
        if not spans:
            return
        try:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            path = self._current_file()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                buf = b"".join(
                    (json.dumps(s.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
                    for s in spans
                )
                os.write(fd, buf)
            finally:
                os.close(fd)
        except Exception as e:
            with self._lock:
                self.drop_count += len(spans)
            log.error("FileSpanExporter dropped %d span(s): %r", len(spans), e)


# ---------------------------------------------------------------------------
# OTLP HTTP/JSON exporter (Fornax ingest)
# ---------------------------------------------------------------------------


_STATUS_CODE_TO_OTLP = {"OK": 1, "ERROR": 2, "UNSET": 0}


def _attr_value(v: Any) -> dict[str, Any]:
    """Serialize a Python value into an OTLP AnyValue.

    Note (2026-05-21): Fornax server rejects OTLP intValue/doubleValue and
    returns 'json Unmarshal err' for any payload that contains them, but
    keeps HTTP 200 so the exporter cannot detect the drop. We coerce all
    numbers to stringValue as a workaround until Fornax fixes its parser.
    """
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, (int, float)):
        return {"stringValue": str(v)}
    if isinstance(v, str):
        return {"stringValue": v}
    return {"stringValue": json.dumps(v, ensure_ascii=False)}


def _kv(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _attr_value(v)} for k, v in attrs.items()]


class OtlpHttpExporter:
    """Best-effort POST of spans to an OTLP HTTP/JSON collector.

    Default OTLP/HTTP path is `/v1/traces` (OTel spec). Fornax's actual ingest
    path is documented in the Fornax wiki — swap `endpoint` accordingly via
    `FORNAX_OTLP_ENDPOINT` when known. Everything below is endpoint-agnostic.

    Failure policy mirrors FileSpanExporter: silent drop + log.error + bump
    `drop_count`. Trace emission must NEVER raise into the scheduler.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        headers: dict[str, str] | None = None,
        service_name: str = "digital-agent",
        timeout: float = 5.0,
    ) -> None:
        self.endpoint = endpoint
        self.headers = {"content-type": "application/json", **(headers or {})}
        self.service_name = service_name
        self.timeout = timeout
        self.drop_count = 0
        self._lock = threading.Lock()

    def _build_payload(self, spans: list[SpanRecord]) -> dict[str, Any]:
        otlp_spans = []
        for s in spans:
            entry: dict[str, Any] = {
                "traceId": s.trace_id,
                "spanId": s.span_id,
                "name": s.name,
                "kind": 1,  # SPAN_KIND_INTERNAL
                "startTimeUnixNano": str(s.start_time_ns),
                "endTimeUnixNano": str(s.end_time_ns),
                "status": {"code": _STATUS_CODE_TO_OTLP.get(s.status_code, 0)},
                "attributes": _kv(s.attributes),
            }
            if s.parent_span_id:
                entry["parentSpanId"] = s.parent_span_id
            otlp_spans.append(entry)
        return {
            "resourceSpans": [{
                "resource": {
                    "attributes": _kv({"service.name": self.service_name}),
                },
                "scopeSpans": [{
                    "scope": {"name": "digital-agent"},
                    "spans": otlp_spans,
                }],
            }],
        }

    def export(self, spans: list[SpanRecord]) -> None:
        """Best-effort POST; never raises."""
        if not spans:
            return
        try:
            body = json.dumps(self._build_payload(spans), ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.endpoint, data=body, headers=self.headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                if resp.status >= 400:
                    raise RuntimeError(f"OTLP ingest non-2xx: {resp.status}")
                # Fornax can return 200 with embedded json Unmarshal err.
                raw = resp.read(2048)
                if isinstance(raw, (bytes, bytearray)) and raw:
                    try:
                        env = json.loads(raw.decode("utf-8", errors="replace"))
                    except (ValueError, TypeError):
                        env = None
                    if isinstance(env, dict):
                        code = env.get("code")
                        if isinstance(code, int) and code != 0:
                            raise RuntimeError(f"OTLP server error code={code}: {env.get('msg')}")
        except Exception as e:
            with self._lock:
                self.drop_count += len(spans)
            log.error("OtlpHttpExporter dropped %d span(s): %r", len(spans), e)


# ---------------------------------------------------------------------------
# Span context managers (turn / tool / judge)
# ---------------------------------------------------------------------------


def _gen_id(byte_len: int) -> str:
    return secrets.token_hex(byte_len)


class _MutableSpan:
    """In-memory span captured on enter, flushed via exporter on exit.

    Deliberately does NOT use opentelemetry-sdk's Tracer because:
      (a) we own the schema and want it stable across SDK upgrades,
      (b) jsonl is the source of truth — observer reads files, not OTLP,
      (c) we want zero risk of background-thread leaks under a long
          running daemon.
    """
    __slots__ = (
        "trace_id", "span_id", "parent_span_id", "name",
        "start_time_ns", "end_time_ns", "status_code", "_attrs",
    )

    def __init__(self, *, trace_id: str, span_id: str,
                 parent_span_id: str | None, name: str):
        self.trace_id = trace_id
        self.span_id = span_id
        self.parent_span_id = parent_span_id
        self.name = name
        self.start_time_ns = time.time_ns()
        self.end_time_ns: int = 0
        self.status_code = "OK"
        self._attrs: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self._attrs[key] = value

    def set_status(self, code: str) -> None:
        if code not in ("OK", "ERROR", "UNSET"):
            raise ValueError(f"invalid span status code: {code!r}")
        self.status_code = code

    def to_record(self) -> SpanRecord:
        return SpanRecord(
            trace_id=self.trace_id,
            span_id=self.span_id,
            parent_span_id=self.parent_span_id,
            name=self.name,
            start_time_ns=self.start_time_ns,
            end_time_ns=self.end_time_ns or time.time_ns(),
            status_code=self.status_code,
            attributes=self._attrs,
        )


class _State:
    """Module-level singleton, configured once at scheduler boot."""
    enabled: bool = False
    exporter: FileSpanExporter | None = None
    otlp_exporter: OtlpHttpExporter | None = None


_state = _State()

# threads "current span" through async call stack so child spans can find
# their parent without explicit passing
_current_span: contextvars.ContextVar[_MutableSpan | None] = contextvars.ContextVar(
    "digital_agent_current_span", default=None,
)


def _otlp_from_env() -> OtlpHttpExporter | None:
    """Construct an OtlpHttpExporter from FORNAX_* env vars, or None.

    `FORNAX_OTLP_ENDPOINT` (required to enable; full URL)
    `FORNAX_OTLP_HEADERS_JSON` (optional; JSON object of header → value)
    `FORNAX_OTLP_SERVICE_NAME` (optional; defaults to 'digital-agent')
    """
    endpoint = os.environ.get("FORNAX_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return None
    headers: dict[str, str] = {}
    raw_headers = os.environ.get("FORNAX_OTLP_HEADERS_JSON", "").strip()
    if raw_headers:
        try:
            parsed = json.loads(raw_headers)
            if isinstance(parsed, dict):
                headers = {str(k): str(v) for k, v in parsed.items()}
        except Exception as e:
            log.error("FORNAX_OTLP_HEADERS_JSON invalid: %r", e)
    service_name = os.environ.get("FORNAX_OTLP_SERVICE_NAME", "digital-agent")
    return OtlpHttpExporter(
        endpoint=endpoint, headers=headers, service_name=service_name,
    )


def configure(*, trace_dir: Path | str, enabled: bool) -> None:
    """Wire up the singleton. Call once at daemon startup. Idempotent.

    File exporter is always created; OTLP exporter only when FORNAX_OTLP_*
    env vars are set. Both run in parallel — file is local source-of-truth
    for observer, OTLP ships to Fornax for cross-cutting analytics.
    """
    _state.enabled = bool(enabled)
    _state.exporter = FileSpanExporter(trace_dir=trace_dir)
    _state.otlp_exporter = _otlp_from_env()


def _flush(span: _MutableSpan) -> None:
    """Best-effort export; never raises (the exporter swallows, but we
    double-guard against any unexpected SDK / file-system surprise)."""
    if not _state.enabled or _state.exporter is None:
        return
    record = span.to_record()
    try:
        _state.exporter.export([record])
    except Exception as e:
        log.error("observability file flush failed: %r", e)
    if _state.otlp_exporter is not None:
        try:
            _state.otlp_exporter.export([record])
        except Exception as e:
            log.error("observability otlp flush failed: %r", e)


@asynccontextmanager
async def start_turn_span(
    *, chat_id: str, msg_id: str, is_alert: bool, branch: str | None = None,
) -> AsyncIterator[_MutableSpan]:
    """Top-level span for one user message → one reply.

    Spec § 5.1: emits a span named 'turn' with `digital_agent.*` attributes.
    """
    if not _state.enabled:
        # Yield a no-op span so callers' .set_attribute() still works
        yield _MutableSpan(
            trace_id="", span_id="", parent_span_id=None, name="turn",
        )
        return
    span = _MutableSpan(
        trace_id=_gen_id(16),
        span_id=_gen_id(8),
        parent_span_id=None,
        name="turn",
    )
    span.set_attribute("digital_agent.chat_id", chat_id)
    span.set_attribute("digital_agent.msg_id", msg_id)
    span.set_attribute("digital_agent.is_alert", is_alert)
    if branch is not None:
        span.set_attribute("digital_agent.branch", branch)
    token = _current_span.set(span)
    try:
        yield span
    except Exception:
        span.set_status("ERROR")
        raise
    finally:
        span.end_time_ns = time.time_ns()
        _current_span.reset(token)
        _flush(span)


@contextmanager
def start_tool_span(
    *, tool_name: str, input_preview: str = "",
) -> Iterator[_MutableSpan]:
    """Child span for one tool_use observation in the stream.

    No-op if there's no current turn span (e.g. tracing disabled, or
    used outside a turn). Span name is 'tool_use' per spec § 4.1.
    """
    parent = _current_span.get()
    if not _state.enabled or parent is None or parent.trace_id == "":
        yield _MutableSpan(
            trace_id="", span_id="", parent_span_id=None, name="tool_use",
        )
        return
    span = _MutableSpan(
        trace_id=parent.trace_id,
        span_id=_gen_id(8),
        parent_span_id=parent.span_id,
        name="tool_use",
    )
    span.set_attribute("digital_agent.tool_name", tool_name)
    if input_preview:
        span.set_attribute("digital_agent.tool_input_preview", input_preview[:200])
    try:
        yield span
    except Exception:
        span.set_status("ERROR")
        raise
    finally:
        span.end_time_ns = time.time_ns()
        _flush(span)


@asynccontextmanager
async def start_judge_span(
    *, judge_kind: str,
) -> AsyncIterator[_MutableSpan]:
    """Child span for observer-side meta-tracing of judge calls.

    Spec § 6.5: observer's own claude calls also emit spans so that
    Phase B can diagnose judge failures from trace data.
    """
    parent = _current_span.get()
    if not _state.enabled:
        yield _MutableSpan(
            trace_id="", span_id="", parent_span_id=None, name="judge",
        )
        return
    trace_id = parent.trace_id if parent else _gen_id(16)
    parent_id = parent.span_id if parent else None
    span = _MutableSpan(
        trace_id=trace_id,
        span_id=_gen_id(8),
        parent_span_id=parent_id,
        name="judge",
    )
    span.set_attribute("digital_agent.judge_kind", judge_kind)
    try:
        yield span
    except Exception:
        span.set_status("ERROR")
        raise
    finally:
        span.end_time_ns = time.time_ns()
        _flush(span)


def current_span() -> _MutableSpan | None:
    """Access the active span for ad-hoc set_attribute() calls.

    Used by scheduler insertion point #3 (auth_failed attribute) and
    insertion point for token usage attrs, where the attribute is
    discovered outside the span's own context-manager frame.
    """
    return _current_span.get()
