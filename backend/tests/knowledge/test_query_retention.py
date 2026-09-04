"""PostgreSQL gates for owner-private Knowledge Query retention."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from actweave_knowledge import (
    KnowledgeSettings,
    create_knowledge_project_purger,
)
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeQueryRow,
)
from extraction_test_helpers import make_test_quota_port
from registry_helpers import seed_registry_models
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.knowledge.composition import is_knowledge_project_pending_deletion
from app.private_work.retention_purge import (
    RetentionCandidate,
    RetentionPurgeRepository,
)


async def _seed_project_queries(
    session,  # noqa: ANN001
    *,
    label: str,
    embedding_model_id: uuid.UUID,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    project_id = uuid.uuid4()
    target_owner_id = uuid.uuid4()
    other_owner_id = uuid.uuid4()
    target_membership_id = uuid.uuid4()
    base_id = uuid.uuid4()
    now = datetime.now(UTC)

    for owner_id, suffix in (
        (target_owner_id, "target"),
        (other_owner_id, "other"),
    ):
        await session.execute(
            text(
                """INSERT INTO users (
                       id, email, username, system_role, created_at,
                       needs_setup, token_version
                   ) VALUES (
                       :owner_id, :email, :username, 'user', :now, false, 1
                   )"""
            ),
            {
                "owner_id": str(owner_id),
                "email": f"query-retention-{label}-{suffix}@example.invalid",
                "username": f"qr_{label}_{suffix}",
                "now": now,
            },
        )
    await session.execute(
        text(
            """INSERT INTO projects (
                   id, slug, display_name, created_by_user_id
               ) VALUES (
                   :project_id, :slug, :display_name, :owner_id
               )"""
        ),
        {
            "project_id": project_id,
            "slug": f"query-retention-{label}",
            "display_name": f"Query retention {label}",
            "owner_id": str(target_owner_id),
        },
    )
    await session.execute(
        text(
            """INSERT INTO project_memberships (
                   id, project_id, user_id, role, status
               ) VALUES
                   (:target_membership_id, :project_id, :target_owner_id,
                    'admin', 'active'),
                   (:other_membership_id, :project_id, :other_owner_id,
                    'editor', 'active')"""
        ),
        {
            "target_membership_id": target_membership_id,
            "other_membership_id": uuid.uuid4(),
            "project_id": project_id,
            "target_owner_id": str(target_owner_id),
            "other_owner_id": str(other_owner_id),
        },
    )
    session.add(
        KnowledgeBaseRow(
            id=base_id,
            project_id=project_id,
            name=f"base-{label}",
            embedding_model_id=embedding_model_id,
        )
    )
    await session.flush()
    session.add_all(
        [
            KnowledgeQueryRow(
                id=uuid.uuid4(),
                project_id=project_id,
                owner_user_id=str(target_owner_id),
                knowledge_base_ids=[str(base_id)],
                query="target private query",
                source="retrieval_test",
            ),
            KnowledgeQueryRow(
                id=uuid.uuid4(),
                project_id=project_id,
                owner_user_id=str(other_owner_id),
                knowledge_base_ids=[str(base_id)],
                query="other private query",
                source="agent",
            ),
        ]
    )
    return project_id, target_owner_id, other_owner_id, target_membership_id


async def _assert_only_other_owner_query_remains(
    factory,  # noqa: ANN001
    *,
    project_id: uuid.UUID,
    other_owner_id: uuid.UUID,
) -> None:
    async with factory() as session:
        queries = (await session.execute(select(KnowledgeQueryRow.owner_user_id, KnowledgeQueryRow.query).where(KnowledgeQueryRow.project_id == project_id).order_by(KnowledgeQueryRow.query))).all()
        base_count = await session.scalar(select(func.count()).select_from(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
    assert queries == [(str(other_owner_id), "other private query")]
    assert int(base_count or 0) == 1


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_former_owner_phase_b_deletes_only_that_owners_query_history(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        retention_until = datetime.now(UTC)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id, target_owner_id, other_owner_id, membership_id = await _seed_project_queries(
                session,
                label=uuid.uuid4().hex[:8],
                embedding_model_id=embedding_model_id,
            )
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='left', ended_at=:retention_until,
                              end_reason='left', retention_until=:retention_until
                        WHERE id=:membership_id"""
                ),
                {
                    "membership_id": membership_id,
                    "retention_until": retention_until,
                },
            )

        candidate = RetentionCandidate.former_owner(
            project_id=project_id,
            owner_user_id=str(target_owner_id),
            membership_id=membership_id,
            activation_generation=1,
            retention_until=retention_until,
            idempotency_key=f"former-owner-query-retention:{project_id}",
            request_id="former-owner-query-retention",
        )
        async with factory() as session, session.begin():
            await RetentionPurgeRepository().physically_purge(
                session,
                candidate,
                quota=SimpleNamespace(),  # type: ignore[arg-type]
                approval_audit=SimpleNamespace(),  # type: ignore[arg-type]
            )

        await _assert_only_other_owner_query_remains(
            factory,
            project_id=project_id,
            other_owner_id=other_owner_id,
        )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_account_phase_b_deletes_only_that_owners_query_history(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        retention_until = datetime.now(UTC)
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id, target_owner_id, other_owner_id, _ = await _seed_project_queries(
                session,
                label=uuid.uuid4().hex[:8],
                embedding_model_id=embedding_model_id,
            )
            await session.execute(
                text(
                    """UPDATE users
                          SET private_retention_state='pending_deletion',
                              private_retention_generation=2,
                              private_retention_effective_at=:retention_until
                        WHERE id=:owner_user_id"""
                ),
                {
                    "owner_user_id": str(target_owner_id),
                    "retention_until": retention_until,
                },
            )

        candidate = RetentionCandidate.account(
            owner_user_id=str(target_owner_id),
            project_ids=(project_id,),
            account_private_generation=2,
            retention_until=retention_until,
            idempotency_key=f"account-query-retention:{target_owner_id}",
            request_id="account-query-retention",
        )
        async with factory() as session, session.begin():
            await RetentionPurgeRepository().physically_purge(
                session,
                candidate,
                quota=SimpleNamespace(),  # type: ignore[arg-type]
                approval_audit=SimpleNamespace(),  # type: ignore[arg-type]
            )

        await _assert_only_other_owner_query_remains(
            factory,
            project_id=project_id,
            other_owner_id=other_owner_id,
        )
    finally:
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_project_knowledge_purge_deletes_every_owners_query_history(
    postgres_database_url: str,
) -> None:
    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        embedding_model_id, _ = await seed_registry_models(factory)
        async with factory() as session, session.begin():
            project_id, _, _, _ = await _seed_project_queries(
                session,
                label=uuid.uuid4().hex[:8],
                embedding_model_id=embedding_model_id,
            )
            await session.execute(
                text("UPDATE projects SET status = 'pending_deletion' WHERE id = :project_id"),
                {"project_id": project_id},
            )

        purger = create_knowledge_project_purger(
            quota=make_test_quota_port(factory),
            settings=KnowledgeSettings(),
            session_factory=factory,
            project_cleanup_check=is_knowledge_project_pending_deletion,
        )
        assert await purger.purge_project(project_id) is True

        async with factory() as session:
            query_count = await session.scalar(select(func.count()).select_from(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id))
            base_count = await session.scalar(select(func.count()).select_from(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
        assert int(query_count or 0) == 0
        assert int(base_count or 0) == 0
    finally:
        await engine.dispose()
