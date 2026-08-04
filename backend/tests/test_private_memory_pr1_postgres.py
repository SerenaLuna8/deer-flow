from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.models import CreateProject
from app.projects.repository import ProjectRepository
from deerflow.agents.memory.storage import (
    ProjectMemorySnapshot,
    ProjectMemoryStorage,
    create_empty_memory,
)
from deerflow.persistence.private_work.memory_repository import (
    PrivateMemoryVersionConflict,
)
from deerflow.private_scope import PrivateResourceScope


@pytest.mark.asyncio
async def test_missing_read_and_atomic_first_write(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    owner_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users
                    (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {
                    "id": str(owner_id),
                    "email": "memory-pr1@example.com",
                    "now": datetime.now(UTC),
                },
            )
        async with session_factory() as session:
            context = await ProjectRepository(session).create_with_admin(
                owner_id,
                CreateProject("memory-pr1", "Memory PR1"),
                "memory-pr1",
            )
        scope = PrivateResourceScope(
            project_id=str(context.project_id),
            owner_user_id=str(owner_id),
            membership_version=context.membership_version,
        )
        storage = ProjectMemoryStorage(session_factory)

        missing = await storage.load(scope=scope, namespace="agent:lead")
        async with engine.connect() as connection:
            row_count = (
                await connection.execute(
                    text(
                        """SELECT count(*) FROM user_project_memories
                        WHERE project_id=:project_id
                          AND owner_user_id=:owner_user_id
                          AND namespace='agent:lead'"""
                    ),
                    {
                        "project_id": context.project_id,
                        "owner_user_id": str(owner_id),
                    },
                )
            ).scalar_one()
        assert missing.version == 0
        assert missing.memory["lastUpdated"] == ""
        assert row_count == 0

        imported = create_empty_memory()
        imported["lastUpdated"] = "1999-01-01T00:00:00Z"
        results = await asyncio.gather(
            storage.save(
                imported,
                scope=scope,
                namespace="agent:lead",
                expected_version=0,
            ),
            storage.save(
                imported,
                scope=scope,
                namespace="agent:lead",
                expected_version=0,
            ),
            return_exceptions=True,
        )

        successes = [result for result in results if isinstance(result, ProjectMemorySnapshot)]
        conflicts = [result for result in results if isinstance(result, PrivateMemoryVersionConflict)]
        assert len(successes) == 1
        assert len(conflicts) == 1
        assert successes[0].version == 1
        assert successes[0].memory["lastUpdated"] != imported["lastUpdated"]

        async with engine.connect() as connection:
            row_count = (
                await connection.execute(
                    text(
                        """SELECT count(*) FROM user_project_memories
                        WHERE project_id=:project_id
                          AND owner_user_id=:owner_user_id
                          AND namespace='agent:lead'"""
                    ),
                    {
                        "project_id": context.project_id,
                        "owner_user_id": str(owner_id),
                    },
                )
            ).scalar_one()
        assert row_count == 1
    finally:
        await engine.dispose()
