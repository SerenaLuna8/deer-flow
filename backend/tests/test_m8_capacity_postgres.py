from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.errors import PrivateWorkMcpQuotaExceeded, PrivateWorkTooLarge
from app.private_work.file_service import PrivateFileLimits, PrivateFileService
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.projects.context import ProjectContext
from app.projects.errors import ProjectMemberQuotaExceeded
from app.projects.invitation_repository import InvitationRepository
from app.projects.invitation_service import InvitationService
from app.projects.models import ProjectRole
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.models import QuotaExceeded, QuotaSourceRef, _issue_quota_compensation_authority, _issue_quota_reconciliation_authority
from app.quotas.reconciliation import QuotaReconciler
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig

_MIB = 1024 * 1024
_GIB = 1024 * _MIB
_NOW = datetime(2026, 7, 20, 12, tzinfo=UTC)


def _source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="test-m8-capacity",
        hmac_hex=hmac.new(b"test-m8-capacity-key" * 2, payload, hashlib.sha256).hexdigest(),
    )


def _project_context(private_context) -> ProjectContext:
    return ProjectContext(
        user_id=private_context.user_id,
        project_id=private_context.project_id,
        membership_id=private_context.membership_id,
        role=private_context.role,
        capabilities=private_context.capabilities,
        membership_version=private_context.membership_version,
        request_id=private_context.request_id,
    )


class _BoundedChunkStream:
    def __init__(self, total: int, *, chunk_size: int = 64 * 1024) -> None:
        self.remaining = total
        self.chunk_size = chunk_size
        self.max_yielded = 0
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if self.remaining == 0:
            self.closed = True
            raise StopAsyncIteration
        size = min(self.chunk_size, self.remaining)
        self.remaining -= size
        self.max_yielded = max(self.max_yielded, size)
        return b"x" * size

    async def aclose(self) -> None:
        self.closed = True


class _FailingCloseStream(_BoundedChunkStream):
    async def aclose(self) -> None:
        self.closed = True
        raise RuntimeError("synthetic stream close failure")


