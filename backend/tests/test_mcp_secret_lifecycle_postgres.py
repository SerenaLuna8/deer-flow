from __future__ import annotations

import base64
import json
import uuid

import pytest
from sqlalchemy import func, select, text
from support.private_thread_seed import seed_private_thread_database

from app.audit.service import AuditService
from app.private_work.asset_runtime import PrivateAgentRuntime
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
from app.shared_assets.models import (
    AssetKind,
    AssetSelection,
    ResolvedMcpSnapshot,
    WorkflowStatus,
)
from app.shared_assets.resolver import ProjectAssetResolver
from app.worker.mcp_discovery import McpToolDiscoveryJobHandler
from app.worker.service import JobLeaseAuthority, JobSettlement
from deerflow.mcp_definition_policy import ExactMcpEndpointPolicy
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.model import WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository
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


async def _claim_discovery(seed, attempt_id: uuid.UUID):
    async with seed.factory() as session, session.begin():
        worker_id = uuid.uuid4()
        session.add(
            WorkerNodeRow(
                id=worker_id,
                version="mcp-discovery-test",
                capabilities_json=["mcp_discovery"],
                max_concurrent_jobs=1,
            )
        )
        await session.flush()
        jobs = JobRepository(session)
        claim = await jobs.claim_next(
            worker_id=worker_id,
            capabilities=frozenset({"mcp_discovery"}),
            lease_seconds=60,
        )
        assert claim is not None
        assert claim.job_id == attempt_id
        assert await jobs.mark_running(
            claim.job_id,
            lease_token=claim.lease_token,
        )
        return claim


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
        resolver = ProjectAssetResolver(seed.factory)
        superseded_snapshot = await resolver.resolve_project_asset_snapshot(
            actor,
            AssetSelection(AssetKind.MCP, asset_id, version_one_id),
        )
        assert isinstance(superseded_snapshot, ResolvedMcpSnapshot)
        assert superseded_snapshot.version_id == version_one_id
        superseded_material = await resolver.materialize_mcp_secrets(
            actor,
            superseded_snapshot,
        )
        assert superseded_material.by_slot["authorization"] == {"headers": {"Authorization": "mcp-superseded-value"}}
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


