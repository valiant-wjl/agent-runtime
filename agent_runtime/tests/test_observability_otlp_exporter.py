"""OtlpHttpExporter — verify OTLP/HTTP-JSON shape + best-effort error swallow.

Spec: trace emission MUST NEVER raise into the scheduler. Network failure,
non-2xx, malformed response → silent drop + log.error + bump drop_count.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from agent_runtime.observability import (
    OtlpHttpExporter,
    SpanRecord,
    _otlp_from_env,
)


def _span(**overrides) -> SpanRecord:
    defaults = dict(
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        parent_span_id=None,
        name="turn",
        start_time_ns=1_000_000_000,
        end_time_ns=2_000_000_000,
        status_code="OK",
        attributes={"digital_agent.text_len": 42, "digital_agent.is_alert": True},
    )
    defaults.update(overrides)
    return SpanRecord(**defaults)


def _mock_resp(status: int = 200) -> MagicMock:
    cm = MagicMock()
    cm.__enter__.return_value.status = status
    cm.__exit__.return_value = False
    return cm


def test_exporter_posts_otlp_json_shape():
    exp = OtlpHttpExporter(
        endpoint="https://fornax.bytedance.net/v1/traces",
        headers={"x-jwt-token": "abc"},
    )
    with patch("urllib.request.urlopen", return_value=_mock_resp(200)) as urlopen:
        exp.export([_span()])
    assert urlopen.call_count == 1
    req = urlopen.call_args.args[0]
    body = json.loads(req.data.decode("utf-8"))
    rs = body["resourceSpans"][0]
    # resource attributes contain service.name
    keys = [a["key"] for a in rs["resource"]["attributes"]]
    assert "service.name" in keys
    span_entry = rs["scopeSpans"][0]["spans"][0]
    assert span_entry["traceId"] == "0af7651916cd43dd8448eb211c80319c"
    assert span_entry["spanId"] == "b7ad6b7169203331"
    assert span_entry["name"] == "turn"
    assert "parentSpanId" not in span_entry  # root span: omit
    assert span_entry["startTimeUnixNano"] == "1000000000"
    assert span_entry["endTimeUnixNano"] == "2000000000"
    assert span_entry["status"]["code"] == 1  # OK
    attr_map = {a["key"]: a["value"] for a in span_entry["attributes"]}
    assert attr_map["digital_agent.text_len"] == {"stringValue": "42"}
    assert attr_map["digital_agent.is_alert"] == {"boolValue": True}
    # headers preserved + content-type default
    assert req.headers.get("X-jwt-token") == "abc"
    assert req.headers.get("Content-type") == "application/json"
    assert exp.drop_count == 0


def test_exporter_emits_parent_span_id_when_set():
    exp = OtlpHttpExporter(endpoint="https://x/v1/traces")
    with patch("urllib.request.urlopen", return_value=_mock_resp(200)) as urlopen:
        exp.export([_span(parent_span_id="cafebabecafebabe", name="tool_use")])
    body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
    span_entry = body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert span_entry["parentSpanId"] == "cafebabecafebabe"
    assert span_entry["name"] == "tool_use"


def test_exporter_maps_status_codes():
    exp = OtlpHttpExporter(endpoint="https://x/v1/traces")
    with patch("urllib.request.urlopen", return_value=_mock_resp(200)) as urlopen:
        exp.export([_span(status_code="ERROR")])
    body = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
    assert body["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"]["code"] == 2


def test_exporter_swallows_network_error():
    exp = OtlpHttpExporter(endpoint="https://x/v1/traces")
    with patch("urllib.request.urlopen", side_effect=ConnectionError("dns boom")):
        exp.export([_span(), _span(span_id="xx")])  # must NOT raise
    assert exp.drop_count == 2


def test_exporter_swallows_non_2xx():
    exp = OtlpHttpExporter(endpoint="https://x/v1/traces")
    with patch("urllib.request.urlopen", return_value=_mock_resp(503)):
        exp.export([_span()])
    assert exp.drop_count == 1


def test_exporter_no_spans_is_noop():
    exp = OtlpHttpExporter(endpoint="https://x/v1/traces")
    with patch("urllib.request.urlopen") as urlopen:
        exp.export([])
    assert urlopen.call_count == 0
    assert exp.drop_count == 0


# --- env wiring ---


def test_otlp_from_env_returns_none_when_endpoint_unset(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_OTLP_ENDPOINT", raising=False)
    assert _otlp_from_env() is None


def test_otlp_from_env_returns_none_when_endpoint_blank(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_OTLP_ENDPOINT", "   ")
    assert _otlp_from_env() is None


def test_otlp_from_env_parses_headers_json(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_OTLP_ENDPOINT", "https://fornax.bytedance.net/v1/traces")
    monkeypatch.setenv(
        "AGENT_RUNTIME_OTLP_HEADERS_JSON",
        json.dumps({"x-jwt-token": "tok", "x-workspace-id": "7590068861145991426"}),
    )
    monkeypatch.setenv("AGENT_RUNTIME_OTLP_SERVICE_NAME", "digital-agent-prod")
    exp = _otlp_from_env()
    assert exp is not None
    assert exp.endpoint == "https://fornax.bytedance.net/v1/traces"
    assert exp.headers["x-jwt-token"] == "tok"
    assert exp.headers["x-workspace-id"] == "7590068861145991426"
    assert exp.service_name == "digital-agent-prod"


def test_otlp_from_env_tolerates_bad_headers_json(monkeypatch):
    """Invalid header JSON must not crash configure()."""
    monkeypatch.setenv("AGENT_RUNTIME_OTLP_ENDPOINT", "https://x/v1/traces")
    monkeypatch.setenv("AGENT_RUNTIME_OTLP_HEADERS_JSON", "not json {")
    exp = _otlp_from_env()
    assert exp is not None  # endpoint alone is enough; bad headers logged + ignored
    assert exp.headers == {"content-type": "application/json"}
