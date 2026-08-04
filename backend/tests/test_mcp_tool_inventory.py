from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetStorageUnavailable
from app.shared_assets.mcp_discovery_repository import McpToolDiscoveryAttemptRecord
from app.shared_assets.mcp_service import _mcp_tool_inventory_view
from app.shared_assets.mcp_tool_inventory_repository import (
    McpToolInventoryRecord,
    mcp_grant_closure_digest,
    normalize_mcp_tool_inventory,
)


class _Args(BaseModel):
    query: str


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return self


def test_inventory_projection_uses_provider_names_and_sanitizes_descriptions() -> None:
    from app.private_work.asset_runtime import (
        _DiscoveredMcpTool,
        _mcp_tool_inventory_payload,
    )

    version_id = uuid.uuid4()
    tools = (
        _DiscoveredMcpTool(
            version_id=version_id,
            name=f"project_{version_id.hex[:16]}_maps_weather",
            provider_name="maps_weather",
            description="Weather\u202e\n  by city\x00",
            args_schema=_Args,
        ),
    )

    assert _mcp_tool_inventory_payload(tools) == (
        {
            "name": "maps_weather",
            "description": "Weather by city",
        },
    )


def test_grant_closure_digest_is_order_independent_and_contains_no_ids() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    digest = mcp_grant_closure_digest((second, first))

    assert digest == mcp_grant_closure_digest((first, second))
    assert len(digest) == 64
    assert str(first) not in digest
    assert str(second) not in digest


def test_inventory_normalization_rejects_duplicate_or_unbounded_provider_data() -> None:
    valid = {"name": "maps_weather", "description": "Weather by city"}

    assert normalize_mcp_tool_inventory([valid]) == (valid,)
    for invalid in (
        [valid, valid],
        [{"name": "maps weather", "description": "invalid name"}],
        [{"name": "maps_weather", "description": "x" * 4_097}],
        [{"name": "maps_weather", "description": "ok", "url": "secret"}],
    ):
        try:
            normalize_mcp_tool_inventory(invalid)
        except ValueError:
            pass
        else:  # pragma: no cover - explicit bounded-contract assertion
            raise AssertionError("invalid tool inventory was accepted")


def test_inventory_projection_distinguishes_ready_degraded_failed_and_stale() -> None:
    grant_id = uuid.uuid4()
    grant_digest = mcp_grant_closure_digest((grant_id,))
    checksum = "a" * 64
    now = datetime.now(UTC)
    tools = ({"name": "maps_weather", "description": "Weather by city"},)

    assert (
        _mcp_tool_inventory_view(
            payload_checksum=checksum,
            active_grant_ids=(grant_id,),
            inventory=None,
        ).status
        == "never_discovered"
    )

    ready_record = McpToolInventoryRecord(
        attempt_payload_checksum=checksum,
        attempt_grant_digest=grant_digest,
        attempt_status="ready",
        public_error_code=None,
        tools=tools,
        tools_payload_checksum=checksum,
        tools_grant_digest=grant_digest,
        last_attempt_at=now,
        last_success_at=now,
    )
    ready = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=ready_record,
    )
    assert ready.status == "ready"
    assert [tool.name for tool in ready.tools] == ["maps_weather"]

    degraded = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=replace(
            ready_record,
            attempt_status="failed",
            public_error_code="mcp_discovery_unavailable",
            last_attempt_at=now + timedelta(seconds=1),
        ),
    )
    assert degraded.status == "degraded"
    assert degraded.error_code == "mcp_discovery_unavailable"
    assert [tool.name for tool in degraded.tools] == ["maps_weather"]

    failed = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=McpToolInventoryRecord(
            attempt_payload_checksum=checksum,
            attempt_grant_digest=grant_digest,
            attempt_status="failed",
            public_error_code="mcp_catalog_invalid",
            tools=(),
            tools_payload_checksum=None,
            tools_grant_digest=None,
            last_attempt_at=now,
            last_success_at=None,
        ),
    )
    assert failed.status == "failed"
    assert failed.tools == ()

    stale = _mcp_tool_inventory_view(
        payload_checksum="b" * 64,
        active_grant_ids=(grant_id,),
        inventory=ready_record,
    )
    assert stale.status == "stale"
    assert stale.tools == ()
    assert stale.error_code is None


def test_inventory_projection_reports_testing_and_preserves_only_matching_success() -> None:
    grant_id = uuid.uuid4()
    grant_digest = mcp_grant_closure_digest((grant_id,))
    checksum = "a" * 64
    now = datetime.now(UTC)
    ready_record = McpToolInventoryRecord(
        attempt_payload_checksum=checksum,
        attempt_grant_digest=grant_digest,
        attempt_status="ready",
        public_error_code=None,
        tools=({"name": "maps_weather", "description": "Weather by city"},),
        tools_payload_checksum=checksum,
        tools_grant_digest=grant_digest,
        last_attempt_at=now,
        last_success_at=now,
    )

    retained = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=ready_record,
        testing=True,
    )
    empty = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=None,
        testing=True,
    )

    assert retained.status == "testing"
    assert [tool.name for tool in retained.tools] == ["maps_weather"]
    assert retained.last_attempt_at == now
    assert retained.last_success_at == now
    assert retained.error_code is None
    assert empty.status == "testing"
    assert empty.tools == ()
    assert empty.last_attempt_at is None
    assert empty.last_success_at is None
    assert empty.error_code is None


