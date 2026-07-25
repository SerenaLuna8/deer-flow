from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from support.m4_private_threads import seed_m4_thread_database

from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.quotas.models import (
    ProjectQuotaLimits,
    QuotaForbidden,
    QuotaReconciliationAuthority,
    QuotaSourceRef,
    _issue_quota_reconciliation_authority,
)
from app.quotas.reconciliation import QuotaReconciler
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.private_work.model import PrivateFileRow
from deerflow.persistence.shared_assets import (
    SkillRow,
    SkillVersionFileRow,
    SkillVersionRow,
)


def _source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="test-quota",
        hmac_hex=hmac.new(
            b"test-quota-hmac-key" * 2,
            payload,
            hashlib.sha256,
        ).hexdigest(),
    )


def _authority(project_id: object) -> QuotaReconciliationAuthority:
    return _issue_quota_reconciliation_authority(
        project_id,
        operation="quota_repair",
    )


async def _seed_authoritative_usage(seed) -> tuple[str, tuple[str, str]]:
    thread_id = str(uuid.uuid4())
    run_ids = (str(uuid.uuid4()), str(uuid.uuid4()))
    skill_id = uuid.uuid4()
    skill_version_id = uuid.uuid4()
    skill_content = b"s" * 37
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )
        runs = PrivateRunRepository(session)
        for run_id in run_ids:
            await runs.create(
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                request=PrivateRunCreate(run_id=run_id, status="pending"),
            )
        session.add(
            PrivateFileRow(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                thread_id=thread_id,
                kind="upload",
                logical_path="quota.bin",
                media_type="application/octet-stream",
                size=100,
                sha256="a" * 64,
                status="ready",
            )
        )
        session.add(
            SkillRow(
                id=skill_id,
                scope="project",
                project_id=seed.owner_a.project_id,
                slug=f"quota-reconcile-{str(skill_id)[:8]}",
                display_name="Quota reconciliation Skill",
                created_by_user_id=str(seed.owner_a.user_id),
            )
        )
        session.add(
            SkillVersionRow(
                id=skill_version_id,
                skill_id=skill_id,
                version_number=1,
                workflow_status="draft",
                description="Count every stored project Skill version",
                frontmatter={},
                secret_requirements=[],
                scan_decision="allow",
                scan_summary={},
                payload_checksum="b" * 64,
                created_by_user_id=str(seed.owner_a.user_id),
            )
        )
        session.add(
            SkillVersionFileRow(
                skill_version_id=skill_version_id,
                path="SKILL.md",
                media_type="text/markdown",
                size_bytes=len(skill_content),
                sha256=hashlib.sha256(skill_content).hexdigest(),
                content=skill_content,
            )
        )
    return thread_id, run_ids


