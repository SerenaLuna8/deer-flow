from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from app.private_work.errors import PrivateWorkConflict
from app.private_work.thread_repository import PrivateThreadRepository
from deerflow.runtime.private_scope import PrivateResourceScope


class _Session:
    def __init__(self) -> None:
        self.executed: list[object] = []

    async def execute(self, statement):
        self.executed.append(statement)


@pytest.mark.asyncio
async def test_touch_activity_is_scoped_monotonic_and_does_not_bump_version() -> None:
    session = _Session()
    scope = PrivateResourceScope(
        project_id=str(uuid.UUID("11111111-1111-4111-8111-111111111111")),
        owner_user_id=str(uuid.UUID("22222222-2222-4222-8222-222222222222")),
        membership_version=1,
    )
    occurred_at = datetime(2026, 8, 6, 3, 30, tzinfo=UTC)

    await PrivateThreadRepository(session).touch_activity(  # type: ignore[arg-type]
        scope=scope,
        thread_id="thread-active",
        occurred_at=occurred_at,
    )

    compiled = session.executed[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    update_clause, where_clause = sql.split(" WHERE ", maxsplit=1)
    assert "UPDATE threads_meta SET updated_at=" in update_clause
    assert "version" not in update_clause
    assert "threads_meta.thread_id" in where_clause
    assert "threads_meta.project_id" in where_clause
    assert "threads_meta.owner_user_id" in where_clause
    assert "threads_meta.deleted_at IS NULL" in where_clause
    assert "threads_meta.frozen_at IS NULL" in where_clause
    assert "threads_meta.updated_at < now()" in where_clause
    assert occurred_at in compiled.params.values()


@pytest.mark.asyncio
async def test_touch_activity_rejects_naive_timestamps() -> None:
    session = _Session()
    scope = PrivateResourceScope(
        project_id=str(uuid.UUID("11111111-1111-4111-8111-111111111111")),
        owner_user_id=str(uuid.UUID("22222222-2222-4222-8222-222222222222")),
        membership_version=1,
    )

    with pytest.raises(PrivateWorkConflict):
        await PrivateThreadRepository(session).touch_activity(  # type: ignore[arg-type]
            scope=scope,
            thread_id="thread-active",
            occurred_at=datetime(2026, 8, 6, 3, 30),
        )

    assert session.executed == []
