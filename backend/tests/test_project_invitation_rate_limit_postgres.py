from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.auth.invitation_rate_limit import (
    InvitationRateLimitRepository,
    hash_rate_limit_key,
)
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
async def test_failure_writes_are_atomic_across_sessions(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key_hash = hash_rate_limit_key("claim\x00192.0.2.10")

    async def record() -> None:
        async with factory() as session:
            await InvitationRateLimitRepository(session).record_failure(key_hash, NOW)

    try:
        await asyncio.gather(*(record() for _ in range(12)))
        async with factory() as session:
            row = await session.scalar(select(ProjectInvitationRateLimitRow).where(ProjectInvitationRateLimitRow.key_hash == key_hash))
            assert row is not None
            assert row.failure_count == 12
            assert row.window_started_at == NOW
            assert row.expires_at == NOW + timedelta(minutes=5)
        async with factory() as session:
            assert await InvitationRateLimitRepository(session).is_limited(key_hash, NOW)
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
                await repository.record_failure(key_hash, NOW)
            assert await repository.is_limited(key_hash, NOW + timedelta(minutes=4))
            assert not await repository.is_limited(key_hash, NOW + timedelta(minutes=5))
            await repository.record_failure(key_hash, NOW + timedelta(minutes=6))

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
