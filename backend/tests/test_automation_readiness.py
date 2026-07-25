from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.automations.errors import AutomationUnavailable
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
    assert result.schema_ready is True
    assert result.request_id == seed.owner_a.request_id


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
    assert result.schema_ready is False
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
    assert result.schema_ready is True
