"""Logging setup helpers for ActWeave."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime
from typing import Any

from deerflow.config.app_config import apply_logging_level
from deerflow.trace_context import get_current_trace_id

DEFAULT_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
TRACE_TEXT_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - [trace_id=%(trace_id)s] - %(message)s"
_TRACE_FILTER_NAME = "deerflow_trace_context_filter"
_STRUCTURED_LOG_FIELD_NAMES = (
    "event",
    "execution_id",
    "scheduling_key",
    "queue_wait_seconds",
    "queue_wait_warning_threshold_seconds",
)


def _structured_log_fields(record: logging.LogRecord) -> dict[str, str | int | float]:
    """Return the bounded telemetry fields supported by production formatters."""
    fields: dict[str, str | int | float] = {}
    for name in _STRUCTURED_LOG_FIELD_NAMES:
        value = getattr(record, name, None)
        if isinstance(value, str):
            fields[name] = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                continue
            fields[name] = value
    return fields


class TraceContextFilter(logging.Filter):
    """Inject the current request trace id into every log record."""

    name = _TRACE_FILTER_NAME

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = get_current_trace_id() or "-"
        return True


class JsonTraceFormatter(logging.Formatter):
    """Small JSON formatter used when ``logging.enhance.format=json``."""

    _deerflow_trace_formatter = True

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "trace_id"):
            record.trace_id = get_current_trace_id() or "-"
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "logger": record.name,
            "level": record.levelname,
            "trace_id": record.trace_id,
            "message": record.getMessage(),
        }
        payload.update(_structured_log_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)
        return json.dumps(payload, ensure_ascii=False)


class TraceTextFormatter(logging.Formatter):
    """Text formatter with explicit production telemetry fields."""

    _deerflow_trace_formatter = True

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        fields = _structured_log_fields(record)
        if not fields:
            return rendered
        suffix = " ".join(f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in fields.items())
        return f"{rendered} {suffix}"


def _ensure_root_handler() -> None:
    if logging.root.handlers:
        return
    logging.basicConfig(level=logging.INFO, format=DEFAULT_LOG_FORMAT, datefmt=DEFAULT_LOG_DATE_FORMAT)


def _has_trace_filter(handler: logging.Handler) -> bool:
    return any(getattr(f, "name", None) == _TRACE_FILTER_NAME or isinstance(f, TraceContextFilter) for f in handler.filters)


def _install_trace_filter(handler: logging.Handler) -> None:
    if not _has_trace_filter(handler):
        handler.addFilter(TraceContextFilter())


def _remove_trace_filter(handler: logging.Handler) -> None:
    handler.filters = [f for f in handler.filters if not (getattr(f, "name", None) == _TRACE_FILTER_NAME or isinstance(f, TraceContextFilter))]


def _default_formatter() -> logging.Formatter:
    return logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_LOG_DATE_FORMAT)


def _trace_formatter(format_name: str | None) -> logging.Formatter:
    if (format_name or "text").strip().lower() == "json":
        return JsonTraceFormatter()
    return TraceTextFormatter(TRACE_TEXT_LOG_FORMAT, datefmt=DEFAULT_LOG_DATE_FORMAT)


def configure_logging(config: object) -> None:
    """Configure ActWeave logging from an AppConfig-like object.

    With logging enhancement disabled this preserves the previous
    ``basicConfig + apply_logging_level`` behavior. With enhancement enabled,
    root handlers gain a trace-context filter and a formatter that includes
    ``trace_id`` plus an explicit allowlist of operational telemetry fields.
    """
    _ensure_root_handler()

    logging_config = getattr(config, "logging", None)
    enhance = getattr(logging_config, "enhance", None)
    enhanced = bool(getattr(enhance, "enabled", False))

    for handler in logging.root.handlers:
        if enhanced:
            _install_trace_filter(handler)
            handler.setFormatter(_trace_formatter(getattr(enhance, "format", "text")))
        else:
            _remove_trace_filter(handler)
            if getattr(handler.formatter, "_deerflow_trace_formatter", False):
                handler.setFormatter(_default_formatter())

    apply_logging_level(getattr(config, "log_level", None))