@pytest.mark.asyncio
async def test_superseded_project_mcp_version_executes_discovery_with_exact_secret(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"d" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    actor = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="superseded-mcp-discovery",
    )
    endpoints = (
        "http://127.0.0.1:6555/mcp",
        "http://127.0.0.1:6556/mcp",
    )
    endpoint_policy = ExactMcpEndpointPolicy(frozenset(endpoints))
    audit = DurableSharedAssetGovernanceEventSink(
        AuditService(
            seed.factory,
            AuditHmacKeyring("test", {"test": b"a" * 32}),
        )
    )
    mcp_service = McpService(
        seed.factory,
        audit,
        endpoint_policy=endpoint_policy,
    )
    secrets = McpSecretService(seed.factory, audit)
    expected_secret = "superseded-discovery-secret"

    async def fake_discover_exact_mcp(
        version_id,
        definition,
        material,
        authorization_boundary,
        **_kwargs,
    ):
        assert version_id == version_one_id
        assert definition["url"] == endpoints[0]
        assert material["authorization"] == {"headers": {"Authorization": expected_secret}}
        await authorization_boundary.before_mcp_call()
        return ()

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_discover_exact_mcp",
        staticmethod(fake_discover_exact_mcp),
    )
    try:
        created = await mcp_service.create_project_configured(
            actor,
            CreateMcpServer(
                slug=f"superseded-discovery-{uuid.uuid4().hex[:12]}",
                display_name="Superseded discovery",
            ),
            _definition(endpoints[0]),
        )
        asset_id = created.asset.id
        version_one_id = created.version.id
        await secrets.replace(
            actor,
            asset_id,
            version_one_id,
            "authorization",
            {"headers": {"Authorization": expected_secret}},
        )
        superseded = await mcp_service.request_tool_discovery(
            actor,
            asset_id,
            version_one_id,
        )
        current = await mcp_service.update_project_configured(
            actor,
            asset_id,
            _definition(endpoints[1]),
            expected_asset_version=created.asset.version,
        )
        assert current.version.id != version_one_id
        assert current.asset.current_published_version_id == current.version.id
        assert current.version.supersedes_version_id == version_one_id

        async with seed.factory() as session:
            admitted_attempt = await McpToolDiscoveryAttemptRepository(session).get(actor.project_id, superseded.id)
            version_one = await session.get(McpServerVersionRow, version_one_id)
        assert admitted_attempt is not None
        assert version_one is not None
        assert version_one.workflow_status == "published"
        assert admitted_attempt.mcp_server_version_id == version_one_id
        assert admitted_attempt.payload_checksum == created.version.payload_checksum

        claim = await _claim_discovery(seed, superseded.id)

        handler = McpToolDiscoveryJobHandler(
            seed.factory,
            endpoint_policy=endpoint_policy,
            http_client_factory=lambda *_args, **_kwargs: None,  # type: ignore[arg-type,return-value]
            discovery_timeout_seconds=5,
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=60),
        )
        assert isinstance(settlement, JobSettlement)
        assert settlement.outcome.status == "succeeded"
        await settlement.commit()

        async with seed.factory() as session:
            attempt = await McpToolDiscoveryAttemptRepository(session).get(
                actor.project_id,
                superseded.id,
            )
            inventory = await McpToolInventoryRepository(session).get(
                project_id=actor.project_id,
                mcp_server_id=asset_id,
                mcp_server_version_id=version_one_id,
            )
            current_inventory = await McpToolInventoryRepository(session).get(
                project_id=actor.project_id,
                mcp_server_id=asset_id,
                mcp_server_version_id=current.version.id,
            )
        assert attempt is not None
        assert attempt.status == "succeeded"
        assert inventory is not None
        assert inventory.attempt_status == "ready"
        assert inventory.attempt_payload_checksum == created.version.payload_checksum
        assert inventory.attempt_secret_digest == attempt.secret_digest
        assert inventory.tools_payload_checksum == created.version.payload_checksum
        assert inventory.tools_secret_digest == attempt.secret_digest
        assert current_inventory is None
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_late_discovery_result_for_replaced_generation_is_cancelled(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"r" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    actor = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="mcp-late-discovery-result",
    )
    endpoint = "http://127.0.0.1:6558/mcp"
    endpoint_policy = ExactMcpEndpointPolicy(frozenset({endpoint}))
    audit = DurableSharedAssetGovernanceEventSink(
        AuditService(
            seed.factory,
            AuditHmacKeyring("test", {"test": b"a" * 32}),
        )
    )
    service = McpService(
        seed.factory,
        audit,
        endpoint_policy=endpoint_policy,
    )
    secrets = McpSecretService(seed.factory, audit)

    async def fake_discover_exact_mcp(
        _version_id,
        _definition_value,
        material,
        authorization_boundary,
        **_kwargs,
    ):
        assert material["authorization"] == {"headers": {"Authorization": "old-generation-secret"}}
        await authorization_boundary.before_mcp_call()
        return ()

    monkeypatch.setattr(
        PrivateAgentRuntime,
        "_discover_exact_mcp",
        staticmethod(fake_discover_exact_mcp),
    )
    try:
        created = await service.create_project_configured(
            actor,
            CreateMcpServer(
                slug=f"late-discovery-{uuid.uuid4().hex[:12]}",
                display_name="Late discovery fence",
            ),
            _definition(endpoint),
        )
        await secrets.replace(
            actor,
            created.asset.id,
            created.version.id,
            "authorization",
            {"headers": {"Authorization": "old-generation-secret"}},
        )
        async with seed.factory() as session:
            old_attempt = await McpToolDiscoveryAttemptRepository(session).latest_for_version(
                actor.project_id,
                created.asset.id,
                created.version.id,
            )
        assert old_attempt is not None
        claim = await _claim_discovery(seed, old_attempt.attempt_id)
        handler = McpToolDiscoveryJobHandler(
            seed.factory,
            endpoint_policy=endpoint_policy,
            http_client_factory=lambda *_args, **_kwargs: None,  # type: ignore[arg-type,return-value]
            discovery_timeout_seconds=5,
        )
        settlement = await handler(
            claim,
            JobLeaseAuthority(seed.factory, claim, lease_seconds=60),
        )
        assert isinstance(settlement, JobSettlement)

        await secrets.replace(
            actor,
            created.asset.id,
            created.version.id,
            "authorization",
            {"headers": {"Authorization": "new-generation-secret"}},
        )
        async with seed.factory() as session:
            new_attempt = await McpToolDiscoveryAttemptRepository(session).latest_for_version(
                actor.project_id,
                created.asset.id,
                created.version.id,
            )
        assert new_attempt is not None
        assert new_attempt.attempt_id != old_attempt.attempt_id
        assert new_attempt.secret_digest != old_attempt.secret_digest

        await settlement.commit()

        async with seed.factory() as session:
            settled_old = await McpToolDiscoveryAttemptRepository(session).get(
                actor.project_id,
                old_attempt.attempt_id,
            )
            queued_new = await McpToolDiscoveryAttemptRepository(session).get(
                actor.project_id,
                new_attempt.attempt_id,
            )
            inventory = await McpToolInventoryRepository(session).get(
                project_id=actor.project_id,
                mcp_server_id=created.asset.id,
                mcp_server_version_id=created.version.id,
            )
            asset = await session.get(McpServerRow, created.asset.id)
            version = await session.get(McpServerVersionRow, created.version.id)
            slots = tuple((await session.execute(select(McpSecretSlotRow).where(McpSecretSlotRow.mcp_server_version_id == created.version.id))).scalars().all())
            assert asset is not None
            assert version is not None
            current_closure = await lock_mcp_secret_closure(
                session,
                project_id=actor.project_id,
                mcp_server_id=created.asset.id,
                mcp_server_version_id=created.version.id,
                slots=slots,
                request_id=actor.request_id,
            )
            with pytest.raises(RunSnapshotAssetStale):
                await RunSnapshotRepository._validate_mcp_discovery_readiness(
                    session,
                    [(asset, version)],
                    {created.version.id: current_closure},
                    project_id=actor.project_id,
                )
        view = await service.get_tool_inventory(
            actor,
            created.asset.id,
            created.version.id,
        )
        assert settled_old is not None
        assert settled_old.status == "cancelled"
        assert queued_new is not None
        assert queued_new.status == "queued"
        assert inventory is None
        assert view.status == "testing"
    finally:
        await seed.engine.dispose()


