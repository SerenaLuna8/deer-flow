from __future__ import annotations

import inspect

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.engine import close_engine
from deerflow.persistence.scheduled_task_runs import ScheduledTaskRunRepository
from deerflow.persistence.scheduled_tasks import ScheduledTaskRepository


@pytest_asyncio.fixture()
async def _postgres_database(migrated_postgres_database_url):
    try:
        yield
    finally:
        await close_engine()


def test_scheduled_task_repositories_are_session_bound() -> None:
    task_parameters = tuple(inspect.signature(ScheduledTaskRepository.__init__).parameters)
    occurrence_parameters = tuple(inspect.signature(ScheduledTaskRunRepository.__init__).parameters)
    assert task_parameters == ("self", "session")
    assert occurrence_parameters == ("self", "session")
    assert inspect.signature(ScheduledTaskRepository.__init__).parameters["session"].annotation in {AsyncSession, "AsyncSession"}


def test_all_public_repository_methods_start_with_scope() -> None:
    for repository, methods in (
        (
            ScheduledTaskRepository,
            ("create", "get", "list", "list_by_thread", "lock_active", "update", "soft_delete"),
        ),
        (
            ScheduledTaskRunRepository,
            ("create", "get", "get_by_agent_run_id", "list_by_task", "has_active", "finish", "cancel_queued"),
        ),
    ):
        for method_name in methods:
            parameters = tuple(inspect.signature(getattr(repository, method_name)).parameters)
            assert parameters[:2] == ("self", "scope"), (repository, method_name)
