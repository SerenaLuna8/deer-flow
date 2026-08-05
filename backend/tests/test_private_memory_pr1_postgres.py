from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.projects.models import CreateProject
from app.projects.repository import ProjectRepository
from deerflow.agents.memory.storage import (
    ProjectMemoryStorage,
    create_empty_memory,
)
from deerflow.private_scope import PrivateResourceScope


@pytest.mark.asyncio
async def test_load_is_non_mutating_and_reads_manually_seeded_memory(
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

        memory_id = uuid.uuid4()
        fact_id = uuid.uuid4()
        aggregate_updated_at = datetime.now(UTC)
        fact_created_at = datetime.now(UTC)
        context_summary = create_empty_memory(last_updated="ignored-on-read")
        context_summary["user"]["workContext"] = {
            "summary": "负责 ActWeave 项目",
            "updatedAt": "2026-08-05T00:00:00Z",
        }
        context_summary.pop("facts")

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO user_project_memories
                    (id,project_id,owner_user_id,namespace,context_summary,version,created_at,updated_at)
                    VALUES
                    (:id,:project_id,:owner_user_id,:namespace,CAST(:context_summary AS jsonb),7,:created_at,:updated_at)"""
                ),
                {
                    "id": memory_id,
                    "project_id": context.project_id,
                    "owner_user_id": str(owner_id),
                    "namespace": "agent:lead",
                    "context_summary": json.dumps(context_summary, ensure_ascii=False),
                    "created_at": aggregate_updated_at,
                    "updated_at": aggregate_updated_at,
                },
            )
            await connection.execute(
                text(
                    """INSERT INTO user_project_memory_facts
                    (id,project_id,owner_user_id,memory_id,content,category,confidence,created_at,updated_at)
                    VALUES
                    (:id,:project_id,:owner_user_id,:memory_id,:content,:category,:confidence,:created_at,:updated_at)"""
                ),
                {
                    "id": fact_id,
                    "project_id": context.project_id,
                    "owner_user_id": str(owner_id),
                    "memory_id": memory_id,
                    "content": "用户偏好中文交流",
                    "category": "preference",
                    "confidence": 0.95,
                    "created_at": fact_created_at,
                    "updated_at": fact_created_at,
                },
            )

        loaded = await storage.load(scope=scope, namespace="agent:lead")
        assert loaded.version == 7
        assert loaded.memory["lastUpdated"] == aggregate_updated_at.isoformat().removesuffix("+00:00") + "Z"
        assert loaded.memory["user"]["workContext"]["summary"] == "负责 ActWeave 项目"
        assert loaded.memory["facts"] == [
            {
                "id": str(fact_id),
                "content": "用户偏好中文交流",
                "category": "preference",
                "confidence": 0.95,
                "createdAt": fact_created_at.isoformat().removesuffix("+00:00") + "Z",
                "source": "manual",
            }
        ]

        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        """SELECT
                            (SELECT count(*) FROM user_project_memories
                             WHERE project_id=:project_id
                               AND owner_user_id=:owner_user_id
                               AND namespace='agent:lead') AS memories,
                            (SELECT count(*) FROM user_project_memory_facts
                             WHERE project_id=:project_id
                               AND owner_user_id=:owner_user_id
                               AND memory_id=:memory_id) AS facts"""
                    ),
                    {
                        "project_id": context.project_id,
                        "owner_user_id": str(owner_id),
                        "memory_id": memory_id,
                    },
                )
            ).one()
        assert counts.memories == 1
        assert counts.facts == 1
    finally:
        await engine.dispose()
