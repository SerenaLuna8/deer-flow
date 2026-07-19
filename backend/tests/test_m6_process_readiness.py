from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.automations.ownership import AutomationSchedulerOwnership
from app.final_schema import FINAL_REQUIRED_RELATIONS, M7_FINAL_SCHEMA_REVISION
from app.reliability.process_readiness import read_process_readiness


@pytest.mark.asyncio
async def test_pre_expand_missing_relations_report_unavailable_without_aggregate_queries() -> None:
    class PreExpandSession:
        calls = 0

        async def scalar(self, _query: object, _params: object = None) -> bool:
            self.calls += 1
            return False

        async def execute(self, _query: object, _params: object = None) -> object:
            raise AssertionError("pre-expand readiness must not query M6 aggregate tables")

    session = PreExpandSession()
    snapshot = await read_process_readiness(
        session,  # type: ignore[arg-type]
        role="gateway",
        scheduler_enabled=True,
        worker_fresh_for_seconds=60,
    )

    assert session.calls == 2
    assert snapshot.ready is False
    assert snapshot.worker_fleet == "unavailable"
    assert snapshot.scheduler_ownership == "unowned"
    assert snapshot.schema_state == "unavailable"


@pytest.mark.asyncio
async def test_missing_worker_relation_reports_schema_unavailable_without_aggregate_queries() -> None:
    class MissingWorkerRelationSession:
        def __init__(self) -> None:
            self.values = iter(
                (
                    M7_FINAL_SCHEMA_REVISION,
                    FINAL_REQUIRED_RELATIONS,
                    False,
                )
            )

        async def scalar(self, _query: object, _params: object = None) -> object:
            return next(self.values)

        async def execute(self, _query: object, _params: object = None) -> object:
            raise AssertionError("missing worker relation must not query aggregates")

    snapshot = await read_process_readiness(
        MissingWorkerRelationSession(),  # type: ignore[arg-type]
        role="worker",
        scheduler_enabled=False,
        worker_fresh_for_seconds=60,
    )

    assert snapshot.ready is False
    assert snapshot.schema_state == "unavailable"
    assert snapshot.worker_fleet == "unavailable"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_missing_is_not_ready_and_scheduler_disabled_is_legal(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            snapshot = await read_process_readiness(
                session,
                role="gateway",
                scheduler_enabled=False,
                worker_fresh_for_seconds=60,
            )
        assert snapshot.role == "gateway"
        assert snapshot.worker_fleet == "unavailable"
        assert snapshot.worker_count == 0
        assert snapshot.worker_capacity == 0
        assert snapshot.worker_oldest_heartbeat_age_seconds is None
        assert snapshot.scheduler == "disabled"
        assert snapshot.scheduler_ownership == "disabled"
        assert snapshot.ready is False
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_process_readiness_reports_only_public_aggregate_fields(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,max_concurrent_jobs,draining,
                        started_at,heartbeat_at)
                       VALUES (gen_random_uuid(),'m6','[\"private_run\"]'::jsonb,4,
                               false,:started,:heartbeat)"""
                ),
                {"started": now - timedelta(minutes=2), "heartbeat": now},
            )
        async with factory() as session:
            snapshot = await read_process_readiness(
                session,
                role="worker",
                scheduler_enabled=False,
                worker_fresh_for_seconds=60,
                now=now,
            )
        payload = snapshot.as_public_dict()
        assert payload == {
            "ready": True,
            "role": "worker",
            "worker_fleet": "ready",
            "worker_count": 1,
            "worker_capacity": 4,
            "worker_oldest_heartbeat_age_seconds": 0,
            "scheduler": "disabled",
            "scheduler_ownership": "disabled",
            "schema_state": "ready",
        }
        serialized = str(payload).lower()
        for forbidden in (
            "hostname",
            "backend_pid",
            "pid",
            "lock_key",
            "token",
            "postgresql://",
        ):
            assert forbidden not in serialized
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_enabled_requires_owned_session_and_loss_fails_closed(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    ownership = AutomationSchedulerOwnership(engine)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO worker_nodes
                       (id,version,capabilities_json,max_concurrent_jobs,draining,
                        started_at,heartbeat_at)
                       VALUES (gen_random_uuid(),'m6','[\"private_run\"]'::jsonb,2,
                               false,:started,:heartbeat)"""
                ),
                {"started": now, "heartbeat": now},
            )
        async with factory() as session:
            unowned = await read_process_readiness(
                session,
                role="gateway",
                scheduler_enabled=True,
                worker_fresh_for_seconds=60,
                now=now,
            )
        assert unowned.scheduler == "unavailable"
        assert unowned.scheduler_ownership == "unowned"
        assert unowned.ready is False

        await ownership.acquire()
        async with factory() as session:
            owned = await read_process_readiness(
                session,
                role="scheduler",
                scheduler_enabled=True,
                scheduler_ownership=ownership,
                worker_fresh_for_seconds=60,
                now=now,
            )
        assert owned.scheduler == "ready"
        assert owned.scheduler_ownership == "owned"
        assert owned.ready is True

        lost_ownership = SimpleNamespace(is_acquired=False, is_lost=True)
        async with factory() as session:
            lost = await read_process_readiness(
                session,
                role="scheduler",
                scheduler_enabled=True,
                scheduler_ownership=lost_ownership,
                worker_fresh_for_seconds=60,
                now=now,
            )
        assert lost.scheduler == "unavailable"
        assert lost.scheduler_ownership == "ownership_lost"
        assert lost.ready is False
        serialized = str(lost.as_public_dict()).lower()
        assert "backend_pid" not in serialized
        assert "lock_key" not in serialized
    finally:
        await ownership.release()
        await engine.dispose()
