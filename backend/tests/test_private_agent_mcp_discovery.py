from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.private_work.asset_runtime_contracts import (
    PrivateAgentManifest,
    PrivateMcpManifest,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkAssetStale, PrivateWorkUnavailable
from app.private_work.mcp_runtime_contracts import DiscoveredMcpTool
from app.private_work.private_agent_runtime import PrivateAgentRuntime
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.models import AssetKind, AssetScope, ResolvedMcpSnapshot
from deerflow.agents.lead_agent.prompt import apply_prompt_template
from deerflow.sandbox.sandbox import AuthorizationRevoked


class _Args(BaseModel):
    value: str


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="private-mcp-discovery-test",
        )
    )


def _snapshot() -> ResolvedMcpSnapshot:
    return ResolvedMcpSnapshot(
        kind=AssetKind.MCP,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        checksum="a" * 64,
        catalog_generation=1,
        dependency_version_ids=(),
        definition={"transport": "http", "url": "https://mcp.invalid/mcp"},
        secret_generation_ids=(),
        secret_digest="",
    )


def _runtime(
    tmp_path: Path,
    snapshots: tuple[ResolvedMcpSnapshot, ...],
) -> PrivateAgentRuntime:
    manifest = PrivateAgentManifest(
        agent_asset_id=uuid.uuid4(),
        agent_version_id=uuid.uuid4(),
        checksum="b" * 64,
        catalog_generation=1,
        description="MCP degradation test",
        payload_schema_version=3,
        agents_instructions="",
        soul="",
        identity="",
        user_context="",
        model_ref="test-model",
        tool_groups=(),
        skills=(),
        mcps=tuple(
            PrivateMcpManifest(
                asset_id=snapshot.asset_id,
                version_id=snapshot.version_id,
                definition=dict(snapshot.definition),
            )
            for snapshot in snapshots
        ),
    )
    return PrivateAgentRuntime(
        context=_context(),
        run_id="private-mcp-discovery-run",
        resolver=object(),  # type: ignore[arg-type]
        session_factory=object(),  # type: ignore[arg-type]
        safe_manifest=manifest,
        skill_root=tmp_path,
        skills=(),
        mcp_snapshots=snapshots,
        authorization_boundary=object(),
        run_session_reuse=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    (
        (ConnectionError("remote-connection-sentinel"), "mcp_discovery_unavailable"),
        (PrivateWorkUnavailable("remote-discovery"), "mcp_discovery_unavailable"),
        (PrivateWorkAssetStale("remote-catalog"), "mcp_catalog_invalid"),
    ),
)
async def test_remote_mcp_discovery_failure_isolated_from_healthy_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: Exception,
    expected_code: str,
) -> None:
    failed, healthy = _snapshot(), _snapshot()
    runtime = _runtime(tmp_path, (failed, healthy))
    inventory: list[tuple[uuid.UUID, str | None, tuple[str, ...]]] = []

    async def invoke_with_material(self, version_id, operation):  # type: ignore[no-untyped-def]
        del self
        return await operation({}, {})

    async def discover_exact(cls, version_id, *_args, **_kwargs):  # type: ignore[no-untyped-def]
        del cls
        if version_id == failed.version_id:
            raise failure
        return (
            DiscoveredMcpTool(
                version_id=version_id,
                name="healthy_lookup",
                provider_name="lookup",
                description="Healthy lookup",
                args_schema=_Args,
            ),
        )

    async def record_inventory(
        self,
        snapshot,
        *,
        tools=None,
        error_code=None,
    ):  # type: ignore[no-untyped-def]
        del self
        inventory.append(
            (
                snapshot.version_id,
                error_code,
                tuple(tool.name for tool in (tools or ())),
            )
        )

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "invoke_with_mcp_material",
        invoke_with_material,
    )
    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_discover_exact_mcp",
        classmethod(discover_exact),
    )
    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_record_mcp_tool_inventory",
        record_inventory,
    )

    await runtime.discover_mcp_tools()

    assert [tool.name for tool in runtime.mcp_tools] == ["healthy_lookup"]
    assert runtime.capability_issues == (expected_code,)
    assert "<runtime_capability_status>" in runtime.capability_notice
    assert "Continue with available capabilities" in runtime.capability_notice
    assert "mcp.invalid" not in runtime.capability_notice
    assert str(failed.asset_id) not in runtime.capability_notice
    assert "remote-connection-sentinel" not in runtime.capability_notice
    request_id = getattr(failure, "request_id", None)
    if isinstance(request_id, str):
        assert request_id not in runtime.capability_notice
    assert inventory == [
        (failed.version_id, expected_code, ()),
        (healthy.version_id, None, ("healthy_lookup",)),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        PrivateWorkUnavailable("materialization-unavailable"),
        PrivateWorkAssetStale("snapshot-stale"),
        AuthorizationRevoked(),
    ),
)
async def test_pre_discovery_authority_and_materialization_failures_remain_fatal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
) -> None:
    runtime = _runtime(tmp_path, (_snapshot(),))

    async def invoke_with_material(self, _version_id, _operation):  # type: ignore[no-untyped-def]
        del self
        raise failure

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "invoke_with_mcp_material",
        invoke_with_material,
    )

    with pytest.raises(type(failure)):
        await runtime.discover_mcp_tools()


def test_runtime_capability_notice_is_injected_once_into_lead_prompt() -> None:
    notice = "<runtime_capability_status>safe capability status</runtime_capability_status>"

    prompt = apply_prompt_template(runtime_capability_notice=notice)

    assert prompt.count(notice) == 1
