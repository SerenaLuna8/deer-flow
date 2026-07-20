from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from scripts.release_acceptance.live_probe import (
    LiveProbeRequest,
    LiveProbeResult,
    PostgresLiveProbe,
    run_live_probe_handoff,
)
from scripts.release_acceptance.models import LiveModelSummary


def _valid_live_summary() -> dict[str, object]:
    return {
        "provider": "deepseek",
        "logical_model_name": "release-live",
        "provider_model_id": "deepseek-v4-pro",
        "outcome": "completed",
        "frame_count": 4,
        "tool_call_count": 1,
        "terminal_count": 1,
        "cursor_count": 4,
        "duration_ms": 1200,
    }


@pytest.mark.parametrize(
    ("forbidden_key", "forbidden_value"),
    [
        ("content", "provider text"),
        ("reasoning", "provider reasoning"),
        ("tool_result", "raw tool result"),
        ("run_id", str(uuid.uuid4())),
        ("thread_id", str(uuid.uuid4())),
        ("file_id", str(uuid.uuid4())),
    ],
)
def test_live_summary_rejects_model_body_and_business_ids(
    forbidden_key: str,
    forbidden_value: str,
) -> None:
    with pytest.raises(ValidationError):
        LiveModelSummary.model_validate({**_valid_live_summary(), forbidden_key: forbidden_value})


@pytest.mark.parametrize(
    "invalid_counts",
    [
        {"frame_count": 1},
        {"tool_call_count": 0},
        {"terminal_count": 0},
        {"terminal_count": 2},
        {"cursor_count": 1},
    ],
)
def test_completed_live_summary_requires_full_durable_tool_proof(
    invalid_counts: dict[str, int],
) -> None:
    with pytest.raises(ValidationError):
        LiveModelSummary.model_validate({**_valid_live_summary(), **invalid_counts})


class _FakeLiveConnection:
    def __init__(
        self,
        event_counts: Mapping[str, Any],
        artifact_counts: Mapping[str, Any],
    ) -> None:
        self._rows = [event_counts, artifact_counts]
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    async def fetchrow(self, query: str, *args: object):
        self.calls.append((query, args))
        return self._rows.pop(0)

    async def close(self) -> None:
        self.closed = True


def _live_request() -> LiveProbeRequest:
    return LiveProbeRequest(
        project_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        thread_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
    )


@pytest.mark.asyncio
async def test_postgres_live_probe_requires_scoped_stream_tool_and_artifact() -> None:
    artifact_id = uuid.uuid4()
    connection = _FakeLiveConnection(
        {
            "frame_count": 5,
            "tool_count": 1,
            "terminal_count": 1,
            "cursor_count": 5,
        },
        {"artifact_count": 1, "artifact_id": str(artifact_id)},
    )

    async def connect(database_url: str):
        assert database_url == "postgresql://m8-app@127.0.0.1/m8-live"
        return connection

    request = _live_request()
    result = await PostgresLiveProbe(connect=connect).inspect(
        "postgresql+asyncpg://m8-app@127.0.0.1/m8-live",
        request,
    )

    assert result.frame_count == 5
    assert result.tool_call_count == 1
    assert result.terminal_count == 1
    assert result.cursor_count == 5
    assert result.artifact_id == artifact_id
    assert connection.closed is True
    assert len(connection.calls) == 2
    for query, args in connection.calls:
        assert "project_id" in query
        assert "owner_user_id" in query
        assert "thread_id" in query
        assert "run_id" in query
        assert args == (
            request.project_id,
            str(request.owner_user_id),
            str(request.thread_id),
            str(request.run_id),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("events", "artifacts"),
    [
        (
            {
                "frame_count": 1,
                "tool_count": 1,
                "terminal_count": 1,
                "cursor_count": 1,
            },
            {"artifact_count": 1, "artifact_id": str(uuid.uuid4())},
        ),
        (
            {
                "frame_count": 4,
                "tool_count": 0,
                "terminal_count": 1,
                "cursor_count": 4,
            },
            {"artifact_count": 1, "artifact_id": str(uuid.uuid4())},
        ),
        (
            {
                "frame_count": 4,
                "tool_count": 1,
                "terminal_count": 2,
                "cursor_count": 4,
            },
            {"artifact_count": 1, "artifact_id": str(uuid.uuid4())},
        ),
        (
            {
                "frame_count": 4,
                "tool_count": 1,
                "terminal_count": 1,
                "cursor_count": 4,
            },
            {"artifact_count": 0, "artifact_id": None},
        ),
    ],
)
async def test_postgres_live_probe_fails_closed_on_incomplete_authority(
    events: Mapping[str, Any],
    artifacts: Mapping[str, Any],
) -> None:
    connection = _FakeLiveConnection(events, artifacts)

    async def connect(_database_url: str):
        return connection

    with pytest.raises(RuntimeError, match="M8_LIVE_PROBE_FAILED"):
        await PostgresLiveProbe(connect=connect).inspect(
            "postgresql://m8-app@127.0.0.1/m8-live",
            _live_request(),
        )
    assert connection.closed is True


@pytest.mark.asyncio
async def test_live_probe_handoff_replaces_raw_exception_without_echo() -> None:
    request = _live_request()

    class FailingProbe:
        async def inspect(self, database_url: str, selected: LiveProbeRequest):
            assert database_url == "postgresql" + "://app:secret@127.0.0.1/live"
            assert selected == request
            raise RuntimeError("provider body sk-test-secret and /private/raw/location")

    result = await run_live_probe_handoff(
        request.model_dump_json().encode(),
        {"M8_LIVE_DATABASE_URL": "postgresql" + "://app:secret@127.0.0.1/live"},
        probe=FailingProbe(),
    )

    encoded = result.model_dump_json()
    assert result.status == "failed"
    assert result.code == "M8_LIVE_PROBE_FAILED"
    assert "secret" not in encoded
    assert "/private" not in encoded
    assert str(request.run_id) not in encoded


@pytest.mark.asyncio
async def test_live_probe_handoff_success_is_closed_and_bounded() -> None:
    artifact_id = uuid.uuid4()

    class PassingProbe:
        async def inspect(
            self,
            _database_url: str,
            _selected: LiveProbeRequest,
        ) -> LiveProbeResult:
            return LiveProbeResult(
                frame_count=5,
                tool_call_count=1,
                terminal_count=1,
                cursor_count=5,
                artifact_id=artifact_id,
            )

    result = await run_live_probe_handoff(
        _live_request().model_dump_json().encode(),
        {"M8_LIVE_DATABASE_URL": "postgresql://m8-app@127.0.0.1/live"},
        probe=PassingProbe(),
    )

    assert result.status == "passed"
    assert result.frame_count == 5
    assert result.artifact_id == artifact_id
    with pytest.raises(ValidationError):
        type(result).model_validate({**result.model_dump(), "content": "forbidden provider body"})


@pytest.mark.asyncio
async def test_live_probe_handoff_rejects_oversized_or_missing_database_input() -> None:
    oversized = await run_live_probe_handoff(
        b"x" * 4097,
        {"M8_LIVE_DATABASE_URL": "postgresql://m8-app@127.0.0.1/live"},
    )
    missing_database = await run_live_probe_handoff(
        _live_request().model_dump_json().encode(),
        {},
    )
    assert oversized.status == "failed"
    assert missing_database.status == "failed"
