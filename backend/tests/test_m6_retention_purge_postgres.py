from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text, update
from support.m3_shared_assets import M3Scenario
from support.m4_private_threads import seed_m4_thread_database

from app.audit.service import AuditService, _bind_worker_audit_process
from app.audit.sinks import TrustedOperationAuditSink
from app.private_work.privacy_center import PrivacyCenterService
from app.private_work.retention import PrivateWorkRetentionService
from app.private_work.retention_jobs import RetentionJobAdmission
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionNotEligible,
    RetentionPurger,
    RetentionPurgeRepository,
)
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.credential_service import CreateCredential
from app.shared_assets.models import SkillArchiveFile
from app.shared_assets.skill_service import CreateSkill, SkillService
from app.worker.retention import RetentionPurgeJobHandler
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.jobs.model import JobRow, WorkerNodeRow
from deerflow.persistence.jobs.sql import JobRepository
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
from deerflow.persistence.shared_assets import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)
from deerflow.persistence.user.model import UserRow

NOW = datetime(2026, 7, 18, 12, tzinfo=UTC)
EXPIRED = NOW - timedelta(days=31)


@pytest.mark.anyio
async def test_project_purge_releases_skill_storage_before_deleting_shared_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.private_work import retention_purge

    project_id = uuid.uuid4()
    candidate = RetentionCandidate.project(
        project_id=project_id,
        deletion_effective_at=NOW,
        idempotency_key=f"skill-storage:{project_id}",
        request_id="retention-skill-storage",
    )
    calls: list[str] = []

    async def release_private(*_args, **_kwargs):
        calls.append("release-private")

    async def release_skills(*_args, **_kwargs):
        calls.append("release-skills")

    async def purge_private(*_args, **_kwargs):
        calls.append("purge-private")

    async def purge_shared(*_args, **_kwargs):
        calls.append("purge-shared")

    async def reconcile_storage(*_args, **_kwargs):
        calls.append("reconcile-storage")

    monkeypatch.setattr(retention_purge, "release_private_storage_quota", release_private)
    monkeypatch.setattr(
        retention_purge,
        "release_project_skill_storage_quota",
        release_skills,
    )
    monkeypatch.setattr(retention_purge, "purge_private_scope", purge_private)
    monkeypatch.setattr(retention_purge, "purge_project_shared_scope", purge_shared)

    quota = AsyncMock()
    quota.reconcile_project_storage.side_effect = reconcile_storage

    await RetentionPurgeRepository().physically_purge(
        object(),  # type: ignore[arg-type]
        candidate,
        quota=quota,
    )

    assert calls == [
        "release-private",
        "release-skills",
        "purge-private",
        "purge-shared",
        "reconcile-storage",
    ]


@pytest.mark.anyio
async def test_project_skill_storage_release_groups_exact_version_bytes() -> None:
    from app.private_work.retention_purge import (
        release_project_skill_storage_quota,
    )

    project_id = uuid.uuid4()
    first_version = uuid.uuid4()
    second_version = uuid.uuid4()

    class Result:
        def all(self):
            return [
                SimpleNamespace(skill_version_id=first_version, size_bytes=3),
                SimpleNamespace(skill_version_id=first_version, size_bytes=5),
                SimpleNamespace(skill_version_id=second_version, size_bytes=7),
            ]

    class Session:
        statement = None

        async def execute(self, statement):
            self.statement = statement
            return Result()

    session = Session()
    quota = AsyncMock()

    await release_project_skill_storage_quota(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        quota=quota,
        request_id="retention-exact-skill-release",
    )

    assert "skill_version_files" in str(session.statement)
    assert "skills.scope" in str(session.statement)
    assert quota.release_skill_version_if_reserved.await_count == 2
    quota.release_skill_version_if_reserved.assert_any_await(
        session,
        project_id=project_id,
        version_id=first_version,
        size=8,
    )
    quota.release_skill_version_if_reserved.assert_any_await(
        session,
        project_id=project_id,
        version_id=second_version,
        size=7,
    )


@pytest.mark.anyio
async def test_private_work_retention_service_exposes_transactional_purge_boundary() -> None:
    candidate = RetentionCandidate.project(
        project_id=uuid.uuid4(),
        deletion_effective_at=NOW,
        idempotency_key="retention-service-boundary",
        request_id="task17-retention-service",
    )
    calls: list[tuple[RetentionCandidate, datetime | None]] = []

    class _Purger:
        async def purge(self, value: RetentionCandidate, *, now: datetime | None = None) -> str:
            calls.append((value, now))
            return "purge-result"

    result = await PrivateWorkRetentionService.purge_expired(
        _Purger(),  # type: ignore[arg-type]
        candidate,
        now=NOW,
    )

    assert result == "purge-result"
    assert calls == [(candidate, NOW)]


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(active_key_id="audit-v1", _keys={"audit-v1": b"a" * 32})


def _audit(factory) -> TrustedOperationAuditSink:
    service = AuditService(factory, _keyring())
    return TrustedOperationAuditSink(
        service,
        process_context=_bind_worker_audit_process(service),
    )


def _quota(factory) -> ProjectQuotaEnforcer:
    return ProjectQuotaEnforcer(
        QuotaService(
            factory,
            QuotaConfig(),
            source_ref_hasher=_keyring(),
        )
    )


async def _seed_deleted_file(seed, *, context, thread_id: str, deleted_at: datetime) -> uuid.UUID:
    file_id = uuid.uuid4()
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=context.resource_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        session.add(
            PrivateFileRow(
                id=file_id,
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                thread_id=thread_id,
                kind="upload",
                logical_path=f"{file_id}.txt",
                media_type="text/plain",
                size=4,
                sha256="0" * 64,
                status="deleted",
                deleted_at=deleted_at,
            )
        )
        await session.flush()
        await session.execute(
            text(
                """INSERT INTO file_chunks (file_id,chunk_index,content,size,sha256)
                   VALUES (:file_id,0,:content,4,:sha256)"""
            ),
            {"file_id": file_id, "content": b"data", "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7"},
        )
    return file_id


