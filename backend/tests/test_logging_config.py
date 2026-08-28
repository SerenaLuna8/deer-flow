from __future__ import annotations

import json
import logging

from deerflow.logging_config import JsonTraceFormatter, TraceTextFormatter


def _scheduler_record() -> logging.LogRecord:
    record = logging.LogRecord(
        name="deerflow.subagents.lifecycle",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="Sub-Agent Task scheduler admitted",
        args=(),
        exc_info=None,
    )
    record.event = "subagent_scheduler_queue_wait"
    record.execution_id = "execution-123"
    record.scheduling_key = "run-456"
    record.queue_wait_seconds = 5.125
    record.queue_wait_warning_threshold_seconds = 5.0
    return record


def test_json_trace_formatter_emits_scheduler_telemetry_fields() -> None:
    payload = json.loads(JsonTraceFormatter().format(_scheduler_record()))

    assert payload["event"] == "subagent_scheduler_queue_wait"
    assert payload["execution_id"] == "execution-123"
    assert payload["scheduling_key"] == "run-456"
    assert payload["queue_wait_seconds"] == 5.125
    assert payload["queue_wait_warning_threshold_seconds"] == 5.0


def test_text_trace_formatter_emits_scheduler_telemetry_fields() -> None:
    rendered = TraceTextFormatter("%(message)s").format(_scheduler_record())

    assert 'event="subagent_scheduler_queue_wait"' in rendered
    assert 'execution_id="execution-123"' in rendered
    assert 'scheduling_key="run-456"' in rendered
    assert "queue_wait_seconds=5.125" in rendered
    assert "queue_wait_warning_threshold_seconds=5.0" in rendered
