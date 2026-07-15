from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelConnectionRow,
    ChannelCredentialCipher,
)


@pytest_asyncio.fixture()
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


@pytest.fixture()
def repository(seed: M4ThreadSeed) -> ChannelConnectionRepository:
    return ChannelConnectionRepository(
        seed.factory,
        cipher=ChannelCredentialCipher.from_key("private-connection-test-key"),
    )


async def _create_thread(
    seed: M4ThreadSeed,
    *,
    scope,
    thread_id: str,
    agent_id: uuid.UUID,
) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(agent_id, "project"),
        )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_same_owner_connections_are_isolated_between_projects(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    project_a = await repository.upsert_connection(
        scope=seed.owner_a_scope,
        provider="slack",
        external_account_id="owner-a-project-a",
        workspace_id="workspace-a",
    )
    project_b = await repository.upsert_connection(
        scope=seed.project_b_owner_a_scope,
        provider="slack",
        external_account_id="owner-a-project-b",
        workspace_id="workspace-b",
    )

    assert [row["id"] for row in await repository.list_connections(seed.owner_a_scope)] == [project_a["id"]]
    assert [row["id"] for row in await repository.list_connections(seed.project_b_owner_a_scope)] == [project_b["id"]]
    assert (
        await repository.disconnect_connection(
            scope=seed.project_b_owner_a_scope,
            connection_id=project_a["id"],
        )
        is False
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_connection_owner_reads_and_credentials_require_exact_scope(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    connection = await repository.upsert_connection(
        scope=seed.owner_a_scope,
        provider="telegram",
        external_account_id="owner-a",
    )
    assert await repository.store_credentials(
        scope=seed.owner_a_scope,
        connection_id=connection["id"],
        access_token="secret",
    )

    assert await repository.list_connections(seed.owner_b_scope) == []
    assert (
        await repository.get_credentials(
            scope=seed.owner_b_scope,
            connection_id=connection["id"],
        )
        is None
    )
    assert (
        await repository.store_credentials(
            scope=seed.owner_b_scope,
            connection_id=connection["id"],
            access_token="replacement",
        )
        is False
    )
    credentials = await repository.get_credentials(
        scope=seed.owner_a_scope,
        connection_id=connection["id"],
    )
    assert credentials is not None
    assert credentials["access_token"] == "secret"


@pytest.mark.postgres
@pytest.mark.anyio
async def test_oauth_state_persists_and_returns_project_scope(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    now = datetime.now(UTC)
    await repository.create_oauth_state(
        scope=seed.owner_a_scope,
        provider="slack",
        state="project-a-state",
        expires_at=now + timedelta(minutes=5),
    )
    await repository.create_oauth_state(
        scope=seed.project_b_owner_a_scope,
        provider="slack",
        state="project-b-state",
        expires_at=now + timedelta(minutes=5),
    )

    assert (
        await repository.count_oauth_states(
            scope=seed.owner_a_scope,
            provider="slack",
            active_only=True,
            now=now,
        )
        == 1
    )
    assert (
        await repository.count_oauth_states(
            scope=seed.project_b_owner_a_scope,
            provider="slack",
            active_only=True,
            now=now,
        )
        == 1
    )

    consumed = await repository.consume_oauth_state(
        provider="slack",
        state="project-b-state",
        now=now,
    )
    assert consumed is not None
    assert consumed["project_id"] == seed.project_b_owner_a_scope.project_id
    assert consumed["owner_user_id"] == seed.project_b_owner_a_scope.owner_user_id


@pytest.mark.postgres
@pytest.mark.anyio
async def test_conversation_mapping_requires_exact_connection_and_thread_scope(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    await _create_thread(
        seed,
        scope=seed.owner_a_scope,
        thread_id="connection-thread-a",
        agent_id=seed.project_agent_id,
    )
    await _create_thread(
        seed,
        scope=seed.owner_b_scope,
        thread_id="connection-thread-b",
        agent_id=seed.project_agent_id,
    )
    connection = await repository.upsert_connection(
        scope=seed.owner_a_scope,
        provider="slack",
        external_account_id="conversation-owner-a",
        workspace_id="workspace-a",
    )

    assert await repository.set_thread_id(
        scope=seed.owner_a_scope,
        connection_id=connection["id"],
        provider="slack",
        external_conversation_id="conversation-1",
        external_topic_id="topic-1",
        thread_id="connection-thread-a",
    )
    assert (
        await repository.get_thread_id(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
            external_conversation_id="conversation-1",
            external_topic_id="topic-1",
        )
        == "connection-thread-a"
    )
    assert (
        await repository.get_thread_id(
            scope=seed.owner_b_scope,
            connection_id=connection["id"],
            external_conversation_id="conversation-1",
            external_topic_id="topic-1",
        )
        is None
    )
    assert (
        await repository.set_thread_id(
            scope=seed.owner_b_scope,
            connection_id=connection["id"],
            provider="slack",
            external_conversation_id="conversation-2",
            thread_id="connection-thread-b",
        )
        is False
    )

    with pytest.raises(IntegrityError):
        await repository.set_thread_id(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
            provider="slack",
            external_conversation_id="conversation-wrong-thread",
            thread_id="connection-thread-b",
        )


class _ExplodingDecryptCipher:
    def decrypt_text(self, value: str | None) -> str | None:
        raise AssertionError("frozen credentials must not be decrypted")


@pytest.mark.postgres
@pytest.mark.anyio
async def test_frozen_connection_credentials_are_not_decrypted(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    connection = await repository.upsert_connection(
        scope=seed.owner_a_scope,
        provider="slack",
        external_account_id="frozen-owner",
        workspace_id="frozen-workspace",
    )
    assert await repository.store_credentials(
        scope=seed.owner_a_scope,
        connection_id=connection["id"],
        access_token="secret",
    )
    async with seed.factory() as session, session.begin():
        await session.execute(update(ChannelConnectionRow).where(ChannelConnectionRow.id == connection["id"]).values(status="frozen", frozen_at=datetime.now(UTC)))

    no_decrypt_repository = ChannelConnectionRepository(
        seed.factory,
        cipher=_ExplodingDecryptCipher(),  # type: ignore[arg-type]
    )
    assert (
        await no_decrypt_repository.get_credentials(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
        )
        is None
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_external_identity_resolves_only_the_active_connection_scope(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    first = await repository.upsert_connection(
        scope=seed.owner_a_scope,
        provider="slack",
        external_account_id="shared-external-owner",
        workspace_id="shared-workspace",
    )
    second = await repository.upsert_connection(
        scope=seed.owner_b_scope,
        provider="slack",
        external_account_id="shared-external-owner",
        workspace_id="shared-workspace",
    )

    resolved = await repository.find_connection_by_external_identity(
        provider="slack",
        external_account_id="shared-external-owner",
        workspace_id="shared-workspace",
    )
    assert resolved is not None
    assert resolved["id"] == second["id"]
    assert resolved["project_id"] == seed.owner_b_scope.project_id
    assert resolved["owner_user_id"] == seed.owner_b_scope.owner_user_id

    async with seed.factory() as session:
        first_row = await session.get(ChannelConnectionRow, first["id"])
        second_row = await session.get(ChannelConnectionRow, second["id"])
        assert first_row is not None and first_row.status == "revoked"
        assert second_row is not None
        second_row.status = "frozen"
        second_row.frozen_at = datetime.now(UTC)
        await session.commit()

    assert (
        await repository.find_connection_by_external_identity(
            provider="slack",
            external_account_id="shared-external-owner",
            workspace_id="shared-workspace",
        )
        is None
    )


@pytest.mark.postgres
@pytest.mark.anyio
async def test_disconnect_is_scoped_and_trusted_provider_disconnect_is_explicit(
    seed: M4ThreadSeed,
    repository: ChannelConnectionRepository,
) -> None:
    first = await repository.upsert_connection(
        scope=seed.owner_a_scope,
        provider="telegram",
        external_account_id="telegram-owner-a",
    )
    second = await repository.upsert_connection(
        scope=seed.owner_b_scope,
        provider="telegram",
        external_account_id="telegram-owner-b",
    )
    assert (
        await repository.disconnect_connection(
            scope=seed.owner_b_scope,
            connection_id=first["id"],
        )
        is False
    )

    assert await repository.trusted_disconnect_provider_connections(provider="telegram") == 2
    async with seed.factory() as session:
        rows = (await session.execute(select(ChannelConnectionRow).where(ChannelConnectionRow.id.in_((first["id"], second["id"]))))).scalars()
        assert {row.status for row in rows} == {"revoked"}


@pytest.mark.postgres
@pytest.mark.anyio
async def test_repository_rejects_non_private_scope(
    repository: ChannelConnectionRepository,
) -> None:
    with pytest.raises(RuntimeError, match="scope"):
        await repository.list_connections(object())  # type: ignore[arg-type]
