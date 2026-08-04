from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkUnavailable
from app.shared_assets.mcp_discovery_repository import McpToolDiscoveryAttemptRecord
from app.worker.service import JobOutcome, JobSettlement
from deerflow.persistence.jobs.sql import JobClaim, JobScope


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


def _claim() -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease-token",
        job_type="mcp_discovery",
        scope=JobScope(uuid.uuid4(), str(uuid.uuid4())),
        run_id=None,
        occurrence_id=None,
        retry_safety="unsafe",
        cancel_requested=False,
        origin_trace_id=None,
    )


def _attempt(claim: JobClaim) -> McpToolDiscoveryAttemptRecord:
    from datetime import UTC, datetime

    return McpToolDiscoveryAttemptRecord(
        attempt_id=claim.job_id,
        project_id=claim.scope.project_id,
        mcp_server_id=uuid.uuid4(),
        mcp_server_version_id=uuid.uuid4(),
        requested_by_user_id=claim.scope.owner_user_id or "",
        trigger="manual",
        payload_checksum="a" * 64,
        grant_digest="b" * 64,
        status="running",
        requested_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=None,
        public_error_code=None,
        revision=1,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "result_status", "error_code"),
    [
        (None, "succeeded", None),
        (PrivateWorkUnavailable("secret detail"), "failed", "mcp_discovery_unavailable"),
        (PrivateWorkAssetStale("secret detail"), "failed", "mcp_catalog_invalid"),
    ],
)
async def test_discovery_job_maps_worker_result_to_one_atomic_success_settlement(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None,
    result_status: str,
    error_code: str | None,
) -> None:
    from app.worker.mcp_discovery import McpToolDiscoveryJobHandler

    claim = _claim()
    attempt = _attempt(claim)
    authority = SimpleNamespace(cancel_requested=False, heartbeat=AsyncMock())
    tools = (SimpleNamespace(provider_name="maps_weather", description="Weather"),)
    settlement = JobSettlement(JobOutcome.succeeded(), AsyncMock())
    handler = McpToolDiscoveryJobHandler(
        lambda: None,
        endpoint_policy=SimpleNamespace(),
        http_client_factory=object(),
        discovery_timeout_seconds=5,
    )
    handler._load_attempt = AsyncMock(return_value=attempt)
    handler._resolve_and_materialize = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(by_slot={})))
    handler._discover = AsyncMock(side_effect=error or None, return_value=tools)
    handler._settlement = AsyncMock(return_value=settlement)

    result = await handler(claim, authority)

    assert result is settlement
    authority.heartbeat.assert_awaited()
    handler._settlement.assert_awaited_once()
    call = handler._settlement.await_args
    assert call.kwargs["result_status"] == result_status
    assert call.kwargs["error_code"] == error_code
    assert call.kwargs["tools"] == (tools if error is None else None)
    assert call.args[:2] == (claim, attempt)
    assert settlement.outcome.status == "succeeded"


@pytest.mark.anyio
async def test_discovery_job_rejects_wrong_claim_without_loading_credentials() -> None:
    from app.worker.mcp_discovery import McpToolDiscoveryJobHandler

    claim = _claim()
    object.__setattr__(claim, "job_type", "private_run")
    handler = McpToolDiscoveryJobHandler(
        lambda: None,
        endpoint_policy=SimpleNamespace(),
        http_client_factory=object(),
        discovery_timeout_seconds=5,
    )
    handler._load_attempt = AsyncMock()

    result = await handler(
        claim,
        SimpleNamespace(cancel_requested=False, heartbeat=AsyncMock()),
    )

    assert result == JobOutcome.cancelled()
    handler._load_attempt.assert_not_awaited()


@pytest.mark.anyio
async def test_discovery_settlement_atomically_writes_inventory_attempt_and_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.worker.mcp_discovery as module

    claim = _claim()
    attempt = _attempt(claim)
    session = _Session()
    events: list[tuple[str, object]] = []

    class Inventory:
        def __init__(self, value) -> None:
            assert value is session

        async def record_success(self, **kwargs) -> None:
            events.append(("inventory", kwargs))

    class Attempts:
        def __init__(self, value) -> None:
            assert value is session

        async def mark_result(self, *args) -> None:
            events.append(("attempt", args))

    class Jobs:
        async def settle_success(self, *args, **kwargs) -> bool:
            events.append(("job", (args, kwargs)))
            return True

    monkeypatch.setattr(module, "McpToolInventoryRepository", Inventory)
    monkeypatch.setattr(module, "McpToolDiscoveryAttemptRepository", Attempts)
    monkeypatch.setattr(
        module,
        "_validate_project_mcp_snapshot_policy",
        lambda *args, **kwargs: None,
    )
    handler = module.McpToolDiscoveryJobHandler(
        lambda: session,
        endpoint_policy=SimpleNamespace(),
        http_client_factory=object(),
        discovery_timeout_seconds=5,
        job_repository_builder=lambda value: Jobs(),
    )
    handler._current_snapshot_in_session = AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace()))
    tools = (SimpleNamespace(provider_name="maps_weather", description="Weather"),)

    settlement = await handler._settlement(
        claim,
        attempt,
        result_status="succeeded",
        error_code=None,
        tools=tools,
    )
    await settlement.commit()

    assert [name for name, _value in events] == ["inventory", "attempt", "job"]
    assert events[1][1] == (attempt.attempt_id, "succeeded", None)
    assert events[2][1][0] == (claim.job_id,)
    assert events[2][1][1]["lease_token"] == claim.lease_token
