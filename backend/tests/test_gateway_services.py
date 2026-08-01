"""Tests for the surviving Gateway/private-work HTTP helpers."""

from __future__ import annotations

import json

from app.private_work.http_runtime import format_sse
from app.private_work.runtime_context import prepare_private_run_config


def test_format_sse_basic() -> None:
    frame = format_sse("metadata", {"run_id": "abc"})

    assert frame.startswith("event: metadata\n")
    parsed = json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0])
    assert parsed == {"run_id": "abc"}


def test_format_sse_with_event_id_places_id_first() -> None:
    frame = format_sse("metadata", {"run_id": "abc"}, event_id="123")

    assert frame == 'id: 123\nevent: metadata\ndata: {"run_id":"abc"}\n\n'


def test_format_sse_end_event_null() -> None:
    assert format_sse("end", None) == "event: end\ndata: null\n\n"


def test_format_sse_no_event_id() -> None:
    assert "id:" not in format_sse("values", {"x": 1})


def test_sanitize_log_param_strips_control_characters() -> None:
    from app.gateway.utils import sanitize_log_param

    assert sanitize_log_param("thread\nid\rwith\x00controls") == "threadidwithcontrols"


def test_private_run_config_clamps_recursion_and_strips_client_authority() -> None:
    scope = object()
    config = prepare_private_run_config(
        thread_id="thread-safe",
        opaque_scope=scope,
        request_config={
            "recursion_limit": 100_000_000,
            "context": {
                "project_id": "forged-project",
                "owner_user_id": "forged-owner",
                "safe_hint": "keep",
            },
            "configurable": {
                "thread_id": "forged-thread",
                "membership_version": 999,
                "safe_value": 1,
            },
        },
        metadata={"role": "admin", "safe": "value"},
        body_context={"user_id": "forged-user", "mode_hint": "keep"},
    )

    assert config["recursion_limit"] == 100_000
    assert config["configurable"] == {
        "safe_value": 1,
        "thread_id": "thread-safe",
    }
    assert config["context"] == {
        "safe_hint": "keep",
        "mode_hint": "keep",
        "thread_id": "thread-safe",
        "private_scope": scope,
    }
    assert config["metadata"] == {"safe": "value"}
