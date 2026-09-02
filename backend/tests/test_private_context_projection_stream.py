from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.gateway.routers.private_work_routes import context_controls
from app.gateway.routers.private_work_routes.context_controls import _context_projection_sse_consumer
from deerflow.runtime.context_evidence import ContextProjectionHead


def _projection(*, projection_seq: str, execution_id: str | None = None) -> ContextProjectionHead:
    subject = {
        "kind": "lead_thread" if execution_id is None else "subagent_task",
        "thread_id": "11111111-1111-4111-8111-111111111111",
        "execution_id": execution_id,
    }
    return ContextProjectionHead.from_safe_mapping(
        {
            "contract_version": 2,
            "thread_id": subject["thread_id"],
            "subject": subject,
            "phase": "active" if execution_id is None else "settled",
            "projection_seq": projection_seq,
            "evidence_seq": "9",
            "context_window_generation": "22222222-2222-4222-8222-222222222222",
            "checkpoint_id": "checkpoint-1",
            "projector_revision": "context-projector-v1",
            "model": {
                "identity_digest": "a" * 64,
                "context_window_tokens": 300_000,
            },
            "basis": "estimated",
            "coverage": "complete",
            "freshness": "current",
            "totals": {
                "projected_tokens": 1_000,
                "lower_bound_tokens": 1_000,
                "safety_upper_bound_tokens": 1_100,
                "context_window_tokens": 300_000,
                "remaining_tokens": 299_000,
                "progress_percent": 0.3,
            },
            "lanes": [
                {
                    "lane": "conversation",
                    "projected_tokens": 1_000,
                    "lower_bound_tokens": 1_000,
                    "safety_upper_bound_tokens": 1_100,
                }
            ],
            "last_provider_observation": None,
            "compaction": {
                "enabled": True,
                "threshold_tokens": 240_000,
                "reached": False,
                "authority": "frozen_run",
                "blocked_reason": None,
            },
            "notices": [],
            "as_of": "2026-08-27T00:00:00Z",
        }
    )


class _ProjectionService:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def context_projection_updates(
        self,
        _context: object,
        _thread_id: str,
        *,
        after_projection_seq: int,
    ) -> tuple[ContextProjectionHead, ...]:
        self.calls.append(after_projection_seq)
        if len(self.calls) == 1:
            return (
                _projection(projection_seq="7"),
                _projection(
                    projection_seq="8",
                    execution_id="33333333-3333-4333-8333-333333333333",
                ),
            )
        return ()


class _Request:
    def __init__(self) -> None:
        self.checks = 0

    async def is_disconnected(self) -> bool:
        self.checks += 1
        return self.checks >= 2


def _route_request(*, last_event_id: str | None = None) -> Request:
    headers = []
    if last_event_id is not None:
        headers.append((b"last-event-id", last_event_id.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/context-usage/stream",
            "raw_path": b"/context-usage/stream",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "app": SimpleNamespace(),
        }
    )


class _ProjectionRouteService:
    def __init__(self) -> None:
        self.calls: list[int] = []

    async def context_projection_updates(
        self,
        _context: object,
        _thread_id: str,
        *,
        after_projection_seq: int,
    ) -> tuple[ContextProjectionHead, ...]:
        self.calls.append(after_projection_seq)
        return ()


@pytest.mark.asyncio
async def test_context_projection_stream_replays_thread_wide_subject_heads() -> None:
    service = _ProjectionService()
    frames = [
        frame
        async for frame in _context_projection_sse_consumer(
            service=service,
            context=SimpleNamespace(request_id="projection-stream"),
            thread_id="11111111-1111-4111-8111-111111111111",
            request=_Request(),
            cursor=6,
            poll_seconds=0,
        )
    ]

    assert service.calls == [6, 8]
    assert len(frames) == 2
    assert frames[0].startswith("id: 7\nevent: context.projection.updated.v2\n")
    assert frames[1].startswith("id: 8\nevent: context.projection.updated.v2\n")
    first_payload = json.loads(next(line.removeprefix("data: ") for line in frames[0].splitlines() if line.startswith("data: ")))
    assert first_payload["projection_seq"] == "7"
    assert first_payload["subject"]["kind"] == "lead_thread"
    assert first_payload["subject"]["execution_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_cursor", "header_cursor", "expected"),
    [
        ("9223372036854775806", "9223372036854775807", 9_223_372_036_854_775_807),
        ("9223372036854775807", "12", 9_223_372_036_854_775_807),
    ],
)
async def test_context_projection_stream_resumes_from_the_greater_valid_cursor(
    monkeypatch: pytest.MonkeyPatch,
    query_cursor: str,
    header_cursor: str,
    expected: int,
) -> None:
    service = _ProjectionRouteService()
    monkeypatch.setattr(
        context_controls,
        "_chat_control_service",
        lambda _request, _request_id: service,
    )

    response = await context_controls.stream_private_thread_context_usage(
        uuid.UUID("11111111-1111-4111-8111-111111111111"),
        _route_request(last_event_id=header_cursor),
        after_seq=query_cursor,
        context=SimpleNamespace(request_id="projection-stream-route"),
    )

    assert response.media_type == "text/event-stream"
    assert service.calls == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_cursor", "header_cursor"),
    [
        ("01", None),
        ("9223372036854775808", None),
        ("0", "01"),
        ("0", "9223372036854775808"),
        ("0", "-1"),
    ],
)
async def test_context_projection_stream_rejects_each_noncanonical_or_oversized_cursor(
    monkeypatch: pytest.MonkeyPatch,
    query_cursor: str,
    header_cursor: str | None,
) -> None:
    service = _ProjectionRouteService()
    monkeypatch.setattr(
        context_controls,
        "_chat_control_service",
        lambda _request, _request_id: service,
    )

    with pytest.raises(HTTPException) as raised:
        await context_controls.stream_private_thread_context_usage(
            uuid.UUID("11111111-1111-4111-8111-111111111111"),
            _route_request(last_event_id=header_cursor),
            after_seq=query_cursor,
            context=SimpleNamespace(request_id="projection-stream-route"),
        )

    assert raised.value.status_code == 422
    assert service.calls == []
