from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.auth.invitation_rate_limit import (
    InvitationRateLimitRepository,
    hash_rate_limit_key,
)
from app.projects.errors import ProjectDatabaseUnavailable
from deerflow.persistence.projects.invitation_rate_limit_model import (
    ProjectInvitationRateLimitRow,
)

pytestmark = pytest.mark.postgres
NOW = datetime(2026, 7, 12, 9, 0, tzinfo=UTC)


def test_rate_limit_key_is_irreversible_sha256() -> None:
    raw = "claim\x00192.0.2.10\x00member@example.com"
    digest = hash_rate_limit_key(raw)
    assert digest == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in digest
    assert len(digest) == 64


@pytest.mark.asyncio
async def test_cleanup_database_error_is_stable_and_does_not_leak_details() -> None:
    session = MagicMock()

    async def execute(statement):
        if getattr(statement, "is_delete", False):
            raise DBAPIError(
                "DELETE FROM project_invitation_rate_limits WHERE secret=:url",
                {"url": "postgresql://owner:password@db/deerflow"},
                Exception("cleanup failed"),
                False,
            )
        return SimpleNamespace(
            one=lambda: SimpleNamespace(failure_count=1),
        )

    session.execute = AsyncMock(side_effect=execute)
    session.begin.return_value.__aenter__ = AsyncMock()
    session.begin.return_value.__aexit__ = AsyncMock(return_value=False)

    with pytest.raises(ProjectDatabaseUnavailable) as exc_info:
        await InvitationRateLimitRepository(session).admit_attempt(
            hash_rate_limit_key("claim\x00192.0.2.30"),
            NOW,
        )

    assert str(exc_info.value) == "Project storage unavailable"
    assert "DELETE" not in str(exc_info.value)
    assert "postgresql" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_attempt_writes_are_atomic_across_sessions(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key_hash = hash_rate_limit_key("claim\x00192.0.2.10")

    async def admit() -> None:
        async with factory() as session:
            await InvitationRateLimitRepository(session).admit_attempt(key_hash, NOW)

    try:
        await asyncio.gather(*(admit() for _ in range(12)))
        async with factory() as session:
            row = await session.scalar(select(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.key_hash == key_hash))
            assert row is not None
            assert row.failure_count == 12
            assert row.window_started_at == NOW
            assert row.expires_at == NOW + timedelta(minutes=5)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_admission_allows_only_first_five_attempts(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key_hash = hash_rate_limit_key("claim\x00192.0.2.11")

    async def admit() -> bool:
        async with factory() as session:
            return await InvitationRateLimitRepository(session).admit_attempt(key_hash, NOW)

    try:
        admissions = await asyncio.gather(*(admit() for _ in range(12)))
        assert sum(admissions) == 5
        async with factory() as session:
            row = await session.get(ProjectInvitationRateLimitRow, key_hash)
            assert row is not None
            assert row.failure_count == 12
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_expired_window_restarts_and_success_clears_counter(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key_hash = hash_rate_limit_key("redeem\x00192.0.2.10\x00member@example.com")
    try:
        async with factory() as session:
            repository = InvitationRateLimitRepository(session)
            for _ in range(5):
                assert await repository.admit_attempt(key_hash, NOW)
            assert not await repository.admit_attempt(key_hash, NOW + timedelta(minutes=4))
            assert await repository.admit_attempt(key_hash, NOW + timedelta(minutes=5))

        async with factory() as session:
            row = await session.get(ProjectInvitationRateLimitRow, key_hash)
            assert row is not None
            assert row.failure_count == 1
        async with factory() as session:
            await InvitationRateLimitRepository(session).clear(key_hash)

        async with factory() as session:
            assert await session.get(ProjectInvitationRateLimitRow, key_hash) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_admission_opportunistically_cleans_expired_rows_in_bounded_batches(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    current_key = hash_rate_limit_key("claim\x00192.0.2.20")
    live_key = hash_rate_limit_key("claim\x00192.0.2.21")
    expired_rows = [
        ProjectInvitationRateLimitRow(
            key_hash=hash_rate_limit_key(f"expired\x00{index}"),
            failure_count=1,
            window_started_at=NOW - timedelta(minutes=20),
            expires_at=NOW - timedelta(minutes=10) + timedelta(seconds=index),
            updated_at=NOW - timedelta(minutes=10),
        )
        for index in range(205)
    ]
    try:
        async with factory.begin() as session:
            session.add_all(
                [
                    *expired_rows,
                    ProjectInvitationRateLimitRow(
                        key_hash=current_key,
                        failure_count=2,
                        window_started_at=NOW - timedelta(minutes=1),
                        expires_at=NOW + timedelta(minutes=4),
                        updated_at=NOW - timedelta(minutes=1),
                    ),
                    ProjectInvitationRateLimitRow(
                        key_hash=live_key,
                        failure_count=4,
                        window_started_at=NOW - timedelta(minutes=1),
                        expires_at=NOW + timedelta(minutes=4),
                        updated_at=NOW - timedelta(minutes=1),
                    ),
                ]
            )

        async with factory() as session:
            repository = InvitationRateLimitRepository(session)
            assert await repository.admit_attempt(current_key, NOW)

        async with factory() as session:
            expired_after_first = await session.scalar(select(func.count()).select_from(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.expires_at <= NOW))
            current = await session.get(ProjectInvitationRateLimitRow, current_key)
            unrelated_live = await session.get(ProjectInvitationRateLimitRow, live_key)
            assert expired_after_first == 105
            assert current is not None and current.failure_count == 3
            assert unrelated_live is not None and unrelated_live.failure_count == 4

        async with factory() as session:
            assert await InvitationRateLimitRepository(session).admit_attempt(
                current_key,
                NOW,
            )

        async with factory() as session:
            expired_after_second = await session.scalar(select(func.count()).select_from(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.expires_at <= NOW))
            current = await session.get(ProjectInvitationRateLimitRow, current_key)
            unrelated_live = await session.get(ProjectInvitationRateLimitRow, live_key)
            assert expired_after_second == 5
            assert current is not None and current.failure_count == 4
            assert unrelated_live is not None and unrelated_live.failure_count == 4
    finally:
        await engine.dispose()
