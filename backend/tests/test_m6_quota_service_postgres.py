from __future__ import annotations

import asyncio
import hashlib
import hmac
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from support.m4_private_threads import seed_m4_thread_database

from app.quotas.models import (
    ProjectQuotaLimits,
    QuotaCompensationAuthority,
    QuotaConflict,
    QuotaExceeded,
    QuotaForbidden,
    QuotaPolicyInvalid,
    QuotaSourceRef,
    _issue_quota_compensation_authority,
)
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from deerflow.config.quota_config import QuotaConfig


def _source_ref(payload: bytes) -> QuotaSourceRef:
    digest = hmac.new(b"test-quota-hmac-key" * 2, payload, hashlib.sha256).hexdigest()
    return QuotaSourceRef(key_id="test-quota", hmac_hex=digest)


def _compensation(seed, *, reason: str = "run_terminal") -> QuotaCompensationAuthority:
    return _issue_quota_compensation_authority(
        seed.owner_a_scope,
        reason=reason,
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_exact_reservation_survives_active_hmac_key_rotation(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    keys = {"audit-old": b"o" * 32, "audit-new": b"n" * 32}
    old_service = QuotaService(
        seed.factory,
        QuotaConfig(),
        source_ref_hasher=AuditHmacKeyring(
            active_key_id="audit-old",
            _keys=keys,
        ),
    )
    new_service = QuotaService(
        seed.factory,
        QuotaConfig(),
        source_ref_hasher=AuditHmacKeyring(
            active_key_id="audit-new",
            _keys=keys,
        ),
    )
    key = f"run:{uuid.uuid4()}"
    try:
        reserved = await old_service.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            1,
            key,
        )
        replay = await new_service.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            1,
            key,
        )
        released = await new_service.release_new_session(
            _compensation(seed),
            "concurrent_runs",
            1,
            key,
        )

        assert reserved.created is True
        assert replay.created is False
        assert replay.reserved == 1
        assert released.reserved == 0
        async with seed.factory() as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT source_kind,source_ref_key_id
                           FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND dimension='concurrent_runs'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).all()
        assert len(rows) == 2
        assert {(kind, key_id) for kind, key_id in rows} == {
            ("reserve", "audit-old"),
            ("release", "audit-new"),
        }
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_effective_limits_pin_defaults_and_admin_can_only_tighten(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    try:
        async with seed.factory() as session, session.begin():
            assert (
                await service.effective_limit(
                    session,
                    seed.owner_a.project_id,
                    "members",
                )
                == 20
            )
            assert (
                await service.effective_limit(
                    session,
                    seed.owner_a.project_id,
                    "storage_bytes",
                )
                == 5_368_709_120
            )
            assert (
                await service.effective_limit(
                    session,
                    seed.owner_a.project_id,
                    "concurrent_runs",
                )
                == 3
            )
            assert (
                await service.effective_limit(
                    session,
                    seed.owner_a.project_id,
                    "mcp_calls_daily",
                )
                == 10_000
            )
            policy = await service.set_limits(
                session,
                seed.owner_a,
                ProjectQuotaLimits(
                    member_limit=10,
                    storage_bytes_limit=1024,
                    concurrent_run_limit=2,
                    mcp_calls_daily_limit=100,
                ),
                expected_version=0,
            )
            assert policy.version == 1
            assert policy.effective.concurrent_run_limit == 2

        async with seed.factory() as session, session.begin():
            with pytest.raises(QuotaForbidden):
                await service.set_limits(
                    session,
                    seed.viewer,
                    ProjectQuotaLimits(concurrent_run_limit=1),
                    expected_version=1,
                )

        async with seed.factory() as session, session.begin():
            with pytest.raises(QuotaPolicyInvalid):
                await service.set_limits(
                    session,
                    seed.owner_a,
                    ProjectQuotaLimits(concurrent_run_limit=4),
                    expected_version=1,
                )

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_quotas SET concurrent_run_limit=99
                       WHERE project_id=:project_id"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            assert (
                await service.effective_limit(
                    session,
                    seed.owner_a.project_id,
                    "concurrent_runs",
                )
                == 3
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_concurrent_reservations_never_exceed_limit_and_release_compensates(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    try:
        held = await service.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            1,
            "run:held",
        )
        results = await asyncio.gather(
            *(
                service.reserve_new_session(
                    seed.owner_a,
                    "concurrent_runs",
                    1,
                    f"run:{index}",
                )
                for index in range(5)
            ),
            return_exceptions=True,
        )
        accepted = [item for item in results if not isinstance(item, Exception)]
        rejected = [item for item in results if isinstance(item, Exception)]
        assert len(accepted) == 2
        assert len(rejected) == 3
        assert all(isinstance(item, QuotaExceeded) for item in rejected)
        assert sum(item.threshold_crossed for item in (held, *accepted)) == 1

        with pytest.raises(QuotaForbidden):
            await service.release_new_session(
                _compensation(seed, reason="file_delete"),
                "concurrent_runs",
                1,
                "run:held",
            )
        forged = object.__new__(QuotaCompensationAuthority)
        object.__setattr__(forged, "scope", seed.owner_a_scope)
        object.__setattr__(forged, "reason", "run_terminal")
        object.__setattr__(forged, "dimension", "concurrent_runs")
        with pytest.raises(QuotaForbidden):
            await service.release_new_session(
                forged,
                "concurrent_runs",
                1,
                "run:held",
            )
        with pytest.raises(QuotaConflict):
            await service.release_new_session(
                _compensation(seed),
                "concurrent_runs",
                1,
                "run:never-reserved",
            )
        released = await service.release_new_session(
            _compensation(seed),
            "concurrent_runs",
            1,
            "run:held",
        )
        retried_release = await service.release_new_session(
            _compensation(seed),
            "concurrent_runs",
            1,
            "run:held",
        )
        replacement = await service.reserve_new_session(
            seed.owner_a,
            "concurrent_runs",
            1,
            "run:replacement",
        )
        assert released.reserved == 2
        assert retried_release.created is False
        assert replacement.reserved == 3

        async with seed.factory() as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT source_kind,delta FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND dimension='concurrent_runs'
                           ORDER BY occurred_at,id"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).all()
        assert sum(row.delta for row in rows) == 3
        assert sum(row.source_kind.endswith("_threshold") for row in rows) == 1
        assert sum(row.source_kind == "release" for row in rows) == 1
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_mutations_require_current_authority_and_bind_source_to_project_owner(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    try:
        first = await service.consume_new_session(
            seed.owner_a,
            "mcp_calls_daily",
            1,
            "call:same-low-entropy-key",
            now=now,
        )
        second_owner = await service.consume_new_session(
            seed.owner_b,
            "mcp_calls_daily",
            1,
            "call:same-low-entropy-key",
            now=now,
        )
        second_project = await service.consume_new_session(
            seed.project_b_owner_a,
            "mcp_calls_daily",
            1,
            "call:same-low-entropy-key",
            now=now,
        )
        assert first.created and second_owner.created and second_project.created

        async with seed.factory() as session:
            distinct = (
                await session.execute(
                    text(
                        """SELECT count(*),count(DISTINCT idempotency_key),
                                  count(DISTINCT source_ref_hmac)
                           FROM project_usage_ledger
                           WHERE dimension='mcp_calls_daily'"""
                    )
                )
            ).one()
        assert tuple(distinct) == (3, 3, 3)

        with pytest.raises(QuotaForbidden):
            await service.consume_new_session(
                seed.owner_a_scope,
                "mcp_calls_daily",
                1,
                "call:authority-free-scope",
                now=now,
            )

        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships SET version=version+1
                       WHERE id=:membership_id"""
                ),
                {"membership_id": seed.owner_b.membership_id},
            )
            await session.execute(
                text("UPDATE projects SET is_suspended=true WHERE id=:project_id"),
                {"project_id": seed.project_b_owner_a.project_id},
            )
        with pytest.raises(QuotaForbidden):
            await service.consume_new_session(
                seed.owner_b,
                "mcp_calls_daily",
                1,
                "call:stale-membership",
                now=now,
            )
        with pytest.raises(QuotaForbidden):
            await service.consume_new_session(
                seed.project_b_owner_a,
                "mcp_calls_daily",
                1,
                "call:suspended-project",
                now=now,
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_policy_tightening_records_threshold_once_without_changing_usage(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    try:
        for index in range(2):
            await service.reserve_new_session(
                seed.owner_a,
                "concurrent_runs",
                1,
                f"run:policy-threshold:{index}",
            )
        async with seed.factory() as session, session.begin():
            await service.set_limits(
                session,
                seed.owner_a,
                ProjectQuotaLimits(concurrent_run_limit=2),
                expected_version=0,
            )
        async with seed.factory() as session, session.begin():
            await service.set_limits(
                session,
                seed.owner_a,
                ProjectQuotaLimits(concurrent_run_limit=2),
                expected_version=1,
            )

        async with seed.factory() as session:
            counter = (
                await session.execute(
                    text(
                        """SELECT used,reserved FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension='concurrent_runs'
                             AND bucket='lifetime'"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
            threshold_count = await session.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='concurrent_runs'
                         AND source_kind='policy_threshold'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            net = await session.scalar(
                text(
                    """SELECT sum(delta) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='concurrent_runs'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert tuple(counter) == (0, 2)
        assert threshold_count == 1
        assert net == 2
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_daily_consumption_is_utc_bucketed_and_idempotent(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    first_day = datetime(2026, 7, 16, 23, 59, tzinfo=UTC)
    second_day = datetime(2026, 7, 17, 0, 0, tzinfo=UTC)
    try:
        first = await service.consume_new_session(
            seed.owner_a,
            "mcp_calls_daily",
            1,
            "call:42",
            now=first_day,
        )
        duplicate = await service.consume_new_session(
            seed.owner_a,
            "mcp_calls_daily",
            1,
            "call:42",
            now=first_day,
        )
        with pytest.raises(QuotaConflict):
            await service.consume_new_session(
                seed.owner_a,
                "mcp_calls_daily",
                2,
                "call:42",
                now=first_day,
            )
        next_bucket = await service.consume_new_session(
            seed.owner_a,
            "mcp_calls_daily",
            1,
            "call:42",
            now=second_day,
        )

        assert first.bucket == "2026-07-16"
        assert duplicate.created is False
        assert duplicate.used == 1
        assert next_bucket.bucket == "2026-07-17"
        assert next_bucket.used == 1
        async with seed.factory() as session:
            ledger_rows = (
                await session.execute(
                    text(
                        """SELECT idempotency_key,source_ref_hmac
                       FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='mcp_calls_daily'
                       ORDER BY bucket"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).all()
        assert len(ledger_rows) == 2
        assert all(len(row.idempotency_key) == 64 for row in ledger_rows)
        assert all(len(row.source_ref_hmac) == 64 for row in ledger_rows)
        assert all("call:42" not in tuple(row) for row in ledger_rows)

        with pytest.raises(QuotaPolicyInvalid):
            await service.reserve_new_session(
                seed.owner_a,
                "mcp_calls_daily",
                1,
                "invalid:reserve",
            )
        with pytest.raises(QuotaPolicyInvalid):
            await service.consume_new_session(
                seed.owner_a,
                "storage_bytes",
                1,
                "invalid:consume",
            )
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_transaction_rollback_leaves_no_counter_or_ledger(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    try:
        with pytest.raises(RuntimeError, match="domain rollback"):
            async with seed.factory() as session, session.begin():
                await service.reserve(
                    session,
                    seed.owner_a,
                    "storage_bytes",
                    64,
                    "file:rollback",
                )
                raise RuntimeError("domain rollback")

        async with seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                           (SELECT count(*) FROM project_usage_counters
                            WHERE project_id=:project_id),
                           (SELECT count(*) FROM project_usage_ledger
                            WHERE project_id=:project_id)"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).one()
        assert tuple(counts) == (0, 0)
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_usage_ledger_is_append_only(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    try:
        await service.reserve_new_session(
            seed.owner_a,
            "storage_bytes",
            1,
            f"file:{uuid.uuid4()}",
        )
        with pytest.raises(IntegrityError):
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """UPDATE project_usage_ledger SET delta=2
                           WHERE project_id=:project_id"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
        with pytest.raises(IntegrityError):
            async with seed.factory() as session, session.begin():
                await session.execute(
                    text(
                        """DELETE FROM project_usage_ledger
                           WHERE project_id=:project_id"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
    finally:
        await seed.engine.dispose()