def test_capacity_defaults_are_pinned_to_the_release_boundary() -> None:
    config = QuotaConfig()
    assert config.default_member_limit == 20
    assert config.default_storage_bytes_limit == 5 * _GIB
    assert config.default_concurrent_run_limit == 3
    assert config.default_mcp_calls_daily_limit == 10_000
    assert PrivateFileLimits() == PrivateFileLimits(
        max_files=10,
        max_file_size=100 * _MIB,
        max_total_size=100 * _MIB,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_default_concurrent_run_limit_is_three_and_fourth_is_atomic(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    keys = [f"run:{uuid.uuid4()}" for _ in range(4)]
    barrier = asyncio.Barrier(3)

    async def reserve(key: str):
        await barrier.wait()
        return await service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, key)

    try:
        async with asyncio.timeout(30):
            reserved = await asyncio.gather(*(reserve(key) for key in keys[:3]))
            assert sorted(result.reserved for result in reserved) == [1, 2, 3]

            async with seed.factory() as session:
                before_rejection = (
                    await session.scalar(text("SELECT count(*) FROM runs WHERE project_id=:project_id"), {"project_id": seed.owner_a.project_id}),
                    await session.scalar(text("SELECT count(*) FROM jobs WHERE project_id=:project_id"), {"project_id": seed.owner_a.project_id}),
                    await session.scalar(
                        text(
                            """SELECT count(*) FROM project_usage_ledger
                               WHERE project_id=:project_id AND dimension='concurrent_runs'"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    ),
                )

            with pytest.raises(QuotaExceeded):
                await service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, keys[3])

            async with seed.factory() as session:
                after_rejection = (
                    await session.scalar(text("SELECT count(*) FROM runs WHERE project_id=:project_id"), {"project_id": seed.owner_a.project_id}),
                    await session.scalar(text("SELECT count(*) FROM jobs WHERE project_id=:project_id"), {"project_id": seed.owner_a.project_id}),
                    await session.scalar(
                        text(
                            """SELECT count(*) FROM project_usage_ledger
                               WHERE project_id=:project_id AND dimension='concurrent_runs'"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    ),
                )
            assert after_rejection == before_rejection == (0, 0, 3)

            released = await service.release_new_session(
                _issue_quota_compensation_authority(seed.owner_a_scope, reason="run_terminal"),
                "concurrent_runs",
                1,
                keys[0],
            )
            accepted = await service.reserve_new_session(seed.owner_a, "concurrent_runs", 1, keys[3])
            assert released.reserved == 2
            assert accepted.reserved == 3

            async with seed.factory() as session:
                state = (
                    await session.execute(
                        text(
                            """SELECT c.used,c.reserved,count(l.id),coalesce(sum(l.delta),0),
                                      count(*) FILTER (WHERE l.source_kind='release')
                               FROM project_usage_counters c
                               JOIN project_usage_ledger l
                                 ON l.project_id=c.project_id AND l.dimension=c.dimension AND l.bucket=c.bucket
                               WHERE c.project_id=:project_id AND c.dimension='concurrent_runs'
                               GROUP BY c.used,c.reserved"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    )
                ).one()
            assert tuple(state) == (0, 3, 5, 3, 1)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_member_limit_accepts_twenty_real_redeems_and_rolls_back_twenty_first(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    new_users = tuple((uuid.uuid4(), f"m8-member-{index}@example.com") for index in range(18))
    try:
        async with asyncio.timeout(30):
            await QuotaReconciler(seed.factory, quotas).execute(
                _issue_quota_reconciliation_authority(seed.owner_a.project_id, operation="quota_repair"),
                now=_NOW,
            )
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """INSERT INTO users
                           (id,email,system_role,created_at,needs_setup,token_version)
                           VALUES (:id,:email,'user',:now,false,0)"""
                    ),
                    [{"id": str(user_id), "email": email, "now": _NOW} for user_id, email in new_users],
                )

            for user_id, email in new_users[:17]:
                async with seed.factory() as session:
                    invitations = InvitationService(InvitationRepository(session), quota=enforcer)
                    created = await invitations.create(_project_context(seed.owner_a), email, ProjectRole.VIEWER, _NOW)
                    claim = await invitations.claim(created.token, _NOW)
                    await invitations.redeem(user_id, email, claim, _NOW)

            overflow_user_id, overflow_email = new_users[17]
            async with seed.factory() as session:
                invitations = InvitationService(InvitationRepository(session), quota=enforcer)
                overflow = await invitations.create(_project_context(seed.owner_a), overflow_email, ProjectRole.VIEWER, _NOW)
                claim = await invitations.claim(overflow.token, _NOW)
                with pytest.raises(ProjectMemberQuotaExceeded):
                    await invitations.redeem(overflow_user_id, overflow_email, claim, _NOW)

            async with seed.factory() as session:
                state = (
                    await session.execute(
                        text(
                            """SELECT
                                 (SELECT count(*) FROM project_memberships
                                  WHERE project_id=:project_id AND status='active'),
                                 (SELECT count(*) FROM project_memberships
                                  WHERE project_id=:project_id AND user_id=:overflow_user_id),
                                 (SELECT status FROM project_invitations WHERE id=:invitation_id),
                                 (SELECT version FROM project_invitations WHERE id=:invitation_id),
                                 (SELECT reserved FROM project_usage_counters
                                  WHERE project_id=:project_id AND dimension='members' AND bucket='lifetime')"""
                        ),
                        {
                            "project_id": seed.owner_a.project_id,
                            "overflow_user_id": str(overflow_user_id),
                            "invitation_id": overflow.invitation.id,
                        },
                    )
                ).one()
            assert tuple(state) == (20, 0, "pending", 1, 20)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_private_file_boundary_streams_100_mib_and_cleans_100_mib_plus_one(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    files = PrivateFileService(seed.factory, quota=ProjectQuotaEnforcer(quotas))
    thread_id = str(uuid.uuid4())
    accepted_stream = _BoundedChunkStream(100 * _MIB)
    rejected_stream = _BoundedChunkStream(100 * _MIB + 1)
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )

        async with asyncio.timeout(90):
            accepted = await files.upload(
                seed.owner_a,
                thread_id=thread_id,
                logical_path="m8-boundary.bin",
                media_type="application/octet-stream",
                chunks=accepted_stream,
            )
            assert accepted.size == 100 * _MIB
            assert accepted_stream.max_yielded == 64 * 1024
            assert accepted_stream.closed is True

            async with seed.factory() as session:
                before_rejection = (
                    await session.scalar(
                        text(
                            """SELECT reserved FROM project_usage_counters
                               WHERE project_id=:project_id AND dimension='storage_bytes'"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    ),
                    await session.scalar(
                        text(
                            """SELECT count(*) FROM project_usage_ledger
                               WHERE project_id=:project_id AND dimension='storage_bytes'"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    ),
                )

            with pytest.raises(PrivateWorkTooLarge):
                await files.upload(
                    seed.owner_a,
                    thread_id=thread_id,
                    logical_path="m8-overflow.bin",
                    media_type="application/octet-stream",
                    chunks=rejected_stream,
                )
            assert rejected_stream.max_yielded == 64 * 1024
            assert rejected_stream.closed is True

            async with seed.factory() as session:
                after_rejection = (
                    await session.scalar(
                        text(
                            """SELECT reserved FROM project_usage_counters
                               WHERE project_id=:project_id AND dimension='storage_bytes'"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    ),
                    await session.scalar(
                        text(
                            """SELECT count(*) FROM project_usage_ledger
                               WHERE project_id=:project_id AND dimension='storage_bytes'"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    ),
                    await session.scalar(
                        text(
                            """SELECT count(*) FROM files
                               WHERE project_id=:project_id AND owner_user_id=:owner_user_id
                                 AND thread_id=:thread_id AND logical_path='m8-overflow.bin'"""
                        ),
                        {
                            "project_id": seed.owner_a.project_id,
                            "owner_user_id": str(seed.owner_a.user_id),
                            "thread_id": thread_id,
                        },
                    ),
                    await session.scalar(
                        text(
                            """SELECT count(*) FROM file_chunks c JOIN files f ON f.id=c.file_id
                               WHERE f.project_id=:project_id AND f.owner_user_id=:owner_user_id
                                 AND f.thread_id=:thread_id AND f.logical_path='m8-overflow.bin'"""
                        ),
                        {
                            "project_id": seed.owner_a.project_id,
                            "owner_user_id": str(seed.owner_a.user_id),
                            "thread_id": thread_id,
                        },
                    ),
                )
            assert before_rejection == (100 * _MIB, 1)
            assert after_rejection == (*before_rejection, 0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_oversized_stream_preserves_rejection_when_close_also_fails(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    files = PrivateFileService(seed.factory)
    thread_id = str(uuid.uuid4())
    stream = _FailingCloseStream(2, chunk_size=2)
    try:
        async with seed.factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(seed.project_agent_id, "project"),
            )
        with pytest.raises(PrivateWorkTooLarge):
            await files.upload(
                seed.owner_a,
                thread_id=thread_id,
                logical_path="m8-close-failure.bin",
                media_type="application/octet-stream",
                chunks=stream,
                limits=PrivateFileLimits(max_files=1, max_file_size=1, max_total_size=1),
            )
        assert stream.closed is True
        async with seed.factory() as session:
            rows = await session.scalar(
                text(
                    """SELECT count(*) FROM files
                       WHERE project_id=:project_id AND owner_user_id=:owner_user_id
                         AND thread_id=:thread_id AND logical_path='m8-close-failure.bin'"""
                ),
                {
                    "project_id": seed.owner_a.project_id,
                    "owner_user_id": str(seed.owner_a.user_id),
                    "thread_id": thread_id,
                },
            )
        assert rows == 0
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_storage_quota_accepts_five_gib_in_ledger_without_file_payload(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    try:
        async with asyncio.timeout(30):
            accepted = await service.reserve_new_session(seed.owner_a, "storage_bytes", 5 * _GIB, "file:m8-five-gib")
            assert accepted.reserved == 5 * _GIB
            with pytest.raises(QuotaExceeded):
                await service.reserve_new_session(seed.owner_a, "storage_bytes", 1, "file:m8-five-gib-plus-one")

            async with seed.factory() as session:
                state = (
                    await session.execute(
                        text(
                            """SELECT c.used,c.reserved,count(l.id),coalesce(sum(l.delta),0),
                                      (SELECT count(*) FROM file_chunks)
                               FROM project_usage_counters c
                               JOIN project_usage_ledger l
                                 ON l.project_id=c.project_id AND l.dimension=c.dimension AND l.bucket=c.bucket
                               WHERE c.project_id=:project_id AND c.dimension='storage_bytes'
                               GROUP BY c.used,c.reserved"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    )
                ).one()
            assert tuple(state) == (0, 5 * _GIB, 1, 5 * _GIB, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_mcp_ten_thousand_consumes_once_and_overflow_prevents_transport(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    quotas = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    enforcer = ProjectQuotaEnforcer(quotas)
    transport_calls = 0

    async def dispatch(dispatch_id: uuid.UUID) -> None:
        nonlocal transport_calls
        await enforcer.consume_mcp_dispatch(seed.owner_a, dispatch_id=dispatch_id, now=_NOW)
        transport_calls += 1

    try:
        async with asyncio.timeout(30):
            seeded = await quotas.consume_new_session(seed.owner_a, "mcp_calls_daily", 9_999, "mcp:m8-seed", now=_NOW)
            assert seeded.used == 9_999
            await dispatch(uuid.uuid4())
            with pytest.raises(PrivateWorkMcpQuotaExceeded):
                await dispatch(uuid.uuid4())
            assert transport_calls == 1

            async with seed.factory() as session:
                state = (
                    await session.execute(
                        text(
                            """SELECT c.used,c.reserved,count(l.id),coalesce(sum(l.delta),0)
                               FROM project_usage_counters c
                               JOIN project_usage_ledger l
                                 ON l.project_id=c.project_id AND l.dimension=c.dimension AND l.bucket=c.bucket
                               WHERE c.project_id=:project_id AND c.dimension='mcp_calls_daily'
                                 AND c.bucket='2026-07-20'
                               GROUP BY c.used,c.reserved"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    )
                ).one()
            assert tuple(state) == (10_000, 0, 2, 10_000)
    finally:
        await seed.engine.dispose()
