from __future__ import annotations

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine

from app.private_work.legacy_run_skill_snapshot_writer import (
    frozen_run_skill_snapshot_writer,
)
from app.private_work.run_skill_writer_cohort import RunSkillWriterCohortLease


def install_mock_cohort_assertion(monkeypatch) -> None:
    """Replace only the repository import seam for fake-session unit tests."""

    import app.private_work.snapshot_repository as snapshot_module

    async def assert_test_cohort(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(
        snapshot_module,
        "require_active_run_skill_writer_cohort",
        assert_test_cohort,
    )


@asynccontextmanager
async def active_test_run_skill_writer_cohort(engine: AsyncEngine):
    """Hold the same real PostgreSQL process authority used by production."""

    lease = await start_test_run_skill_writer_cohort(engine)
    try:
        yield lease
    finally:
        await lease.close()


async def start_test_run_skill_writer_cohort(
    engine: AsyncEngine,
) -> RunSkillWriterCohortLease:
    return await RunSkillWriterCohortLease.acquire(
        engine,
        frozen_run_skill_snapshot_writer(),
        process_role="gateway",
        process_authority=True,
    )
