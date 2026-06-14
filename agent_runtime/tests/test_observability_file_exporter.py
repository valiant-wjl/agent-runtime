"""FileSpanExporter — verify on-disk OTel JSON schema + safe error swallowing.

Spec § 6.1: trace emission must NEVER raise into scheduler. Disk full,
permission denied, serialization error → silent drop + log.error + bump
drop_count.
"""
import json
from pathlib import Path

from agent_runtime.observability import FileSpanExporter, SpanRecord


def test_exporter_appends_one_json_line_per_span(tmp_path: Path):
    exporter = FileSpanExporter(trace_dir=tmp_path)
    span = SpanRecord(
        trace_id="0af7651916cd43dd8448eb211c80319c",
        span_id="b7ad6b7169203331",
        parent_span_id=None,
        name="turn",
        start_time_ns=1778568000000000000,
        end_time_ns=1778568058000000000,
        status_code="OK",
        attributes={"digital_agent.turn_id": "abc", "digital_agent.text_len": 1958},
    )

    exporter.export([span])

    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["trace_id"] == "0af7651916cd43dd8448eb211c80319c"
    assert data["name"] == "turn"
    assert data["attributes"]["digital_agent.text_len"] == 1958
    assert data["status"]["code"] == "OK"
    assert exporter.drop_count == 0


def test_exporter_appends_multiple_spans(tmp_path: Path):
    exporter = FileSpanExporter(trace_dir=tmp_path)
    spans = [
        SpanRecord(trace_id="t", span_id=f"s{i}", parent_span_id=None,
                   name="turn", start_time_ns=i, end_time_ns=i+1,
                   status_code="OK", attributes={})
        for i in range(3)
    ]
    exporter.export(spans)
    lines = list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()
    assert len(lines) == 3


def test_exporter_swallows_disk_errors(tmp_path: Path):
    """Permission-denied on the trace dir → silent drop + bump counter, no raise."""
    bad_dir = tmp_path / "blocked"
    bad_dir.mkdir()
    bad_dir.chmod(0o000)
    try:
        exporter = FileSpanExporter(trace_dir=bad_dir)
        span = SpanRecord(
            trace_id="x", span_id="y", parent_span_id=None,
            name="turn", start_time_ns=0, end_time_ns=1,
            status_code="OK", attributes={},
        )
        # Must NOT raise
        exporter.export([span])
        assert exporter.drop_count == 1
    finally:
        bad_dir.chmod(0o755)


def test_exporter_creates_trace_dir_if_missing(tmp_path: Path):
    nonexistent = tmp_path / "deep" / "nested" / "dir"
    exporter = FileSpanExporter(trace_dir=nonexistent)
    span = SpanRecord(
        trace_id="t", span_id="s", parent_span_id=None,
        name="turn", start_time_ns=0, end_time_ns=1,
        status_code="OK", attributes={},
    )
    exporter.export([span])
    assert nonexistent.exists()
    assert list(nonexistent.glob("*.jsonl"))
