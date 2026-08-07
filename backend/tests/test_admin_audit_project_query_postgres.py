"""Platform audit listing can filter by project name or slug."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from deerflow.persistence.audit.model import AuditLogRow
from deerflow.persistence.audit.sql import AuditRepository
from deerflow.persistence.projects.model import ProjectRow
from deerflow.persistence.user import UserRow


async def _seed(factory: async_sessionmaker) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    alpha_id = uuid.uuid4()
    beta_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            UserRow(
                id=str(user_id),
                email=f"{user_id}@example.com",
                password_hash=None,
                system_role="system_admin",
                needs_setup=False,
                token_version=0,
            )
        )
        await session.flush()
        session.add_all(
            [
                ProjectRow(
                    id=alpha_id,
                    slug="alpha-lab",
                    display_name="Alpha Lab",
                    created_by_user_id=str(user_id),
                ),
                ProjectRow(
                    id=beta_id,
                    slug="beta-workspace",
                    display_name="Beta Workspace",
                    created_by_user_id=str(user_id),
                ),
            ]
        )
        await session.flush()
        now = datetime.now(UTC)
        session.add_all(
            [
                AuditLogRow(
                    id=uuid.uuid4(),
                    occurred_at=now,
                    actor_process="gateway",
                    project_id=alpha_id,
                    action="project.created",
                    target_kind="project",
                    target_ref_key_id="unit",
                    target_ref_hmac="a" * 64,
                    outcome="success",
                    metadata_json={},
                ),
                AuditLogRow(
                    id=uuid.uuid4(),
                    occurred_at=now,
                    actor_process="gateway",
                    project_id=beta_id,
                    action="project.updated",
                    target_kind="project",
                    target_ref_key_id="unit",
                    target_ref_hmac="b" * 64,
                    outcome="success",
                    metadata_json={},
                ),
                AuditLogRow(
                    id=uuid.uuid4(),
                    occurred_at=now,
                    actor_process="gateway",
                    project_id=None,
                    action="system_setting.updated",
                    target_kind="system_setting",
                    target_ref_key_id="unit",
                    target_ref_hmac="c" * 64,
                    outcome="success",
                    metadata_json={
                        "section": "agent_runtime",
                        "revision": 1,
                        "schema_version": 1,
                        "payload_checksum": "d" * 64,
                        "effect_scope": "new_requests",
                    },
                ),
            ]
        )
        await session.commit()
    return alpha_id, beta_id


@pytest.mark.asyncio
async def test_list_platform_filters_by_project_query(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        alpha_id, _beta_id = await _seed(factory)
        async with factory() as session:
            repo = AuditRepository(session)
            matched = await repo.list_platform(limit=20, project_query="alpha")
            assert [row.project_id for row in matched] == [alpha_id]

            by_slug = await repo.list_platform(limit=20, project_query="beta-work")
            assert len(by_slug) == 1
            assert by_slug[0].project_id is not None

            unfiltered = await repo.list_platform(limit=20)
            assert len(unfiltered) == 3

            platform_only = await repo.list_platform(limit=20, platform_only=True)
            assert len(platform_only) == 1
            assert platform_only[0].project_id is None
            assert platform_only[0].action == "system_setting.updated"
    finally:
        await engine.dispose()