def test_inventory_projection_uses_newer_failed_durable_attempt() -> None:
    grant_id = uuid.uuid4()
    grant_digest = mcp_grant_closure_digest((grant_id,))
    checksum = "a" * 64
    previous = datetime.now(UTC)
    failed_at = previous + timedelta(seconds=2)
    ready_record = McpToolInventoryRecord(
        attempt_payload_checksum=checksum,
        attempt_grant_digest=grant_digest,
        attempt_status="ready",
        public_error_code=None,
        tools=({"name": "maps_weather", "description": "Weather by city"},),
        tools_payload_checksum=checksum,
        tools_grant_digest=grant_digest,
        last_attempt_at=previous,
        last_success_at=previous,
    )
    failed_attempt = McpToolDiscoveryAttemptRecord(
        attempt_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        mcp_server_id=uuid.uuid4(),
        mcp_server_version_id=uuid.uuid4(),
        requested_by_user_id=str(uuid.uuid4()),
        trigger="manual",
        payload_checksum=checksum,
        grant_digest=grant_digest,
        status="failed",
        requested_at=previous + timedelta(seconds=1),
        started_at=previous + timedelta(seconds=1),
        completed_at=failed_at,
        public_error_code="mcp_discovery_unavailable",
        revision=1,
    )

    degraded = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=ready_record,
        latest_attempt=failed_attempt,
    )
    failed = _mcp_tool_inventory_view(
        payload_checksum=checksum,
        active_grant_ids=(grant_id,),
        inventory=None,
        latest_attempt=failed_attempt,
    )

    assert degraded.status == "degraded"
    assert degraded.error_code == "mcp_discovery_unavailable"
    assert [tool.name for tool in degraded.tools] == ["maps_weather"]
    assert degraded.last_attempt_at == failed_at
    assert degraded.last_success_at == previous
    assert failed.status == "failed"
    assert failed.error_code == "mcp_discovery_unavailable"
    assert failed.tools == ()
    assert failed.last_attempt_at == failed_at


def test_inventory_projection_ignores_cancelled_or_older_failed_attempt() -> None:
    grant_id = uuid.uuid4()
    grant_digest = mcp_grant_closure_digest((grant_id,))
    checksum = "a" * 64
    now = datetime.now(UTC)
    ready_record = McpToolInventoryRecord(
        attempt_payload_checksum=checksum,
        attempt_grant_digest=grant_digest,
        attempt_status="ready",
        public_error_code=None,
        tools=({"name": "maps_weather", "description": "Weather by city"},),
        tools_payload_checksum=checksum,
        tools_grant_digest=grant_digest,
        last_attempt_at=now,
        last_success_at=now,
    )

    def attempt(status: str, completed_at: datetime):
        return McpToolDiscoveryAttemptRecord(
            attempt_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            mcp_server_id=uuid.uuid4(),
            mcp_server_version_id=uuid.uuid4(),
            requested_by_user_id=str(uuid.uuid4()),
            trigger="manual",
            payload_checksum=checksum,
            grant_digest=grant_digest,
            status=status,  # type: ignore[arg-type]
            requested_at=completed_at - timedelta(seconds=1),
            started_at=completed_at - timedelta(seconds=1),
            completed_at=completed_at,
            public_error_code=("mcp_discovery_unavailable" if status == "failed" else None),
            revision=1,
        )

    for latest in (
        attempt("cancelled", now + timedelta(seconds=2)),
        attempt("failed", now - timedelta(seconds=1)),
    ):
        view = _mcp_tool_inventory_view(
            payload_checksum=checksum,
            active_grant_ids=(grant_id,),
            inventory=ready_record,
            latest_attempt=latest,
        )
        assert view.status == "ready"
        assert view.error_code is None


@pytest.mark.asyncio
async def test_inventory_storage_corruption_maps_to_safe_domain_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.shared_assets.mcp_service as service_module

    actor = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.VIEWER,
        capabilities=capabilities_for(ProjectRole.VIEWER),
        membership_version=1,
        request_id="req-corrupt-inventory",
    )
    asset_id = uuid.uuid4()
    version_id = uuid.uuid4()
    session = _Session()
    repository = SimpleNamespace(
        session=session,
        get_project_visible_version=AsyncMock(
            return_value=SimpleNamespace(
                row=SimpleNamespace(payload_checksum="a" * 64),
                grants=(),
            )
        ),
    )
    broken_inventory = SimpleNamespace(
        get=AsyncMock(side_effect=ValueError("private invalid JSON detail")),
    )
    monkeypatch.setattr(
        service_module,
        "McpRepository",
        lambda _session: repository,
    )
    monkeypatch.setattr(
        service_module,
        "McpToolInventoryRepository",
        lambda _session: broken_inventory,
    )

    with pytest.raises(AssetStorageUnavailable) as exc_info:
        await service_module.McpService(lambda: session).get_tool_inventory(
            actor,
            asset_id,
            version_id,
        )

    assert exc_info.value.request_id == actor.request_id
