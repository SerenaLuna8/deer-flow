from __future__ import annotations

import re
import uuid
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.thread_service import PrivateThreadService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.bootstrap import M7_FINAL_SCHEMA_REVISION
from deerflow.runtime.checkpointer.async_provider import make_checkpointer
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

_TEST_DATABASE_PATTERN = re.compile(r"deerflow_test_[0-9]+_[0-9a-f]{32}\Z")
PRIVATE_PERSISTENCE_TABLES = (
    "threads_meta",
    "runs",
    "run_events",
    "feedback",
    "run_asset_versions",
    "run_mcp_grant_snapshots",
    "files",
    "file_chunks",
    "artifacts",
    "user_project_memories",
    "user_project_memory_facts",
    "channel_connections",
    "channel_credentials",
    "channel_oauth_states",
    "channel_conversations",
)
LANGGRAPH_CHECKPOINT_TABLES = (
    "checkpoints",
    "checkpoint_blobs",
    "checkpoint_writes",
)


async def dump_table_bytes(connection: Any, table: str) -> bytes:
    allowed = set(PRIVATE_PERSISTENCE_TABLES + LANGGRAPH_CHECKPOINT_TABLES)
    if table not in allowed:
        raise ValueError("unsupported release-gate table")
    assert await connection.scalar(text("SELECT to_regclass(:table)"), {"table": table}) is not None
    rows = (await connection.execute(text(f'SELECT to_jsonb(row)::text FROM "{table}" AS row'))).scalars()
    return "\n".join(str(row) for row in rows).encode("utf-8")


@dataclass(frozen=True)
class M4ReleaseScenario:
    seed: M4ThreadSeed
    raw_checkpointer: Any
    project_checkpointer: ProjectScopedCheckpointer
    thread_service: PrivateThreadService
    outsider: PrivateWorkContext
    _stack: AsyncExitStack

    @classmethod
    async def create(cls, database_url: str) -> M4ReleaseScenario:
        seed = await seed_m4_thread_database(database_url)
        stack = AsyncExitStack()
        provider_config = SimpleNamespace(
            database=SimpleNamespace(
                checkpointer_url=database_url.replace(
                    "postgresql+asyncpg://",
                    "postgresql://",
                )
            )
        )
        raw_checkpointer = await stack.enter_async_context(make_checkpointer(provider_config))
        project_checkpointer = ProjectScopedCheckpointer(
            raw_checkpointer,
            seed.factory,
        )
        return cls(
            seed=seed,
            raw_checkpointer=raw_checkpointer,
            project_checkpointer=project_checkpointer,
            thread_service=PrivateThreadService(
                seed.factory,
                project_checkpointer,
            ),
            outsider=PrivateWorkContext.from_project(
                ProjectContext(
                    user_id=uuid.uuid4(),
                    project_id=seed.owner_a.project_id,
                    membership_id=uuid.uuid4(),
                    role=ProjectRole.RUNNER,
                    capabilities=capabilities_for(ProjectRole.RUNNER),
                    membership_version=1,
                    request_id="req-outsider",
                )
            ),
            _stack=stack,
        )

    async def close(self) -> None:
        await self._stack.aclose()
        await self.seed.engine.dispose()


async def m4_release_database_ready(database_url: str) -> bool:
    """Return whether the isolated database is ready for the M4 release gate."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            database = str(await connection.scalar(text("SELECT current_database()")))
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
        return _TEST_DATABASE_PATTERN.fullmatch(database) is not None and revision == M7_FINAL_SCHEMA_REVISION
    finally:
        await engine.dispose()
