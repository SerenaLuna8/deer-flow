from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.errors import AutomationCutover, AutomationUnavailable
from app.automations.readiness import (
    AUTOMATION_READY,
    AutomationReadinessService,
)


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_reports_scheduler_disabled_without_closing_project_api(
    seed: M4ThreadSeed,
) -> None:
    async with seed.factory() as session:
        result = await AutomationReadinessService().read(
            session,
            seed.owner_a,
            scheduler_enabled=False,
        )

    assert result.status == "ready"
    assert result.code == AUTOMATION_READY
    assert result.scheduler_enabled is False
    assert result.scheduler_status == "disabled"
    assert result.project_private_work_ready is True
    assert result.automation_cutover_ready is True
    assert result.request_id == seed.owner_a.request_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_reports_incomplete_automation_marker_without_writing(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        before = await connection.execute(
            text(
                """UPDATE automation_cutover_state
                SET stage='empty_install',migration_run_id=NULL,
                    empty_domain_probe_complete=true,
                    final_schema_probe_complete=false,cutover_at=NULL
                WHERE id=1
                RETURNING stage,updated_at"""
            )
        )
        before_row = before.one()

    async with seed.factory() as session:
        result = await AutomationReadinessService().read(
            session,
            seed.owner_a,
            scheduler_enabled=True,
        )

    async with seed.engine.connect() as connection:
        after_row = (
            await connection.execute(
                text(
                    """SELECT stage,updated_at
                    FROM automation_cutover_state WHERE id=1"""
                )
            )
        ).one()

    assert result.status == "migration_required"
    assert result.code == AutomationCutover.code
    assert result.scheduler_enabled is True
    assert result.scheduler_status == "stopped"
    assert result.project_private_work_ready is True
    assert result.automation_cutover_ready is False
    assert after_row == before_row


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_requires_m4_marker_even_when_m5_is_complete(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(
            text(
                """UPDATE private_work_cutover_state
                SET stage='migration_ready',cutover_at=NULL WHERE id=1"""
            )
        )

    async with seed.factory() as session:
        result = await AutomationReadinessService().read(
            session,
            seed.owner_a,
            scheduler_enabled=True,
        )

    assert result.status == "migration_required"
    assert result.code == AutomationCutover.code
    assert result.project_private_work_ready is False
    assert result.automation_cutover_ready is False


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_reports_final_revision_gap_separately_from_m4(
    seed: M4ThreadSeed,
) -> None:
    async with seed.engine.begin() as connection:
        await connection.execute(text("UPDATE alembic_version SET version_num='0012_project_automation_expand'"))

    async with seed.factory() as session:
        result = await AutomationReadinessService().read(
            session,
            seed.owner_a,
            scheduler_enabled=True,
        )

    assert result.status == "migration_required"
    assert result.code == AutomationCutover.code
    assert result.project_private_work_ready is True
    assert result.automation_cutover_ready is False


@pytest.mark.asyncio
async def test_readiness_reports_database_unavailable_without_exception_text() -> None:
    class UnavailableSession:
        async def scalar(self, *_args, **_kwargs):
            raise SQLAlchemyError("postgresql://secret@database/private")

    context = type("Context", (), {"request_id": "safe-request"})()
    result = await AutomationReadinessService().read(
        UnavailableSession(),  # type: ignore[arg-type]
        context,  # type: ignore[arg-type]
        scheduler_enabled=True,
    )

    assert result.status == "unavailable"
    assert result.code == AutomationUnavailable.code
    assert result.request_id == "safe-request"
    assert result.scheduler_enabled is True
    assert result.project_private_work_ready is False
    assert result.automation_cutover_ready is False
    assert "secret" not in repr(result)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_readiness_reports_lost_scheduler_without_closing_project_api(
    seed: M4ThreadSeed,
) -> None:
    async with seed.factory() as session:
        result = await AutomationReadinessService(scheduler_status_provider=lambda: "ownership_lost").read(
            session,
            seed.owner_a,
            scheduler_enabled=True,
        )

    assert result.status == "ready"
    assert result.code == AUTOMATION_READY
    assert result.scheduler_enabled is True
    assert result.scheduler_status == "ownership_lost"
    assert result.project_private_work_ready is True
    assert result.automation_cutover_ready is True
