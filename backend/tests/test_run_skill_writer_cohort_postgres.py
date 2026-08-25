from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.private_work.run_skill_writer_cohort as cohort_module
from app.private_work.legacy_run_skill_snapshot_writer import (
    RunSkillSnapshotWriterReadback,
    frozen_run_skill_snapshot_writer,
)
from app.private_work.run_skill_writer_cohort import (
    RunSkillWriterCohortConflict,
    RunSkillWriterCohortLease,
    RunSkillWriterCohortUnavailable,
    require_active_run_skill_writer_cohort,
)
from app.reliability.readiness import ReliabilityReadinessService

pytestmark = pytest.mark.run_skill_writer_cohort_control


def _coordinate(*, mode: str = "legacy_v3", digest_suffix: str = "a") -> RunSkillSnapshotWriterReadback:
    return RunSkillSnapshotWriterReadback(
        writer_mode=mode,  # type: ignore[arg-type]
        artifact_version="r1-test-artifact",
        legacy_policy_digest=("0" * 63) + digest_suffix,
        ready=True,
    )


async def _take_raw_shared_lock(connection, namespace_key: int, lock_key: int) -> None:
    assert (
        await connection.scalar(
            text(
                """SELECT pg_try_advisory_lock_shared(
                           CAST(:namespace_key AS integer),
                           CAST(:lock_key AS integer)
                       )"""
            ),
            {"namespace_key": namespace_key, "lock_key": lock_key},
        )
        is True
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cohort_rejects_concurrent_full_coordinate_mismatch_and_releases_cleanly(
    postgres_database_url: str,
) -> None:
    engines = [create_async_engine(postgres_database_url) for _ in range(4)]
    first: RunSkillWriterCohortLease | None = None
    second: RunSkillWriterCohortLease | None = None
    replacement: RunSkillWriterCohortLease | None = None
    coordinate = _coordinate(digest_suffix="a")
    conflicting = _coordinate(digest_suffix="b")
    try:
        first = await RunSkillWriterCohortLease.acquire(
            engines[0],
            coordinate,
            process_role="gateway",
        )
        second = await RunSkillWriterCohortLease.acquire(
            engines[1],
            coordinate,
            process_role="scheduler",
        )

        with pytest.raises(RunSkillWriterCohortConflict):
            await RunSkillWriterCohortLease.acquire(
                engines[2],
                conflicting,
                process_role="gateway",
            )

        await first.close()
        first = None
        await second.close()
        second = None
        replacement = await RunSkillWriterCohortLease.acquire(
            engines[3],
            conflicting,
            process_role="gateway",
        )
        assert replacement.ready is True
    finally:
        for lease in (replacement, second, first):
            if lease is not None:
                await lease.close()
        for engine in engines:
            await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_opposite_coordinates_cannot_both_join_from_empty_database(
    postgres_database_url: str,
) -> None:
    engines = [create_async_engine(postgres_database_url) for _ in range(2)]
    leases: list[RunSkillWriterCohortLease] = []

    async def join(index: int, coordinate: RunSkillSnapshotWriterReadback):
        try:
            lease = await RunSkillWriterCohortLease.acquire(
                engines[index],
                coordinate,
                process_role="gateway" if index == 0 else "scheduler",
            )
        except RunSkillWriterCohortConflict as error:
            return error
        leases.append(lease)
        return lease

    try:
        results = await asyncio.gather(
            join(0, _coordinate(mode="legacy_v3", digest_suffix="a")),
            join(1, _coordinate(mode="v4_reference", digest_suffix="b")),
        )
        assert sum(type(result) is RunSkillWriterCohortConflict for result in results) >= 1
        assert sum(type(result) is RunSkillWriterCohortLease for result in results) <= 1
    finally:
        for lease in leases:
            await lease.close()
        for engine in engines:
            await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_unpublished_partial_coordinate_is_ignored(
    postgres_database_url: str,
) -> None:
    partial_engine = create_async_engine(postgres_database_url)
    joiner_engine = create_async_engine(postgres_database_url)
    partial_connection = await partial_engine.connect()
    lease: RunSkillWriterCohortLease | None = None
    try:
        partial_coordinate = cohort_module._canonical_coordinate(  # noqa: SLF001
            _coordinate(digest_suffix="a"),
        )
        for lock_key in cohort_module._positioned_lock_keys(  # noqa: SLF001
            partial_coordinate,
        )[:4]:
            await _take_raw_shared_lock(
                partial_connection,
                cohort_module._COORDINATE_NAMESPACE,  # noqa: SLF001
                lock_key,
            )
        await partial_connection.commit()

        lease = await RunSkillWriterCohortLease.acquire(
            joiner_engine,
            _coordinate(digest_suffix="b"),
            process_role="gateway",
        )
        assert lease.ready is True
    finally:
        if lease is not None:
            await lease.close()
        if not partial_connection.closed:
            await partial_connection.execute(text("SELECT pg_advisory_unlock_all()"))
            await partial_connection.commit()
        await partial_connection.close()
        await joiner_engine.dispose()
        await partial_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_published_pid_without_exact_owner_token_fails_admission_without_waiting(
    postgres_database_url: str,
) -> None:
    owner_engine = create_async_engine(postgres_database_url)
    forged_engine = create_async_engine(postgres_database_url)
    observer_engine = create_async_engine(postgres_database_url)
    observer_factory = async_sessionmaker(observer_engine, expire_on_commit=False)
    coordinate = _coordinate()
    owner: RunSkillWriterCohortLease | None = None
    forged_connection = await forged_engine.connect()
    try:
        owner = await RunSkillWriterCohortLease.acquire(
            owner_engine,
            coordinate,
            process_role="gateway",
            process_authority=True,
        )
        encoded = cohort_module._canonical_coordinate(coordinate)  # noqa: SLF001
        for lock_key in cohort_module._positioned_lock_keys(encoded):  # noqa: SLF001
            await _take_raw_shared_lock(
                forged_connection,
                cohort_module._COORDINATE_NAMESPACE,  # noqa: SLF001
                lock_key,
            )
        await _take_raw_shared_lock(
            forged_connection,
            cohort_module._COORDINATE_NAMESPACE,  # noqa: SLF001
            cohort_module._SENTINEL_KEY,  # noqa: SLF001
        )
        await forged_connection.commit()

        async with observer_factory() as session, session.begin():
            with pytest.raises(RunSkillWriterCohortUnavailable):
                await asyncio.wait_for(
                    require_active_run_skill_writer_cohort(session, coordinate),
                    timeout=0.5,
                )
    finally:
        if owner is not None:
            await owner.close()
        if not forged_connection.closed:
            await forged_connection.execute(text("SELECT pg_advisory_unlock_all()"))
            await forged_connection.commit()
        await forged_connection.close()
        await observer_engine.dispose()
        await forged_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_lost_owner_connection_cannot_be_substituted_by_same_coordinate_peer(
    postgres_database_url: str,
) -> None:
    owner_engine = create_async_engine(postgres_database_url)
    peer_engine = create_async_engine(postgres_database_url)
    observer_engine = create_async_engine(postgres_database_url)
    observer_factory = async_sessionmaker(observer_engine, expire_on_commit=False)
    coordinate = _coordinate()
    owner: RunSkillWriterCohortLease | None = None
    peer: RunSkillWriterCohortLease | None = None
    try:
        owner = await RunSkillWriterCohortLease.acquire(
            owner_engine,
            coordinate,
            process_role="gateway",
            heartbeat_interval_seconds=0.05,
            process_authority=True,
        )
        peer = await RunSkillWriterCohortLease.acquire(
            peer_engine,
            coordinate,
            process_role="scheduler",
        )
        async with observer_factory() as session, session.begin():
            await require_active_run_skill_writer_cohort(session, coordinate)
            assert (
                await session.scalar(
                    text("SELECT pg_terminate_backend(CAST(:pid AS integer))"),
                    {"pid": owner.backend_pid},
                )
                is True
            )

        async with asyncio.timeout(5):
            await owner.wait_lost()
        assert owner.ready is False

        async with observer_factory() as session, session.begin():
            with pytest.raises(RunSkillWriterCohortUnavailable):
                await require_active_run_skill_writer_cohort(session, coordinate)
    finally:
        for lease in (peer, owner):
            if lease is not None:
                await lease.close()
        await observer_engine.dispose()
        await peer_engine.dispose()
        await owner_engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_public_readiness_requires_this_process_live_cohort(
    postgres_database_url: str,
) -> None:
    class ReadySchema:
        async def require_ready(self, _session) -> None:
            return None

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    writer = frozen_run_skill_snapshot_writer()

    async def read_status() -> tuple[str, bool]:
        async with factory() as session, session.begin():
            readiness = await ReliabilityReadinessService(
                ReadySchema(),  # type: ignore[arg-type]
                session,
                "writer-cohort-readiness",
                worker_fleet=lambda: "ready",
                stream=lambda: "ready",
                quota=lambda: "ready",
                audit=lambda: "ready",
            ).read()
        return readiness.status, readiness.run_skill_writer_ready

    lease: RunSkillWriterCohortLease | None = None
    try:
        assert await read_status() == ("degraded", False)
        lease = await RunSkillWriterCohortLease.acquire(
            engine,
            writer,
            process_role="gateway",
            process_authority=True,
        )
        assert await read_status() == ("ready", True)
        await lease.close()
        lease = None
        assert await read_status() == ("degraded", False)
    finally:
        if lease is not None:
            await lease.close()
        await engine.dispose()