def _purger(seed) -> RetentionPurger:
    return RetentionPurger(
        seed.factory,
        audit=_audit(seed.factory),
        quota=_quota(seed.factory),
    )


async def _chunks(payload: bytes):
    yield payload


async def _retention_transaction_snapshot(
    factory,
    *,
    project_id: uuid.UUID,
    file_id: uuid.UUID,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    queries = {
        "file": """SELECT id,status,size,owner_user_id,project_id
            FROM files WHERE id=:file_id""",
        "counter": """SELECT used,reserved,version
            FROM project_usage_counters
            WHERE project_id=:project_id
              AND dimension='storage_bytes'
              AND bucket='lifetime'""",
        "ledger": """SELECT id,project_id,dimension,delta,bucket,source_kind,
                   source_ref_key_id,source_ref_hmac,idempotency_key,request_id
            FROM project_usage_ledger
            WHERE project_id=:project_id AND dimension='storage_bytes'
            ORDER BY id""",
        "audit": """SELECT id,project_id,action,target_kind,target_ref_key_id,
                   target_ref_hmac,outcome,request_id,metadata_json
            FROM audit_logs
            WHERE project_id=:project_id AND action='purge.completed'
            ORDER BY id""",
    }
    parameters = {"project_id": project_id, "file_id": file_id}
    async with factory() as session:
        return {name: tuple(tuple(row) for row in (await session.execute(text(statement), parameters)).all()) for name, statement in queries.items()}


@pytest.mark.parametrize(
    "resource_kind",
    ("former_owner", "account", "project"),
)
@pytest.mark.postgres
@pytest.mark.anyio
async def test_retention_purge_releases_exact_ready_file_quota_for_every_scope_kind(
    migrated_postgres_database_url: str,
    resource_kind: str,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quota = _quota(seed.factory)
    files = PrivateFileService(seed.factory, quota=quota)
    project_a_owner_a_thread = f"quota-a-owner-a-{uuid.uuid4()}"
    project_a_owner_b_thread = f"quota-a-owner-b-{uuid.uuid4()}"
    project_b_owner_a_thread = f"quota-b-owner-a-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=project_a_owner_a_thread,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateThreadRepository(session).create(
                scope=seed.owner_b_scope,
                thread_id=project_a_owner_b_thread,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
            await PrivateThreadRepository(session).create(
                scope=seed.project_b_owner_a_scope,
                thread_id=project_b_owner_a_thread,
                agent=ThreadAgentRef(seed.project_b_agent_id, "project"),
            )
        project_a_owner_a_file = await files.upload(
            seed.owner_a,
            thread_id=project_a_owner_a_thread,
            logical_path="uploads/owner-a.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"aaaa"),
        )
        project_a_owner_b_file = await files.upload(
            seed.owner_b,
            thread_id=project_a_owner_b_thread,
            logical_path="uploads/owner-b.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"bbbbb"),
        )
        project_b_owner_a_file = await files.upload(
            seed.project_b_owner_a,
            thread_id=project_b_owner_a_thread,
            logical_path="uploads/project-b.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"cccccc"),
        )

        async with seed.factory() as session, session.begin():
            owner_a_memberships = (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)).order_by(ProjectMembershipRow.project_id).with_for_update())).scalars().all()
            if resource_kind == "former_owner":
                membership = next(row for row in owner_a_memberships if row.project_id == seed.owner_a.project_id)
                membership.status = "left"
                membership.ended_at = EXPIRED
                membership.retention_until = EXPIRED
                membership.end_reason = "left"
                membership.version += 1
                candidate = RetentionCandidate.former_owner(
                    project_id=membership.project_id,
                    owner_user_id=membership.user_id,
                    membership_id=membership.id,
                    activation_generation=membership.activation_generation,
                    retention_until=EXPIRED,
                    idempotency_key=f"quota-former:{membership.id}",
                    request_id="retention-quota-former",
                )
            elif resource_kind == "account":
                for membership in owner_a_memberships:
                    membership.status = "left"
                    membership.ended_at = EXPIRED
                    membership.retention_until = EXPIRED
                    membership.end_reason = "left"
                    membership.version += 1
                candidate = RetentionCandidate.account(
                    owner_user_id=str(seed.owner_a.user_id),
                    project_ids=tuple(
                        sorted(
                            (row.project_id for row in owner_a_memberships),
                            key=str,
                        )
                    ),
                    retention_until=EXPIRED,
                    idempotency_key=f"quota-account:{seed.owner_a.user_id}",
                    request_id="retention-quota-account",
                )
            else:
                project = await session.get(
                    ProjectRow,
                    seed.owner_a.project_id,
                    with_for_update=True,
                )
                assert project is not None
                project.status = "pending_deletion"
                project.deletion_requested_at = EXPIRED
                project.deletion_effective_at = EXPIRED
                candidate = RetentionCandidate.project(
                    project_id=project.id,
                    deletion_effective_at=EXPIRED,
                    idempotency_key=f"quota-project:{project.id}",
                    request_id="retention-quota-project",
                )

        await RetentionPurger(
            seed.factory,
            audit=_audit(seed.factory),
            quota=quota,
        ).purge(candidate, now=NOW)

        async with seed.factory() as session:
            project_a_usage = (
                await session.execute(
                    text(
                        """SELECT used,reserved FROM project_usage_counters
                    WHERE project_id=:project_id
                      AND dimension='storage_bytes'
                      AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
            project_b_usage = (
                await session.execute(
                    text(
                        """SELECT used,reserved FROM project_usage_counters
                        WHERE project_id=:project_id
                          AND dimension='storage_bytes'
                          AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.project_b_owner_a.project_id},
                )
            ).one()
            remaining_ids = set(
                (
                    await session.execute(
                        select(PrivateFileRow.id).where(
                            PrivateFileRow.id.in_(
                                (
                                    project_a_owner_a_file.id,
                                    project_a_owner_b_file.id,
                                    project_b_owner_a_file.id,
                                )
                            )
                        )
                    )
                ).scalars()
            )
            release_rows = set(
                (
                    await session.execute(
                        text(
                            """SELECT project_id,delta,source_kind,
                                      source_ref_key_id,source_ref_hmac,
                                      idempotency_key
                               FROM project_usage_ledger
                               WHERE dimension='storage_bytes'
                                 AND source_kind='release'
                                 AND project_id IN (:project_a,:project_b)"""
                        ),
                        {
                            "project_a": seed.owner_a.project_id,
                            "project_b": seed.project_b_owner_a.project_id,
                        },
                    )
                ).all()
            )
            storage_net_rows = {
                row.project_id: (row.release_count, row.net_delta)
                for row in (
                    await session.execute(
                        text(
                            """SELECT project_id,
                                      count(*) FILTER (
                                          WHERE source_kind='release'
                                      ) AS release_count,
                                      sum(delta) AS net_delta
                               FROM project_usage_ledger
                               WHERE dimension='storage_bytes'
                                 AND project_id IN (:project_a,:project_b)
                               GROUP BY project_id"""
                        ),
                        {
                            "project_a": seed.owner_a.project_id,
                            "project_b": seed.project_b_owner_a.project_id,
                        },
                    )
                ).all()
            }
        if resource_kind == "former_owner":
            released_files = (
                (
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                    project_a_owner_a_file.id,
                    project_a_owner_a_file.size,
                ),
            )
        elif resource_kind == "account":
            released_files = (
                (
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                    project_a_owner_a_file.id,
                    project_a_owner_a_file.size,
                ),
                (
                    seed.project_b_owner_a.project_id,
                    str(seed.project_b_owner_a.user_id),
                    project_b_owner_a_file.id,
                    project_b_owner_a_file.size,
                ),
            )
        else:
            released_files = (
                (
                    seed.owner_a.project_id,
                    str(seed.owner_a.user_id),
                    project_a_owner_a_file.id,
                    project_a_owner_a_file.size,
                ),
                (
                    seed.owner_b.project_id,
                    str(seed.owner_b.user_id),
                    project_a_owner_b_file.id,
                    project_a_owner_b_file.size,
                ),
            )
        expected_release_rows = set()
        for project_id, owner_user_id, file_id, size in released_files:
            source_ref = quota._quotas._source_ref(
                project_id=project_id,
                owner_user_id=owner_user_id,
                dimension="storage_bytes",
                bucket="lifetime",
                operation="release",
                key=f"file:{file_id}",
            )
            expected_release_rows.add(
                (
                    project_id,
                    -size,
                    "release",
                    source_ref.key_id,
                    source_ref.hmac_hex,
                    quota._quotas._idempotency_digest(source_ref=source_ref),
                )
            )
        assert release_rows == expected_release_rows
        if resource_kind == "former_owner":
            assert (tuple(project_a_usage), tuple(project_b_usage)) == (
                (0, 5),
                (0, 6),
            )
            assert storage_net_rows == {
                seed.owner_a.project_id: (1, 5),
                seed.project_b_owner_a.project_id: (0, 6),
            }
            assert remaining_ids == {
                project_a_owner_b_file.id,
                project_b_owner_a_file.id,
            }
        elif resource_kind == "account":
            assert (tuple(project_a_usage), tuple(project_b_usage)) == (
                (0, 5),
                (0, 0),
            )
            assert storage_net_rows == {
                seed.owner_a.project_id: (1, 5),
                seed.project_b_owner_a.project_id: (1, 0),
            }
            assert remaining_ids == {project_a_owner_b_file.id}
        else:
            assert (tuple(project_a_usage), tuple(project_b_usage)) == (
                (0, 0),
                (0, 6),
            )
            assert storage_net_rows == {
                seed.owner_a.project_id: (2, 0),
                seed.project_b_owner_a.project_id: (0, 6),
            }
            assert remaining_ids == {project_b_owner_a_file.id}
    finally:
        await seed.engine.dispose()


