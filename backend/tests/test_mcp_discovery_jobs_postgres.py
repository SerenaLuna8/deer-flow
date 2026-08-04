from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from support.m3_shared_assets import M3Scenario

from app.shared_assets.mcp_discovery_repository import (
    McpToolDiscoveryAttemptRepository,
)
from app.shared_assets.mcp_service import CreateMcpServer, McpDefinition
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.shared_assets import McpToolDiscoveryAttemptRow


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_mcp_discovery_attempt_is_atomic_scoped_and_tracks_job_state(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    worker_id = uuid.uuid4()
    try:
        asset = await scenario.mcp_servers.create_asset(
            scenario.project_admin,
            CreateMcpServer("discovery-job", "Discovery Job"),
        )
        draft = await scenario.mcp_servers.create_version(
            scenario.project_admin,
            asset.id,
            McpDefinition(
                description="Durable discovery test",
                transport="http",
                url="https://m3-scenario.example.test/mcp",
            ),
            expected_asset_version=asset.version,
        )
        await scenario.mcp_servers.publish(
            scenario.project_admin,
            asset.id,
            draft.id,
            expected_asset_version=asset.version + 1,
        )

        async with scenario.session_factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="mcp-discovery-test",
                    capabilities_json=["mcp_discovery"],
                    max_concurrent_jobs=1,
                )
            )
            repository = McpToolDiscoveryAttemptRepository(session)
            queued = await repository.enqueue(
                project_id=scenario.project_admin.project_id,
                requested_by_user_id=str(scenario.project_admin.user_id),
                mcp_server_id=asset.id,
                mcp_server_version_id=draft.id,
                payload_checksum=draft.payload_checksum,
                grant_digest="b" * 64,
                trigger="auto",
                idempotency_key="a" * 64,
            )
            replay = await repository.enqueue(
                project_id=scenario.project_admin.project_id,
                requested_by_user_id=str(scenario.project_admin.user_id),
                mcp_server_id=asset.id,
                mcp_server_version_id=draft.id,
                payload_checksum=draft.payload_checksum,
                grant_digest="b" * 64,
                trigger="auto",
                idempotency_key="a" * 64,
            )
            assert replay == queued
            assert queued.status == "queued"
            assert queued.revision == 1

            active = await repository.active_for_closure(
                scenario.project_admin.project_id,
                asset.id,
                draft.id,
                draft.payload_checksum,
                "b" * 64,
            )
            assert active == queued
            assert (
                await repository.get(
                    scenario.other_project_admin.project_id,
                    queued.attempt_id,
                )
                is None
            )

        async with scenario.session_factory() as session, session.begin():
            job = await session.get(JobRow, queued.attempt_id)
            assert job is not None
            assert job.job_type == "mcp_discovery"
            assert job.owner_user_id == str(scenario.project_admin.user_id)
            assert job.run_id is None
            assert job.automation_occurrence_id is None
            assert job.origin_trace_id is None
            assert job.max_attempts == 1
            assert job.retry_safety == "unsafe"

            claim = await JobRepository(session).claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"mcp_discovery"}),
                lease_seconds=30,
            )
            assert claim is not None
            assert claim.job_id == queued.attempt_id
            assert claim.job_type == "mcp_discovery"
            assert await JobRepository(session).mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
            )

        async with scenario.session_factory() as session, session.begin():
            repository = McpToolDiscoveryAttemptRepository(session)
            running = await repository.get(
                scenario.project_admin.project_id,
                queued.attempt_id,
            )
            assert running is not None
            assert running.status == "running"
            assert running.started_at is not None
            marked = await repository.mark_result(
                queued.attempt_id,
                "succeeded",
                None,
            )
            assert marked.status == "running"
            assert marked.revision == 2
            assert await JobRepository(session).settle_success(
                claim.job_id,
                lease_token=claim.lease_token,
            )

        async with scenario.session_factory() as session:
            repository = McpToolDiscoveryAttemptRepository(session)
            succeeded = await repository.get(
                scenario.project_admin.project_id,
                queued.attempt_id,
            )
            assert succeeded is not None
            assert succeeded.status == "succeeded"
            assert succeeded.completed_at is not None
            assert succeeded.public_error_code is None
            assert (
                await repository.active_for_closure(
                    scenario.project_admin.project_id,
                    asset.id,
                    draft.id,
                    draft.payload_checksum,
                    "b" * 64,
                )
                is None
            )
            latest = await repository.latest_for_version(
                scenario.project_admin.project_id,
                asset.id,
                draft.id,
            )
            assert latest == succeeded
            stored = await session.scalar(select(McpToolDiscoveryAttemptRow).where(McpToolDiscoveryAttemptRow.job_id == queued.attempt_id))
            assert stored is not None
            assert stored.result_status == "succeeded"
    finally:
        await scenario.close()
