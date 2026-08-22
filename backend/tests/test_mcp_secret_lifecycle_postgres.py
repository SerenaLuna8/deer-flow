from __future__ import annotations

import base64
import json
import uuid

import pytest
from sqlalchemy import func, select
from support.private_thread_seed import seed_private_thread_database

from app.audit.service import AuditService
from app.private_work.snapshot_repository import (
    RunSnapshotAssetStale,
    RunSnapshotRepository,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.audit import DurableSharedAssetGovernanceEventSink
from app.shared_assets.mcp_discovery_repository import (
    McpToolDiscoveryAttemptRepository,
)
from app.shared_assets.mcp_secret_closure import lock_mcp_secret_closure
from app.shared_assets.mcp_secret_service import McpSecretService
from app.shared_assets.mcp_service import (
    CreateMcpServer,
    McpDefinition,
    McpSecretSlot,
    McpService,
)
from app.shared_assets.mcp_tool_inventory_repository import (
    McpToolInventoryRepository,
)
from deerflow.mcp_definition_policy import ExactMcpEndpointPolicy
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.shared_assets import (
    McpSecretSlotRow,
    McpServerRow,
    McpServerVersionRow,
    ProjectMcpSecretGenerationRow,
    ProjectMcpSecretStateRow,
    ProjectMcpSecretTombstoneRow,
)


def _definition(endpoint: str) -> McpDefinition:
    return McpDefinition(
        description="Project MCP Configuration Secret lifecycle",
        transport="http",
        url=endpoint,
        secret_slots=(
            McpSecretSlot(
                name="authorization",
                purpose="Authenticate the controlled fake recipient",
                payload_schema={"headers": ("Authorization",)},
                required=True,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_project_mcp_secret_copy_discovery_and_superseded_lifecycle(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"p" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    actor = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="project-mcp-secret-lifecycle",
    )
    endpoints = (
        "http://127.0.0.1:6553/mcp",
        "http://127.0.0.1:6553/mcp-v2",
        "http://127.0.0.1:6554/mcp",
    )
    audit_service = AuditService(
        seed.factory,
        AuditHmacKeyring("test", {"test": b"a" * 32}),
    )
    audit = DurableSharedAssetGovernanceEventSink(audit_service)
    mcp_service = McpService(
        seed.factory,
        audit,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset(endpoints)),
    )
    secrets = McpSecretService(seed.factory, audit)
    first_secret = "mcp-test-value-one"
    second_secret = "mcp-test-value-two"
    try:
        created = await mcp_service.create_project_configured(
            actor,
            CreateMcpServer(
                slug=f"secret-lifecycle-{uuid.uuid4().hex[:12]}",
                display_name="MCP secret lifecycle",
            ),
            _definition(endpoints[0]),
        )
        asset_id = created.asset.id
        version_one_id = created.version.id
        initial = await secrets.get(actor, asset_id, version_one_id)
        assert initial.readiness == "unready"

        configured = await secrets.replace(
            actor,
            asset_id,
            version_one_id,
            "authorization",
            {"headers": {"Authorization": first_secret}},
        )
        assert configured.readiness == "ready"
        assert first_secret not in repr(configured)
        async with seed.factory() as session:
            state_one = (
                await session.execute(
                    select(ProjectMcpSecretStateRow).where(
                        ProjectMcpSecretStateRow.project_id == actor.project_id,
                        ProjectMcpSecretStateRow.mcp_server_id == asset_id,
                        ProjectMcpSecretStateRow.mcp_server_version_id == version_one_id,
                    )
                )
            ).scalar_one()
            first_generation_id = state_one.current_generation_id
            first_attempt = await McpToolDiscoveryAttemptRepository(session).latest_for_version(
                actor.project_id,
                asset_id,
                version_one_id,
            )
            assert first_attempt is not None
            asset = await session.get(McpServerRow, asset_id)
            version = await session.get(McpServerVersionRow, version_one_id)
            slots = tuple((await session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == version_one_id))).scalars().all())
            assert asset is not None
            assert version is not None
            closure = await lock_mcp_secret_closure(
                session,
                project_id=actor.project_id,
                mcp_server_id=asset_id,
                mcp_server_version_id=version_one_id,
                slots=slots,
                request_id=actor.request_id,
            )
            with pytest.raises(RunSnapshotAssetStale):
                await RunSnapshotRepository._validate_mcp_discovery_readiness(
                    session,
                    [(asset, version)],
                    {version_one_id: closure},
                    project_id=actor.project_id,
                )
            await McpToolInventoryRepository(session).record_success(
                project_id=actor.project_id,
                mcp_server_id=asset_id,
                mcp_server_version_id=version_one_id,
                payload_checksum=version.payload_checksum,
                secret_digest=closure.digest,
                tools=(),
            )
            await RunSnapshotRepository._validate_mcp_discovery_readiness(
                session,
                [(asset, version)],
                {version_one_id: closure},
                project_id=actor.project_id,
            )

        replaced = await secrets.replace(
            actor,
            asset_id,
            version_one_id,
            "authorization",
            {"headers": {"Authorization": second_secret}},
        )
        assert replaced.readiness == "ready"
        async with seed.factory() as session:
            state_one = (
                await session.execute(
                    select(ProjectMcpSecretStateRow).where(
                        ProjectMcpSecretStateRow.project_id == actor.project_id,
                        ProjectMcpSecretStateRow.mcp_server_id == asset_id,
                        ProjectMcpSecretStateRow.mcp_server_version_id == version_one_id,
                    )
                )
            ).scalar_one()
            second_generation_id = state_one.current_generation_id
            second_attempt = await McpToolDiscoveryAttemptRepository(session).latest_for_version(
                actor.project_id,
                asset_id,
                version_one_id,
            )
            assert second_attempt is not None
            assert second_attempt.attempt_id != first_attempt.attempt_id
            assert second_attempt.secret_digest != first_attempt.secret_digest
            assert (
                await session.get(
                    ProjectMcpSecretGenerationRow,
                    first_generation_id,
                )
                is None
            )
            replacement_tombstone = (await session.execute(select(ProjectMcpSecretTombstoneRow).where(ProjectMcpSecretTombstoneRow.destroyed_generation_id == first_generation_id))).scalar_one()
            assert replacement_tombstone.reason == "replace"

        compatible = await mcp_service.update_project_configured(
            actor,
            asset_id,
            _definition(endpoints[1]),
            expected_asset_version=created.asset.version,
        )
        version_two_id = compatible.version.id
        copied = await secrets.get(actor, asset_id, version_two_id)
        assert copied.readiness == "ready"
        async with seed.factory() as session:
            states = tuple(
                (
                    await session.execute(
                        select(ProjectMcpSecretStateRow).where(
                            ProjectMcpSecretStateRow.project_id == actor.project_id,
                            ProjectMcpSecretStateRow.mcp_server_id == asset_id,
                            ProjectMcpSecretStateRow.mcp_server_version_id.in_((version_one_id, version_two_id)),
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_version = {state.mcp_server_version_id: state.current_generation_id for state in states}
            assert by_version[version_one_id] == second_generation_id
            assert by_version[version_two_id] not in {
                None,
                second_generation_id,
            }
            version_two_generation_id = by_version[version_two_id]
            version_two_attempt = await McpToolDiscoveryAttemptRepository(session).latest_for_version(
                actor.project_id,
                asset_id,
                version_two_id,
            )
            assert version_two_attempt is not None

        incompatible = await mcp_service.update_project_configured(
            actor,
            asset_id,
            _definition(endpoints[2]),
            expected_asset_version=compatible.asset.version,
        )
        version_three_id = incompatible.version.id
        not_copied = await secrets.get(actor, asset_id, version_three_id)
        assert not_copied.readiness == "unready"
        assert not_copied.slots[0].configured is False

        superseded_replaced = await secrets.replace(
            actor,
            asset_id,
            version_one_id,
            "authorization",
            {"headers": {"Authorization": "mcp-superseded-value"}},
        )
        assert superseded_replaced.readiness == "ready"
        superseded_discovery = await mcp_service.request_tool_discovery(
            actor,
            asset_id,
            version_one_id,
        )
        assert superseded_discovery.mcp_server_version_id == version_one_id
        superseded_cleared = await secrets.clear(
            actor,
            asset_id,
            version_one_id,
            "authorization",
            confirmed=True,
        )
        assert superseded_cleared.readiness == "unready"
        version_two_preserved = await secrets.get(
            actor,
            asset_id,
            version_two_id,
        )
        assert version_two_preserved.readiness == "ready"

        async with seed.factory() as session:
            generation_count_v1 = await session.scalar(
                select(func.count())
                .select_from(ProjectMcpSecretGenerationRow)
                .where(
                    ProjectMcpSecretGenerationRow.project_id == actor.project_id,
                    ProjectMcpSecretGenerationRow.mcp_server_id == asset_id,
                    ProjectMcpSecretGenerationRow.mcp_server_version_id == version_one_id,
                )
            )
            state_v2 = (
                await session.execute(
                    select(ProjectMcpSecretStateRow).where(
                        ProjectMcpSecretStateRow.project_id == actor.project_id,
                        ProjectMcpSecretStateRow.mcp_server_id == asset_id,
                        ProjectMcpSecretStateRow.mcp_server_version_id == version_two_id,
                    )
                )
            ).scalar_one()
            audits = tuple(
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.project_id == actor.project_id,
                            AuditLogRow.action == "asset.updated",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert generation_count_v1 == 0
        assert state_v2.current_generation_id == version_two_generation_id
        secret_audits = [row.metadata_json for row in audits if ".secret." in str(row.metadata_json.get("operation"))]
        operations = {row["operation"] for row in secret_audits}
        assert {
            "mcp.secret.configure",
            "mcp.secret.replace",
            "mcp.secret.copy",
            "mcp.secret.clear",
        } <= operations
        serialized_audits = json.dumps(secret_audits, sort_keys=True)
        for value in (first_secret, second_secret, "mcp-superseded-value"):
            assert value not in serialized_audits
    finally:
        await seed.engine.dispose()
