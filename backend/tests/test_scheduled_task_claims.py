from __future__ import annotations

import pytest_asyncio

from deerflow.persistence.engine import close_engine
from deerflow.persistence.scheduled_tasks import (
    ScheduledTaskRepository,
    ScheduledTaskRow,
)


@pytest_asyncio.fixture()
async def _postgres_database(migrated_postgres_database_url):
    try:
        yield
    finally:
        await close_engine()


def test_m5_definition_repository_does_not_claim_execution_leases() -> None:
    """Occurrence reservation/claim is introduced by M5 Task 5.

    Definition rows are project-scoped schedule authority, not worker leases.
    """

    assert not hasattr(ScheduledTaskRepository, "claim_due_tasks")


def test_m5_definition_row_has_no_legacy_user_or_lease_authority() -> None:
    columns = set(ScheduledTaskRow.__table__.c.keys())
    assert {
        "user_id",
        "assistant_id",
        "lease_owner",
        "lease_expires_at",
        "last_run_id",
        "last_thread_id",
    }.isdisjoint(columns)