@pytest.mark.parametrize("failure_stage", ("purge", "audit"))
@pytest.mark.postgres
@pytest.mark.anyio
async def test_retention_purge_rolls_back_quota_release_and_retries_once(
    migrated_postgres_database_url: str,
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.private_work.file_service import PrivateFileService

    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quota = _quota(seed.factory)
    files = PrivateFileService(seed.factory, quota=quota)
    thread_id = f"quota-rollback-{uuid.uuid4()}"
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        target = await files.upload(
            seed.owner_a,
            thread_id=thread_id,
            logical_path="uploads/rollback.bin",
            media_type="application/octet-stream",
            chunks=_chunks(b"rollback"),
        )
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            assert membership is not None
            membership.status = "left"
            membership.ended_at = EXPIRED
            membership.retention_until = EXPIRED
            membership.end_reason = "left"
            membership.version += 1
            candidate = RetentionCandidate.former_owner(
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=EXPIRED,
                idempotency_key=f"quota-rollback:{failure_stage}:{membership.id}",
                request_id=f"retention-quota-rollback-{failure_stage}",
            )

        before = await _retention_transaction_snapshot(
            seed.factory,
            project_id=seed.owner_a.project_id,
            file_id=target.id,
        )
        audit = _audit(seed.factory)
        repository: RetentionPurgeRepository = RetentionPurgeRepository()
        original_audit = audit.purge_completed
        if failure_stage == "purge":

            class _FailAfterPurge(RetentionPurgeRepository):
                async def physically_purge(self, session, value, *, quota):
                    await super().physically_purge(session, value, quota=quota)
                    raise RuntimeError("injected failure after purge")

            repository = _FailAfterPurge()
        else:

            async def _fail_after_audit(*args, **kwargs):
                await original_audit(*args, **kwargs)
                raise RuntimeError("injected failure after audit")

            monkeypatch.setattr(audit, "purge_completed", _fail_after_audit)

        with pytest.raises(RuntimeError, match=f"injected failure after {failure_stage}"):
            await RetentionPurger(
                seed.factory,
                audit=audit,
                quota=quota,
                repository=repository,
            ).purge(candidate, now=NOW)

        after_failure = await _retention_transaction_snapshot(
            seed.factory,
            project_id=seed.owner_a.project_id,
            file_id=target.id,
        )
        assert after_failure == before

        monkeypatch.setattr(audit, "purge_completed", original_audit)
        await RetentionPurger(
            seed.factory,
            audit=audit,
            quota=quota,
        ).purge(candidate, now=NOW)

        after_retry = await _retention_transaction_snapshot(
            seed.factory,
            project_id=seed.owner_a.project_id,
            file_id=target.id,
        )
        assert after_retry["file"] == ()
        assert after_retry["counter"][0][:2] == (0, 0)
        assert len(after_retry["ledger"]) == len(before["ledger"]) + 1
        assert sum(row[3] for row in after_retry["ledger"]) == 0
        assert [row[3] for row in after_retry["ledger"] if row[5] == "release"] == [-target.size]
        assert len(after_retry["audit"]) == len(before["audit"]) + 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_former_owner_deadline_job_is_worker_claimed_without_scheduler(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        target = await _seed_deleted_file(
            seed,
            context=seed.owner_a,
            thread_id=f"former-owner-{uuid.uuid4()}",
            deleted_at=EXPIRED,
        )
        deadline = NOW - timedelta(seconds=1)
        worker_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            assert membership is not None
            membership.status = "left"
            membership.ended_at = NOW - timedelta(days=30)
            membership.retention_until = deadline
            membership.end_reason = "left"
            membership.version += 1
            await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=deadline,
            )
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="retention-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                    heartbeat_at=NOW,
                )
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=90,
                now=NOW,
            )
            assert claim is not None
            assert claim.scope.project_id == seed.owner_a.project_id
            assert claim.scope.owner_user_id == str(seed.owner_a.user_id)
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=NOW,
            )

        handler = RetentionPurgeJobHandler(
            seed.factory,
            audit=_audit(seed.factory),
            quota=_quota(seed.factory),
            clock=lambda: NOW,
        )
        settlement = await handler(claim, object())  # type: ignore[arg-type]
        await settlement.commit()

        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, target) is None
            row = await session.get(JobRow, claim.job_id)
            assert row is not None
            assert row.status == "succeeded"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_rejoin_generation_makes_old_retention_job_cancel_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        target = await _seed_deleted_file(
            seed,
            context=seed.owner_a,
            thread_id=f"rejoin-retention-{uuid.uuid4()}",
            deleted_at=EXPIRED,
        )
        deadline = NOW - timedelta(seconds=1)
        worker_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            assert membership is not None
            membership.status = "left"
            membership.ended_at = NOW - timedelta(days=30)
            membership.retention_until = deadline
            membership.end_reason = "left"
            membership.version += 1
            await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=deadline,
            )
            membership.status = "active"
            membership.ended_at = None
            membership.retention_until = None
            membership.end_reason = None
            membership.activation_generation += 1
            membership.version += 1
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="retention-rejoin-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                    heartbeat_at=NOW,
                )
            )

        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=90,
                now=NOW,
            )
            assert claim is not None
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=NOW,
            )

        settlement = await RetentionPurgeJobHandler(
            seed.factory,
            audit=_audit(seed.factory),
            quota=_quota(seed.factory),
            clock=lambda: NOW,
        )(claim, object())  # type: ignore[arg-type]
        await settlement.commit()

        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, target) is not None
            row = await session.get(JobRow, claim.job_id)
            assert row is not None
            assert row.status == "cancelled"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_purge_precedence_cancels_then_restore_resumes_former_owner_case(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        member_deadline = NOW + timedelta(days=30)
        project_deadline = NOW + timedelta(days=10)
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).with_for_update())
            assert membership is not None
            assert project is not None
            membership.status = "left"
            membership.ended_at = NOW
            membership.retention_until = member_deadline
            membership.end_reason = "left"
            membership.version += 1
            owner_job_id = await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=member_deadline,
            )
            early_job_id = await RetentionJobAdmission.admit_early_delete(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=member_deadline,
                now=NOW,
            )
            project.status = "pending_deletion"
            project.deletion_requested_at = NOW
            project.deletion_effective_at = project_deadline
            project_job_id = await RetentionJobAdmission.admit_project(
                session,
                project_id=project.id,
                deletion_effective_at=project_deadline,
                now=NOW,
            )

        async with seed.factory() as session:
            owner_job = await session.get(JobRow, owner_job_id)
            early_job = await session.get(JobRow, early_job_id)
            project_job = await session.get(JobRow, project_job_id)
            assert owner_job is not None
            assert early_job is not None
            assert project_job is not None
            assert owner_job.status == "cancelled"
            assert owner_job.cancel_reason == "project_purge_precedence"
            assert early_job.status == "queued"
            assert early_job.cancel_requested_at is None
            assert project_job.status == "queued"

        restored_at = NOW + timedelta(days=1)
        async with seed.factory() as session, session.begin():
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).with_for_update())
            assert project is not None
            project.status = "active"
            project.deletion_requested_at = None
            project.deletion_effective_at = None
            await RetentionJobAdmission.restore_project(
                session,
                project_id=project.id,
                now=restored_at,
            )

        async with seed.factory() as session:
            owner_job = await session.get(JobRow, owner_job_id)
            early_job = await session.get(JobRow, early_job_id)
            project_job = await session.get(JobRow, project_job_id)
            assert owner_job is not None
            assert early_job is not None
            assert project_job is not None
            assert owner_job.status == "queued"
            assert owner_job.available_at == member_deadline
            assert owner_job.cancel_reason is None
            assert early_job.status == "queued"
            assert project_job.status == "cancelled"
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_later_project_deletion_never_extends_former_owner_deadline(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        target = await _seed_deleted_file(
            seed,
            context=seed.owner_a,
            thread_id=f"former-owner-before-project-{uuid.uuid4()}",
            deleted_at=NOW,
        )
        member_deadline = NOW + timedelta(days=30)
        project_requested_at = NOW + timedelta(days=29)
        project_deadline = project_requested_at + timedelta(days=30)
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).with_for_update())
            assert membership is not None
            assert project is not None
            membership.status = "left"
            membership.ended_at = NOW
            membership.retention_until = member_deadline
            membership.end_reason = "left"
            membership.version += 1
            owner_job_id = await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=member_deadline,
            )
            project.status = "pending_deletion"
            project.deletion_requested_at = project_requested_at
            project.deletion_effective_at = project_deadline
            project_job_id = await RetentionJobAdmission.admit_project(
                session,
                project_id=project.id,
                deletion_effective_at=project_deadline,
                now=project_requested_at,
            )

        async with seed.factory() as session:
            owner_job = await session.get(JobRow, owner_job_id)
            project_job = await session.get(JobRow, project_job_id)
            assert owner_job is not None
            assert project_job is not None
            assert owner_job.status == "queued"
            assert owner_job.cancel_requested_at is None
            assert project_job.status == "queued"
        async with seed.factory() as session:
            cases = await PrivacyCenterService(session).list_cases(
                seed.owner_a.user_id,
                now=project_requested_at,
            )
            assert len(cases) == 1
            assert cases[0].retention_kind == "former_owner"
            assert cases[0].deletion_deadline == member_deadline

        worker_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="former-owner-earlier-deadline-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                    heartbeat_at=member_deadline,
                )
            )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=90,
                now=member_deadline,
            )
            assert claim is not None
            assert claim.job_id == owner_job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=member_deadline,
            )

        settlement = await RetentionPurgeJobHandler(
            seed.factory,
            audit=_audit(seed.factory),
            quota=_quota(seed.factory),
            clock=lambda: member_deadline,
        )(claim, object())  # type: ignore[arg-type]
        await settlement.commit()

        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, target) is None
            owner_job = await session.get(JobRow, owner_job_id)
            project_job = await session.get(JobRow, project_job_id)
            assert owner_job is not None
            assert project_job is not None
            assert owner_job.status == "succeeded"
            assert project_job.status == "queued"
            assert project_job.cancel_requested_at is None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_equal_project_deadline_owns_one_exact_purge_case(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        deadline = NOW + timedelta(days=30)
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).with_for_update())
            assert membership is not None
            assert project is not None
            membership.status = "removed"
            membership.ended_at = NOW
            membership.retention_until = deadline
            membership.end_reason = "removed"
            membership.version += 1
            owner_job_id = await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=deadline,
            )
            project.status = "pending_deletion"
            project.deletion_requested_at = NOW
            project.deletion_effective_at = deadline
            await RetentionJobAdmission.admit_project(
                session,
                project_id=project.id,
                deletion_effective_at=deadline,
                now=NOW,
            )

        async with seed.factory() as session:
            owner_job = await session.get(JobRow, owner_job_id)
            assert owner_job is not None
            assert owner_job.status == "cancelled"
            assert owner_job.cancel_reason == "project_purge_precedence"
        async with seed.factory() as session:
            cases = await PrivacyCenterService(session).list_cases(
                seed.owner_a.user_id,
                now=NOW,
            )
            assert len(cases) == 1
            assert cases[0].retention_kind == "project"
            assert cases[0].deletion_deadline == deadline
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_privacy_center_is_account_scoped_exports_no_credentials_and_admits_early_delete(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        target = await _seed_deleted_file(
            seed,
            context=seed.owner_a,
            thread_id=f"privacy-export-{uuid.uuid4()}",
            deleted_at=EXPIRED,
        )
        deadline = NOW + timedelta(days=30)
        async with seed.factory() as session, session.begin():
            membership = await session.scalar(
                select(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                )
                .with_for_update()
            )
            assert membership is not None
            membership.status = "left"
            membership.ended_at = NOW
            membership.retention_until = deadline
            membership.end_reason = "left"
            membership.version += 1
            await RetentionJobAdmission.admit_former_owner(
                session,
                project_id=membership.project_id,
                owner_user_id=membership.user_id,
                membership_id=membership.id,
                activation_generation=membership.activation_generation,
                retention_until=deadline,
            )

        async with seed.factory() as session:
            service = PrivacyCenterService(session)
            cases = await service.list_cases(seed.owner_a.user_id, now=NOW)
            assert [case.project_id for case in cases] == [seed.owner_a.project_id]
        async with seed.factory() as session:
            assert (
                await PrivacyCenterService(session).list_cases(
                    seed.owner_b.user_id,
                    now=NOW,
                )
                == ()
            )
        async with seed.factory() as session:
            exported = await PrivacyCenterService(session).open_case_export(
                seed.owner_a.user_id,
                seed.owner_a.project_id,
                now=NOW,
            )
            records = [json.loads(line) async for line in exported]
            assert records[0]["record_type"] == "manifest"
            assert records[0]["schema_version"] == 2
            file_record = next(record for record in records if record["record_type"] == "file")
            assert file_record["data"]["id"] == str(target)
            chunk_record = next(record for record in records if record["record_type"] == "file_chunk")
            assert chunk_record["data"]["file_id"] == str(target)
            assert chunk_record["data"]["content_base64"] == "ZGF0YQ=="
            serialized = repr(records).lower()
            assert "encrypted_access_token" not in serialized
            assert "encrypted_refresh_token" not in serialized
            assert "credential_envelope" not in serialized
        async with seed.factory() as session:
            admitted = await PrivacyCenterService(session).request_early_delete(
                seed.owner_a.user_id,
                seed.owner_a.project_id,
                now=NOW,
            )
            assert admitted.status == "queued"
        async with seed.factory() as session:
            early = await session.get(JobRow, admitted.job_id)
            assert early is not None
            assert early.available_at == NOW
            assert early.owner_user_id == str(seed.owner_a.user_id)
        worker_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="privacy-early-delete-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                    heartbeat_at=NOW,
                )
            )
        async with seed.factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=90,
                now=NOW,
            )
            assert claim is not None
            assert claim.job_id == admitted.job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=NOW,
            )
        settlement = await RetentionPurgeJobHandler(
            seed.factory,
            audit=_audit(seed.factory),
            quota=_quota(seed.factory),
            clock=lambda: NOW,
        )(claim, object())  # type: ignore[arg-type]
        await settlement.commit()
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, target) is None
            purge_audits = (
                (
                    await session.execute(
                        select(AuditLogRow).where(
                            AuditLogRow.action == "purge.completed",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [row.metadata_json for row in purge_audits] == [
                {"resource_kind": "former_owner", "purged_count": 1},
            ]
            assert purge_audits[0].actor_process == "worker"
        async with seed.factory() as session:
            assert (
                await PrivacyCenterService(session).list_cases(
                    seed.owner_a.user_id,
                    now=NOW,
                )
                == ()
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_purge_revalidates_pending_deletion_and_preserves_other_project(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        project_file = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"project-a-{uuid.uuid4()}", deleted_at=EXPIRED)
        other_file = await _seed_deleted_file(seed, context=seed.project_b_owner_a, thread_id=f"project-b-{uuid.uuid4()}", deleted_at=EXPIRED)
        purger = _purger(seed)
        candidate = RetentionCandidate.project(
            project_id=seed.owner_a.project_id,
            deletion_effective_at=EXPIRED,
            idempotency_key=f"project:{seed.owner_a.project_id}",
            request_id="task17-project-purge",
        )

        with pytest.raises(RetentionNotEligible):
            await purger.purge(candidate, now=NOW)
        async with seed.factory() as session, session.begin():
            await session.execute(update(ProjectRow).where(ProjectRow.id == seed.owner_a.project_id).values(status="pending_deletion", deletion_requested_at=EXPIRED, deletion_effective_at=EXPIRED))

        await purger.purge(candidate, now=NOW)
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, project_file) is None
            assert await session.get(PrivateFileRow, other_file) is not None
            # Governance/audit shells are retained; online private rows are purged.
            assert await session.get(ProjectRow, seed.owner_a.project_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_purge_removes_shared_asset_bodies_secrets_and_invitations(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    try:
        system = await scenario.bootstrap_system_catalog()
        assert scenario.system_agent_id is not None
        assert scenario.project_agent_id is not None
        assert scenario.project_mcp_id is not None
        assert scenario.project_mcp_version_id is not None
        assert scenario.project_mcp_asset_version is not None
        assert scenario.project_credential_id is not None
        assert scenario.project_credential_version_id is not None
        await scenario.mcp_servers.approve(
            scenario.project_admin,
            scenario.project_mcp_id,
            scenario.project_mcp_version_id,
            {"primary": scenario.project_credential_version_id},
            expected_asset_version=scenario.project_mcp_asset_version,
        )

        quota = _quota(scenario.session_factory)
        skills = SkillService(scenario.session_factory, quota=quota)
        project_skill = await skills.create_asset(
            scenario.project_admin,
            CreateSkill("retention-project-skill", "Retention Project Skill"),
        )
        project_skill_version = await skills.create_version_from_archive(
            scenario.project_admin,
            project_skill.id,
            (
                SkillArchiveFile(
                    "SKILL.md",
                    b"---\nname: retention-project-skill\ndescription: purge sentinel body\n---\n\nprivate project instructions\n",
                    "text/markdown",
                ),
            ),
            expected_asset_version=project_skill.version,
        )
        await skills.publish(
            scenario.project_admin,
            project_skill.id,
            project_skill_version.id,
            expected_asset_version=project_skill.version + 1,
        )
        project_skill_storage_bytes = sum(
            len(file.content)
            for file in (
                SkillArchiveFile(
                    "SKILL.md",
                    b"---\nname: retention-project-skill\ndescription: purge sentinel body\n---\n\nprivate project instructions\n",
                    "text/markdown",
                ),
            )
        )
        legacy_skill_id = uuid.uuid4()
        legacy_version_id = uuid.uuid4()
        legacy_content = b"legacy retention skill without a version reservation"
        legacy_storage_bytes = len(legacy_content)
        async with scenario.session_factory() as session, session.begin():
            session.add(
                SkillRow(
                    id=legacy_skill_id,
                    scope="project",
                    project_id=scenario.project_admin.project_id,
                    slug="retention-legacy-project-skill",
                    display_name="Retention Legacy Project Skill",
                    created_by_user_id=str(scenario.project_admin.user_id),
                )
            )
            session.add(
                SkillVersionRow(
                    id=legacy_version_id,
                    skill_id=legacy_skill_id,
                    version_number=1,
                    workflow_status="draft",
                    description="legacy retention row",
                    frontmatter={},
                    secret_requirements=[],
                    scan_decision="allow",
                    scan_summary={},
                    payload_checksum="0" * 64,
                    created_by_user_id=str(scenario.project_admin.user_id),
                )
            )
            await session.flush()
            session.add(
                SkillVersionFileRow(
                    skill_version_id=legacy_version_id,
                    path="SKILL.md",
                    media_type="text/markdown",
                    size_bytes=legacy_storage_bytes,
                    sha256=hashlib.sha256(legacy_content).hexdigest(),
                    content=legacy_content,
                )
            )
            await session.execute(
                text(
                    """UPDATE project_usage_counters
                          SET reserved=reserved+:legacy_bytes,
                              version=version+1
                        WHERE project_id=:project_id
                          AND dimension='storage_bytes'
                          AND bucket='lifetime'"""
                ),
                {
                    "project_id": scenario.project_admin.project_id,
                    "legacy_bytes": legacy_storage_bytes,
                },
            )
        async with scenario.session_factory() as session:
            reserved_before_purge = await session.scalar(
                text(
                    """SELECT reserved FROM project_usage_counters
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND bucket='lifetime'"""
                ),
                {"project_id": scenario.project_admin.project_id},
            )
        assert reserved_before_purge == project_skill_storage_bytes + legacy_storage_bytes

        other_credential = await scenario.credentials.create(
            scenario.other_project_admin,
            CreateCredential(
                "retention-other-token",
                "Retention Other Token",
                "token",
            ),
            {"env": {"OTHER_TOKEN": "other-project-secret"}},
        )
        system_credential = await scenario.credentials.create(
            scenario.system_admin,
            CreateCredential(
                "retention-system-token",
                "Retention System Token",
                "token",
            ),
            {"env": {"SYSTEM_TOKEN": "system-secret"}},
        )

        invitation_email = f"purge-{uuid.uuid4()}@example.com"
        invitation_token_hash = uuid.uuid4().hex * 2
        async with scenario.session_factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO project_system_agent_bindings
                       (project_id, system_agent_id, system_asset_scope,
                        agent_version_id, enabled, version,
                        created_by_user_id, updated_by_user_id)
                       VALUES (:project_id, :agent_id, 'system', :version_id,
                               true, 1, :user_id, :user_id)"""
                ),
                {
                    "project_id": scenario.project_admin.project_id,
                    "agent_id": scenario.system_agent_id,
                    "version_id": system.agent_v1,
                    "user_id": str(scenario.project_admin.user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO project_invitations
                       (id, project_id, invited_email, role, token_hash, status,
                        expires_at, version, created_by_user_id)
                       VALUES (:id, :project_id, :email, 'viewer', :token_hash,
                               'pending', :expires_at, 1, :created_by)"""
                ),
                {
                    "id": uuid.uuid4(),
                    "project_id": scenario.project_admin.project_id,
                    "email": invitation_email,
                    "token_hash": invitation_token_hash,
                    "expires_at": NOW + timedelta(days=7),
                    "created_by": str(scenario.project_admin.user_id),
                },
            )
            project = await session.scalar(select(ProjectRow).where(ProjectRow.id == scenario.project_admin.project_id).with_for_update())
            assert project is not None
            project.display_name = "Sensitive Project Name"
            project.description = "Sensitive project description"
            project.icon = "secret-icon"
            project.status = "pending_deletion"
            project.deletion_requested_at = EXPIRED
            project.deletion_effective_at = EXPIRED
            project_job_id = await RetentionJobAdmission.admit_project(
                session,
                project_id=project.id,
                deletion_effective_at=EXPIRED,
                now=EXPIRED,
            )
            worker_id = uuid.uuid4()
            session.add(
                WorkerNodeRow(
                    id=worker_id,
                    version="project-shared-purge-test",
                    capabilities_json=["retention_purge"],
                    max_concurrent_jobs=1,
                    heartbeat_at=NOW,
                )
            )

        async with scenario.session_factory() as session, session.begin():
            jobs = JobRepository(session)
            claim = await jobs.claim_next(
                worker_id=worker_id,
                capabilities=frozenset({"retention_purge"}),
                lease_seconds=90,
                now=NOW,
            )
            assert claim is not None
            assert claim.job_id == project_job_id
            assert await jobs.mark_running(
                claim.job_id,
                lease_token=claim.lease_token,
                now=NOW,
            )

        settlement = await RetentionPurgeJobHandler(
            scenario.session_factory,
            audit=_audit(scenario.session_factory),
            quota=quota,
            clock=lambda: NOW,
        )(claim, object())  # type: ignore[arg-type]
        await settlement.commit()

        async with scenario.session_factory() as session:
            parameters = {"project_id": scenario.project_admin.project_id}
            for table_name in (
                "credentials",
                "agents",
                "skills",
                "mcp_servers",
                "project_invitations",
                "project_system_agent_bindings",
                "project_system_skill_bindings",
                "project_system_mcp_bindings",
            ):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table_name} WHERE project_id=:project_id"),
                        parameters,
                    )
                    == 0
                ), table_name
            for query in (
                """SELECT count(*) FROM credential_versions version
                   JOIN credentials asset ON asset.id=version.credential_id
                   WHERE asset.project_id=:project_id""",
                """SELECT count(*) FROM credential_envelopes envelope
                   JOIN credential_versions version ON version.id=envelope.credential_version_id
                   JOIN credentials asset ON asset.id=version.credential_id
                   WHERE asset.project_id=:project_id""",
                """SELECT count(*) FROM credential_grants grant_row
                   JOIN credential_versions version ON version.id=grant_row.credential_version_id
                   JOIN credentials asset ON asset.id=version.credential_id
                   WHERE asset.project_id=:project_id""",
                """SELECT count(*) FROM skill_version_files file
                   JOIN skill_versions version ON version.id=file.skill_version_id
                   JOIN skills asset ON asset.id=version.skill_id
                   WHERE asset.project_id=:project_id""",
            ):
                assert await session.scalar(text(query), parameters) == 0

            assert (
                await session.get(
                    ProjectRow,
                    scenario.other_project_admin.project_id,
                )
                is not None
            )
            project = await session.get(ProjectRow, scenario.project_admin.project_id)
            assert project is not None
            assert project.display_name == "Deleted project"
            assert project.description == ""
            assert project.icon == "folder"
            assert await session.get(JobRow, project_job_id) is not None
            assert (
                await session.scalar(
                    select(AuditLogRow.id).where(
                        AuditLogRow.project_id == scenario.project_admin.project_id,
                        AuditLogRow.action == "purge.completed",
                    )
                )
                is not None
            )
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*) FROM credential_envelopes envelope
                       JOIN credential_versions version ON version.id=envelope.credential_version_id
                       WHERE version.credential_id IN (:other_id, :system_id)"""
                    ),
                    {
                        "other_id": other_credential.id,
                        "system_id": system_credential.id,
                    },
                )
                == 2
            )
            assert (
                await session.scalar(
                    text(
                        """SELECT reserved FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'
                             AND bucket='lifetime'"""
                    ),
                    parameters,
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*) FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND dimension='storage_bytes'
                             AND source_kind='reconcile_adjustment'"""
                    ),
                    parameters,
                )
                == 1
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM agents WHERE id=:id AND scope='system'"),
                    {"id": scenario.system_agent_id},
                )
                == 1
            )
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_purge_requires_every_membership_expired(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        project_a_file = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"account-a-{uuid.uuid4()}", deleted_at=EXPIRED)
        project_b_file = await _seed_deleted_file(seed, context=seed.project_b_owner_a, thread_id=f"account-b-{uuid.uuid4()}", deleted_at=EXPIRED)
        other_owner_file = await _seed_deleted_file(seed, context=seed.owner_b, thread_id=f"account-other-{uuid.uuid4()}", deleted_at=EXPIRED)
        project_ids = tuple(sorted((seed.owner_a.project_id, seed.project_b_owner_a.project_id), key=str))
        candidate = RetentionCandidate.account(
            owner_user_id=str(seed.owner_a.user_id),
            project_ids=project_ids,
            retention_until=EXPIRED,
            idempotency_key=f"account:{seed.owner_a.user_id}",
            request_id="task17-account-purge",
        )
        purger = _purger(seed)

        with pytest.raises(RetentionNotEligible):
            await purger.purge(candidate, now=NOW)

        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)).values(status="left", ended_at=EXPIRED, retention_until=EXPIRED, end_reason="left", version=ProjectMembershipRow.version + 1)
            )

        await purger.purge(candidate, now=NOW)
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, project_a_file) is None
            assert await session.get(PrivateFileRow, project_b_file) is None
            assert await session.get(PrivateFileRow, other_owner_file) is not None
            assert await session.get(UserRow, str(seed.owner_a.user_id)) is not None
            memberships = (await session.execute(select(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)))).scalars().all()
            assert len(memberships) == 2
            audit = (await session.execute(select(AuditLogRow).where(AuditLogRow.action == "purge.completed"))).scalar_one()
            assert audit.project_id is None
            assert audit.metadata_json == {"resource_kind": "account", "purged_count": 2}
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_rejoin_race_fails_closed_after_candidate_creation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        file_id = await _seed_deleted_file(seed, context=seed.owner_a, thread_id=f"race-{uuid.uuid4()}", deleted_at=EXPIRED)
        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow).where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id)).values(status="left", ended_at=EXPIRED, retention_until=EXPIRED, end_reason="left", version=ProjectMembershipRow.version + 1)
            )
        project_ids = tuple(sorted((seed.owner_a.project_id, seed.project_b_owner_a.project_id), key=str))
        candidate = RetentionCandidate.account(
            owner_user_id=str(seed.owner_a.user_id),
            project_ids=project_ids,
            retention_until=EXPIRED,
            idempotency_key=f"account-race:{seed.owner_a.user_id}",
            request_id="task17-account-race",
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow)
                .where(
                    ProjectMembershipRow.user_id == str(seed.owner_a.user_id),
                    ProjectMembershipRow.project_id == seed.owner_a.project_id,
                )
                .values(status="active", ended_at=None, retention_until=None, end_reason=None, version=ProjectMembershipRow.version + 1)
            )

        with pytest.raises(RetentionNotEligible):
            await _purger(seed).purge(candidate, now=NOW)
        async with seed.factory() as session:
            assert await session.get(PrivateFileRow, file_id) is not None
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_account_membership_insert_waits_for_owner_lock_and_exact_set_revalidation_rejects(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingRepository(RetentionPurgeRepository):
        async def verify_still_eligible(self, session, candidate, *, now):
            scopes = await super().verify_still_eligible(
                session,
                candidate,
                now=now,
            )
            entered.set()
            await release.wait()
            return scopes

    try:
        new_project_id = uuid.uuid4()
        async with seed.factory() as session, session.begin():
            await session.execute(
                update(ProjectMembershipRow)
                .where(ProjectMembershipRow.user_id == str(seed.owner_a.user_id))
                .values(
                    status="left",
                    ended_at=EXPIRED,
                    retention_until=EXPIRED,
                    end_reason="left",
                    version=ProjectMembershipRow.version + 1,
                )
            )
            session.add(
                ProjectRow(
                    id=new_project_id,
                    slug=f"account-race-{uuid.uuid4().hex[:12]}",
                    display_name="Account race",
                    created_by_user_id=str(seed.owner_b.user_id),
                )
            )
        project_ids = tuple(
            sorted(
                (seed.owner_a.project_id, seed.project_b_owner_a.project_id),
                key=str,
            )
        )
        first = RetentionCandidate.account(
            owner_user_id=str(seed.owner_a.user_id),
            project_ids=project_ids,
            retention_until=EXPIRED,
            idempotency_key=f"account-lock:{seed.owner_a.user_id}",
            request_id="task17-account-lock",
        )
        purger = RetentionPurger(
            seed.factory,
            audit=_audit(seed.factory),
            quota=_quota(seed.factory),
            repository=BlockingRepository(),
        )
        purge_task = asyncio.create_task(purger.purge(first, now=NOW))
        await asyncio.wait_for(entered.wait(), timeout=5)

        async def insert_membership() -> None:
            async with seed.factory() as session, session.begin():
                session.add(
                    ProjectMembershipRow(
                        project_id=new_project_id,
                        user_id=str(seed.owner_a.user_id),
                        role="viewer",
                        status="active",
                    )
                )
                await session.flush()

        insert_task = asyncio.create_task(insert_membership())
        await asyncio.sleep(0.1)
        assert not insert_task.done()
        release.set()
        await purge_task
        await asyncio.wait_for(insert_task, timeout=5)

        retry_with_stale_exact_set = RetentionCandidate.account(
            owner_user_id=str(seed.owner_a.user_id),
            project_ids=project_ids,
            retention_until=EXPIRED,
            idempotency_key=f"account-revalidate:{seed.owner_a.user_id}",
            request_id="task17-account-revalidate",
        )
        with pytest.raises(RetentionNotEligible):
            await purger.purge(
                retry_with_stale_exact_set,
                now=NOW,
            )
    finally:
        release.set()
        await seed.engine.dispose()