@pytest.mark.asyncio
async def test_editor_without_binding_authority_saves_compatible_mcp_unready(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "ACT_WEAVE_SECRET_KEY",
        base64.b64encode(b"e" * 32).decode("ascii"),
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    admin = ProjectContext(
        user_id=seed.owner_a.user_id,
        project_id=seed.owner_a.project_id,
        membership_id=seed.owner_a.membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=seed.owner_a.membership_version,
        request_id="mcp-compatible-copy-admin",
    )
    endpoints = (
        "http://127.0.0.1:6557/mcp",
        "http://127.0.0.1:6557/mcp-v2",
    )
    audit = DurableSharedAssetGovernanceEventSink(
        AuditService(
            seed.factory,
            AuditHmacKeyring("test", {"test": b"a" * 32}),
        )
    )
    service = McpService(
        seed.factory,
        audit,
        endpoint_policy=ExactMcpEndpointPolicy(frozenset(endpoints)),
    )
    secrets = McpSecretService(seed.factory, audit)
    try:
        created = await service.create_project_configured(
            admin,
            CreateMcpServer(
                slug=f"editor-no-copy-{uuid.uuid4().hex[:12]}",
                display_name="Editor compatible update",
            ),
            _definition(endpoints[0]),
        )
        await secrets.replace(
            admin,
            created.asset.id,
            created.version.id,
            "authorization",
            {"headers": {"Authorization": "editor-no-copy-source"}},
        )
        async with seed.factory() as session, session.begin():
            source_state = (
                await session.execute(
                    select(ProjectMcpSecretStateRow).where(
                        ProjectMcpSecretStateRow.project_id == admin.project_id,
                        ProjectMcpSecretStateRow.mcp_server_id == created.asset.id,
                        ProjectMcpSecretStateRow.mcp_server_version_id == created.version.id,
                    )
                )
            ).scalar_one()
            source_generation_id = source_state.current_generation_id
            await session.execute(
                text(
                    """UPDATE project_memberships
                    SET role='editor', version=version + 1
                    WHERE id=:membership_id"""
                ),
                {"membership_id": seed.owner_b.membership_id},
            )

        editor = ProjectContext(
            user_id=seed.owner_b.user_id,
            project_id=seed.owner_b.project_id,
            membership_id=seed.owner_b.membership_id,
            role=ProjectRole.EDITOR,
            capabilities=capabilities_for(ProjectRole.EDITOR),
            membership_version=seed.owner_b.membership_version + 1,
            request_id="mcp-compatible-copy-editor",
        )
        updated = await service.update_project_configured(
            editor,
            created.asset.id,
            _definition(endpoints[1]),
            expected_asset_version=created.asset.version,
        )
        assert updated.version.workflow_status is WorkflowStatus.PUBLISHED
        assert updated.asset.current_published_version_id == updated.version.id

        status = await secrets.get(admin, created.asset.id, updated.version.id)
        assert status.readiness == "unready"
        assert status.slots[0].configured is False
        async with seed.factory() as session:
            state_count = await session.scalar(
                select(func.count())
                .select_from(ProjectMcpSecretStateRow)
                .where(
                    ProjectMcpSecretStateRow.project_id == admin.project_id,
                    ProjectMcpSecretStateRow.mcp_server_id == created.asset.id,
                    ProjectMcpSecretStateRow.mcp_server_version_id == updated.version.id,
                )
            )
            generation_count = await session.scalar(
                select(func.count())
                .select_from(ProjectMcpSecretGenerationRow)
                .where(
                    ProjectMcpSecretGenerationRow.project_id == admin.project_id,
                    ProjectMcpSecretGenerationRow.mcp_server_id == created.asset.id,
                    ProjectMcpSecretGenerationRow.mcp_server_version_id == updated.version.id,
                )
            )
            source_generation = await session.get(
                ProjectMcpSecretGenerationRow,
                source_generation_id,
            )
            discovery = await McpToolDiscoveryAttemptRepository(session).latest_for_version(
                admin.project_id,
                created.asset.id,
                updated.version.id,
            )
        assert state_count == 0
        assert generation_count == 0
        assert source_generation is not None
        assert discovery is None
    finally:
        await seed.engine.dispose()
