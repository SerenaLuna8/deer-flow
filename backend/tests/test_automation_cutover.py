from __future__ import annotations

from datetime import UTC, datetime
from types import MappingProxyType

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.cutover import AutomationCutoverGuard
from app.automations.errors import (
    AutomationCutover,
    AutomationMigrationRequired,
    AutomationUnavailable,
)
from deerflow.persistence.revisions import RevisionAncestry


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


async def _set_private_marker(seed: M4ThreadSeed, stage: str) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE private_work_cutover_state
                SET stage=:stage,
                    cutover_at=:cutover_at
                WHERE id=1"""
            ),
            {
                "stage": stage,
                "cutover_at": datetime.now(UTC) if stage == "cutover_complete" else None,
            },
        )


async def _set_automation_marker(seed: M4ThreadSeed, stage: str) -> None:
    async with seed.engine.begin() as connection:
        if stage == "migration_ready":
            migration_id = await connection.scalar(text("SELECT gen_random_uuid()"))
            await connection.execute(
                text(
                    """INSERT INTO automation_migration_runs
                    (id,mode,status,source_fingerprint,owner_map_digest,
                     source_task_count,source_run_count,source_probe_complete,
                     scope_relation_probe_complete,completed_at)
                    VALUES
                    (:id,'execute','completed',:digest,:digest,0,0,true,true,now())"""
                ),
                {"id": migration_id, "digest": "a" * 64},
            )
            await connection.execute(
                text(
                    """UPDATE automation_cutover_state
                    SET stage='migration_ready',migration_run_id=:id,
                        empty_domain_probe_complete=false,
                        final_schema_probe_complete=false,cutover_at=NULL,
                        updated_at=now()
                    WHERE id=1"""
                ),
                {"id": migration_id},
            )
            return
        await connection.execute(
            text(
                """UPDATE automation_cutover_state
                SET stage=:stage,
                    empty_domain_probe_complete=true,
                    final_schema_probe_complete=:complete,
                    cutover_at=:cutover_at,
                    updated_at=now()
                WHERE id=1"""
            ),
            {
                "stage": stage,
                "complete": stage == "cutover_complete",
                "cutover_at": datetime.now(UTC) if stage == "cutover_complete" else None,
            },
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_guard_requires_m4_marker_m5_marker_and_descendant(
    seed: M4ThreadSeed,
) -> None:
    async with seed.factory() as session:
        guard = AutomationCutoverGuard.for_session(
            session,
            request_id="req",
        )
        await _set_private_marker(seed, "cutover_complete")
        await _set_automation_marker(seed, "migration_ready")
        with pytest.raises(AutomationCutover):
            await guard.require_project_open()

        await _set_automation_marker(seed, "cutover_complete")
        await guard.require_project_open()

        await _set_private_marker(seed, "migration_ready")
        with pytest.raises(AutomationCutover):
            await guard.require_project_open()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_guard_requires_final_revision_and_accepts_descendant(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='0012_project_automation_expand'"))
    async with seed.factory() as session:
        with pytest.raises(AutomationCutover):
            await AutomationCutoverGuard.for_session(
                session,
                request_id="pre-final",
            ).require_project_open()

    revisions = RevisionAncestry(
        MappingProxyType(
            {
                "0014_future": frozenset(
                    {
                        "0014_future",
                        "0013_project_automation_finalize",
                    }
                )
            }
        )
    )
    async with seed.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='0014_future'"))
    async with seed.factory() as session:
        await AutomationCutoverGuard.for_session(
            session,
            request_id="descendant",
            revisions=revisions,
        ).require_project_open()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_guard_closes_only_after_m5_marker_completion(
    seed: M4ThreadSeed,
) -> None:
    await _set_automation_marker(seed, "migration_ready")
    async with seed.factory() as session:
        guard = AutomationCutoverGuard.for_session(session, request_id="legacy")
        await guard.require_legacy_open()

    await _set_automation_marker(seed, "cutover_complete")
    async with seed.factory() as session:
        with pytest.raises(AutomationCutover) as captured:
            await AutomationCutoverGuard.for_session(
                session,
                request_id="legacy",
            ).require_legacy_open()
    assert captured.value.request_id == "legacy"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_legacy_mutation_guard_freezes_at_expand_and_closes_at_cutover(
    seed: M4ThreadSeed,
) -> None:
    await _set_automation_marker(seed, "migration_ready")
    async with seed.factory() as session:
        with pytest.raises(AutomationMigrationRequired) as expanded:
            await AutomationCutoverGuard.for_session(
                session,
                request_id="legacy-mutation",
            ).require_legacy_mutation_open()
    assert expanded.value.request_id == "legacy-mutation"

    await _set_automation_marker(seed, "cutover_complete")
    async with seed.factory() as session:
        with pytest.raises(AutomationCutover):
            await AutomationCutoverGuard.for_session(
                session,
                request_id="legacy-mutation",
            ).require_legacy_mutation_open()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_missing_expand_marker_row_still_freezes_legacy_mutation(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("DELETE FROM automation_cutover_state WHERE id=1"))

    async with seed.factory() as session:
        await AutomationCutoverGuard.for_session(
            session,
            request_id="legacy-read",
        ).require_legacy_open()
        with pytest.raises(AutomationMigrationRequired):
            await AutomationCutoverGuard.for_session(
                session,
                request_id="legacy-write",
            ).require_legacy_mutation_open()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_missing_m5_marker_is_legacy_open_and_project_closed(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("DELETE FROM automation_cutover_state WHERE id=1"))

    async with seed.factory() as session:
        guard = AutomationCutoverGuard.for_session(session, request_id="missing")
        await guard.require_legacy_open()
        with pytest.raises(AutomationCutover):
            await guard.require_project_open()


@pytest.mark.asyncio
async def test_cutover_guard_maps_database_errors_without_leaking_details() -> None:
    class UnavailableSession:
        async def scalar(self, *_args, **_kwargs):
            raise SQLAlchemyError("postgresql://secret@database/private")

    guard = AutomationCutoverGuard.for_session(
        UnavailableSession(),  # type: ignore[arg-type]
        request_id="safe-request",
    )
    with pytest.raises(AutomationUnavailable) as captured:
        await guard.require_project_open()

    assert captured.value.request_id == "safe-request"
    assert "secret" not in str(captured.value)