@pytest.mark.postgres
@pytest.mark.anyio
async def test_reconciliation_dry_run_is_zero_write_and_execute_converges(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    reconciler = QuotaReconciler(seed.factory, service)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    try:
        await _seed_authoritative_usage(seed)
        with pytest.raises(QuotaForbidden):
            await reconciler.preview(seed.owner_a.project_id, now=now)
        forged = object.__new__(QuotaReconciliationAuthority)
        object.__setattr__(forged, "project_id", seed.owner_a.project_id)
        object.__setattr__(forged, "operation", "quota_repair")
        with pytest.raises(QuotaForbidden):
            await reconciler.preview(forged, now=now)
        authority = _authority(seed.owner_a.project_id)
        preview = await reconciler.preview(authority, now=now)

        assert {(item.dimension, item.current, item.expected) for item in preview.differences} >= {
            ("members", 0, 3),
            ("storage_bytes", 0, 137),
            ("concurrent_runs", 0, 2),
        }
        async with seed.factory() as session:
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*) FROM project_usage_counters
                       WHERE project_id=:project_id"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
                == 0
            )
            assert (
                await session.scalar(
                    text(
                        """SELECT count(*) FROM project_usage_ledger
                       WHERE project_id=:project_id"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
                == 0
            )

        applied = await reconciler.execute(authority, now=now)
        assert applied.applied is True
        assert len(applied.differences) >= 3
        second = await reconciler.execute(authority, now=now)
        assert second.differences == ()
        assert second.applied is False

        async with seed.factory() as session:
            counters = {
                row.dimension: (row.used, row.reserved)
                for row in (
                    await session.execute(
                        text(
                            """SELECT dimension,used,reserved
                               FROM project_usage_counters
                               WHERE project_id=:project_id"""
                        ),
                        {"project_id": seed.owner_a.project_id},
                    )
                ).all()
            }
            adjustment_count = await session.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND source_kind='reconcile_adjustment'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert counters["members"] == (0, 3)
        assert counters["storage_bytes"] == (0, 137)
        assert counters["concurrent_runs"] == (0, 2)
        assert adjustment_count == 3
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_reconciliation_repairs_counter_drift_with_compensation_rows(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    reconciler = QuotaReconciler(seed.factory, service)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    try:
        await _seed_authoritative_usage(seed)
        authority = _authority(seed.owner_a.project_id)
        await reconciler.execute(authority, now=now)
        await service.consume_new_session(
            seed.owner_a,
            "mcp_calls_daily",
            2,
            "call:drift",
            now=now,
        )
        async with seed.factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_usage_counters
                       SET reserved=99
                       WHERE project_id=:project_id
                         AND dimension='storage_bytes'
                         AND bucket='lifetime'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            await session.execute(
                text(
                    """UPDATE project_usage_counters
                       SET used=7
                       WHERE project_id=:project_id
                         AND dimension='mcp_calls_daily'
                         AND bucket='2026-07-16'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            await session.execute(
                text(
                    """UPDATE project_usage_counters
                       SET used=3,reserved=0
                       WHERE project_id=:project_id
                         AND dimension='members'
                         AND bucket='lifetime'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )

        preview = await reconciler.preview(authority, now=now)
        assert {(item.dimension, item.current, item.expected) for item in preview.differences} >= {
            ("storage_bytes", 99, 137),
            ("mcp_calls_daily", 7, 2),
            ("members", 3, 3),
        }
        await reconciler.execute(authority, now=now)

        async with seed.factory() as session:
            repaired = (
                await session.execute(
                    text(
                        """SELECT dimension,used,reserved
                           FROM project_usage_counters
                           WHERE project_id=:project_id
                             AND dimension IN
                                 ('members','storage_bytes','mcp_calls_daily')
                           ORDER BY dimension"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).all()
            adjustments = (
                await session.execute(
                    text(
                        """SELECT dimension,delta FROM project_usage_ledger
                           WHERE project_id=:project_id
                             AND source_kind='reconcile_adjustment'
                           ORDER BY occurred_at,id"""
                    ),
                    {"project_id": seed.owner_a.project_id},
                )
            ).all()
        assert [(row.dimension, row.used, row.reserved) for row in repaired] == [
            ("mcp_calls_daily", 2, 0),
            ("members", 0, 3),
            ("storage_bytes", 0, 137),
        ]
        assert ("mcp_calls_daily", -5) in adjustments
        assert ("storage_bytes", 38) in adjustments
        async with seed.factory() as session:
            axis_adjustments = await session.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND source_kind IN
                             ('reconcile_axis_debit','reconcile_axis_credit')"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert axis_adjustments == 2
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_reconciliation_crossing_threshold_records_warning_once(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_m4_thread_database(migrated_postgres_database_url)
    service = QuotaService(seed.factory, QuotaConfig(), source_ref_hasher=_source_ref)
    reconciler = QuotaReconciler(seed.factory, service)
    authority = _authority(seed.owner_a.project_id)
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    try:
        async with seed.factory() as session, session.begin():
            await service.set_limits(
                session,
                seed.owner_a,
                ProjectQuotaLimits(member_limit=3),
                expected_version=0,
            )
        await reconciler.execute(authority, now=now)
        await reconciler.execute(authority, now=now)

        async with seed.factory() as session:
            threshold_count = await session.scalar(
                text(
                    """SELECT count(*) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='members'
                         AND source_kind='reconcile_threshold'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
            net = await session.scalar(
                text(
                    """SELECT sum(delta) FROM project_usage_ledger
                       WHERE project_id=:project_id
                         AND dimension='members'"""
                ),
                {"project_id": seed.owner_a.project_id},
            )
        assert threshold_count == 1
        assert net == 3
    finally:
        await seed.engine.dispose()
