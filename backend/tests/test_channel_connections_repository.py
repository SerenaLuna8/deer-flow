"""Tests for per-user IM channel connection persistence."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from support.m4_private_threads import M4ThreadSeed, seed_m4_thread_database

from app.channels.store import ChannelStore
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelConnectionRow,
    ChannelCredentialCipher,
    ChannelCredentialRow,
    ChannelOAuthStateRow,
)


@pytest_asyncio.fixture
async def seed(migrated_postgres_database_url: str) -> M4ThreadSeed:
    value = await seed_m4_thread_database(migrated_postgres_database_url)
    try:
        yield value
    finally:
        await value.engine.dispose()


@pytest.fixture
def repo(seed: M4ThreadSeed) -> ChannelConnectionRepository:
    return ChannelConnectionRepository(
        seed.factory,
        cipher=ChannelCredentialCipher.from_key("test-encryption-key"),
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


class TestChannelConnectionRepository:
    @pytest.mark.anyio
    async def test_connections_are_listed_per_owner(self, repo, seed):
        alice = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-alice",
            external_account_name="Alice",
            workspace_id="T1",
            workspace_name="Team One",
            scopes=["chat:write"],
        )
        await repo.upsert_connection(
            scope=seed.owner_b_scope,
            provider="slack",
            external_account_id="U-bob",
            external_account_name="Bob",
            workspace_id="T1",
            workspace_name="Team One",
            scopes=["chat:write"],
        )

        results = await repo.list_connections(seed.owner_a_scope)

        assert [item["id"] for item in results] == [alice["id"]]
        assert results[0]["account_id"] == seed.owner_a_scope.owner_user_id
        assert results[0]["owner_user_id"] == seed.owner_a_scope.owner_user_id
        assert results[0]["provider"] == "slack"
        assert results[0]["scopes"] == ["chat:write"]
        assert "encrypted_access_token" not in results[0]

    @pytest.mark.anyio
    async def test_upsert_connection_updates_existing_provider_identity(self, repo, seed):
        first = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="telegram",
            external_account_id="42",
            external_account_name="Alice",
            workspace_id=None,
            workspace_name=None,
            status="frozen",
        )
        second = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="telegram",
            external_account_id="42",
            external_account_name="Alice Telegram",
            workspace_id=None,
            workspace_name=None,
            status="connected",
        )

        assert second["id"] == first["id"]
        assert second["status"] == "connected"
        assert second["external_account_name"] == "Alice Telegram"
        assert len(await repo.list_connections(seed.owner_a_scope)) == 1

    @pytest.mark.anyio
    async def test_upsert_connection_transfers_external_identity_between_owners(self, repo, seed):
        await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-shared",
            workspace_id="T1",
            status="connected",
        )

        bob = await repo.upsert_connection(
            scope=seed.owner_b_scope,
            provider="slack",
            external_account_id="U-shared",
            workspace_id="T1",
            status="connected",
        )

        alice_rows = await repo.list_connections(seed.owner_a_scope)
        resolved = await repo.find_connection_by_external_identity(
            provider="slack",
            external_account_id="U-shared",
            workspace_id="T1",
        )

        assert alice_rows[0]["status"] == "revoked"
        assert bob["status"] == "connected"
        assert resolved is not None
        assert resolved["owner_user_id"] == seed.owner_b_scope.owner_user_id
        assert resolved["id"] == bob["id"]

    @pytest.mark.anyio
    async def test_active_identity_unique_index_rejects_second_connected_owner(self, repo, seed):
        # The single-active-owner invariant must be enforced by the database, not
        # only by the app-level revoke step (which can race under READ COMMITTED).
        from sqlalchemy.exc import IntegrityError

        await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-shared",
            workspace_id="T1",
            status="connected",
        )

        with pytest.raises(IntegrityError):
            async with repo.session_factory() as session:
                session.add(
                    ChannelConnectionRow(
                        id="manual-duplicate-active",
                        project_id=uuid.UUID(seed.owner_b_scope.project_id),
                        owner_user_id=seed.owner_b_scope.owner_user_id,
                        provider="slack",
                        external_account_id="U-shared",
                        workspace_id="T1",
                        status="connected",
                    )
                )
                await session.commit()

    @pytest.mark.anyio
    async def test_active_identity_unique_index_allows_revoked_rows(self, repo, seed):
        # A revoked row must not occupy the active-identity slot, so a fresh
        # connected bind for the same identity is allowed afterwards.
        first = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-shared",
            workspace_id="T1",
            status="connected",
        )
        await repo.disconnect_connection(
            scope=seed.owner_a_scope,
            connection_id=first["id"],
        )

        second = await repo.upsert_connection(
            scope=seed.owner_b_scope,
            provider="slack",
            external_account_id="U-shared",
            workspace_id="T1",
            status="connected",
        )
        assert second["status"] == "connected"

    @pytest.mark.anyio
    async def test_concurrent_upserts_keep_single_active_owner(self, repo, seed):
        import asyncio

        async def connect(scope):
            return await repo.upsert_connection(
                scope=scope,
                provider="slack",
                external_account_id="U-shared",
                workspace_id="T1",
                status="connected",
            )

        await asyncio.gather(
            connect(seed.owner_a_scope),
            connect(seed.owner_b_scope),
        )

        async with repo.session_factory() as session:
            connected = (
                (
                    await session.execute(
                        select(ChannelConnectionRow).where(
                            ChannelConnectionRow.provider == "slack",
                            ChannelConnectionRow.external_account_id == "U-shared",
                            ChannelConnectionRow.workspace_id == "T1",
                            ChannelConnectionRow.status == "connected",
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert len(connected) == 1

    @pytest.mark.anyio
    async def test_credentials_are_encrypted_at_rest_and_decrypted_by_repository(self, repo, seed):
        connection = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-alice",
            workspace_id="T1",
        )
        expires_at = datetime.now(UTC) + timedelta(hours=1)

        await repo.store_credentials(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
            access_token="xoxb-secret-access-token",
            refresh_token="secret-refresh-token",
            token_type="Bearer",
            expires_at=expires_at,
            extra={"bot_user_id": "B123"},
        )

        async with repo.session_factory() as session:
            row = (await session.execute(select(ChannelCredentialRow))).scalar_one()
            assert row.encrypted_access_token is not None
            assert "xoxb-secret-access-token" not in row.encrypted_access_token
            assert "secret-refresh-token" not in (row.encrypted_refresh_token or "")
            assert "B123" not in (row.encrypted_extra_json or "")

        credentials = await repo.get_credentials(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
        )

        assert credentials is not None
        assert credentials["access_token"] == "xoxb-secret-access-token"
        assert credentials["refresh_token"] == "secret-refresh-token"
        assert credentials["token_type"] == "Bearer"
        assert credentials["expires_at"] == expires_at
        assert credentials["extra"] == {"bot_user_id": "B123"}

    @pytest.mark.anyio
    async def test_get_credentials_returns_none_when_decryption_fails(self, repo, seed, caplog):
        connection = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-alice",
            workspace_id="T1",
        )
        await repo.store_credentials(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
            access_token="xoxb-secret-access-token",
        )
        wrong_key_repo = ChannelConnectionRepository(
            repo.session_factory,
            cipher=ChannelCredentialCipher.from_key("wrong-encryption-key"),
        )

        with caplog.at_level(logging.WARNING, logger="deerflow.persistence.channel_connections.sql"):
            credentials = await wrong_key_repo.get_credentials(
                scope=seed.owner_a_scope,
                connection_id=connection["id"],
            )

        assert credentials is None
        assert any("Unable to decrypt channel connection credentials" in record.message for record in caplog.records)

    @pytest.mark.anyio
    async def test_conversations_are_scoped_by_connection(self, repo, seed):
        await _create_thread(
            seed,
            scope=seed.owner_a_scope,
            thread_id="thread-alice",
            agent_id=seed.project_agent_id,
        )
        await _create_thread(
            seed,
            scope=seed.owner_b_scope,
            thread_id="thread-bob",
            agent_id=seed.project_agent_id,
        )
        alice = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-alice",
            workspace_id="T1",
        )
        bob = await repo.upsert_connection(
            scope=seed.owner_b_scope,
            provider="slack",
            external_account_id="U-bob",
            workspace_id="T1",
        )

        await repo.set_thread_id(
            scope=seed.owner_a_scope,
            connection_id=alice["id"],
            provider="slack",
            external_conversation_id="C-shared",
            external_topic_id="1710000000.000100",
            thread_id="thread-alice",
        )
        await repo.set_thread_id(
            scope=seed.owner_b_scope,
            connection_id=bob["id"],
            provider="slack",
            external_conversation_id="C-shared",
            external_topic_id="1710000000.000100",
            thread_id="thread-bob",
        )

        assert (
            await repo.get_thread_id(
                scope=seed.owner_a_scope,
                connection_id=alice["id"],
                external_conversation_id="C-shared",
                external_topic_id="1710000000.000100",
            )
            == "thread-alice"
        )
        assert (
            await repo.get_thread_id(
                scope=seed.owner_b_scope,
                connection_id=bob["id"],
                external_conversation_id="C-shared",
                external_topic_id="1710000000.000100",
            )
            == "thread-bob"
        )

    @pytest.mark.postgres
    @pytest.mark.anyio
    async def test_channel_store_round_trip_rejects_wrong_project_owner_scope(self, repo, seed):
        await _create_thread(
            seed,
            scope=seed.owner_a_scope,
            thread_id="thread-channel-store",
            agent_id=seed.project_agent_id,
        )
        connection = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="feishu",
            external_account_id="open-id-a",
            workspace_id="chat-a",
        )
        store = ChannelStore(repo)

        assert await store.set_thread_id(
            "feishu",
            "chat-a",
            "thread-channel-store",
            topic_id="message-a",
            connection_id=connection["id"],
            scope=seed.owner_a_scope,
        )
        assert (
            await store.get_thread_id(
                "feishu",
                "chat-a",
                topic_id="message-a",
                connection_id=connection["id"],
                scope=seed.owner_a_scope,
            )
            == "thread-channel-store"
        )
        assert (
            await store.get_thread_id(
                "feishu",
                "chat-a",
                topic_id="message-a",
                connection_id=connection["id"],
                scope=seed.owner_b_scope,
            )
            is None
        )
        assert (
            await store.get_thread_id(
                "slack",
                "chat-a",
                topic_id="message-a",
                connection_id=connection["id"],
                scope=seed.owner_a_scope,
            )
            is None
        )

    @pytest.mark.postgres
    @pytest.mark.anyio
    async def test_channel_store_concurrent_alias_writes_are_first_writer_wins(self, repo, seed):
        for thread_id in ("thread-channel-first", "thread-channel-second"):
            await _create_thread(
                seed,
                scope=seed.owner_a_scope,
                thread_id=thread_id,
                agent_id=seed.project_agent_id,
            )
        connection = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="feishu",
            external_account_id="open-id-race",
            workspace_id="chat-race",
        )
        first_store = ChannelStore(repo)
        second_store = ChannelStore(repo)

        results = await asyncio.gather(
            first_store.set_thread_id(
                "feishu",
                "chat-race",
                "thread-channel-first",
                topic_id="message-race",
                connection_id=connection["id"],
                scope=seed.owner_a_scope,
            ),
            second_store.set_thread_id(
                "feishu",
                "chat-race",
                "thread-channel-second",
                topic_id="message-race",
                connection_id=connection["id"],
                scope=seed.owner_a_scope,
            ),
        )

        assert sorted(results) == [False, True]
        persisted = await first_store.get_thread_id(
            "feishu",
            "chat-race",
            topic_id="message-race",
            connection_id=connection["id"],
            scope=seed.owner_a_scope,
        )
        assert persisted in {"thread-channel-first", "thread-channel-second"}

    @pytest.mark.anyio
    async def test_disconnect_connection_revokes_owner_connection_and_removes_credentials(self, repo, seed):
        connection = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="telegram",
            external_account_id="42",
        )
        await repo.store_credentials(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
            access_token="secret-token",
        )

        disconnected = await repo.disconnect_connection(
            scope=seed.owner_a_scope,
            connection_id=connection["id"],
        )

        assert disconnected is True
        async with repo.session_factory() as session:
            connection_row = await session.get(ChannelConnectionRow, connection["id"])
            credential_row = await session.get(ChannelCredentialRow, connection["id"])
        assert connection_row is not None
        assert connection_row.status == "revoked"
        assert credential_row is None
        assert (
            await repo.find_connection_by_external_identity(
                provider="telegram",
                external_account_id="42",
            )
            is None
        )

    @pytest.mark.anyio
    async def test_disconnect_connection_is_owner_scoped(self, repo, seed):
        connection = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="telegram",
            external_account_id="42",
        )

        disconnected = await repo.disconnect_connection(
            scope=seed.owner_b_scope,
            connection_id=connection["id"],
        )

        assert disconnected is False
        assert (await repo.list_connections(seed.owner_a_scope))[0]["status"] == "connected"

    @pytest.mark.anyio
    async def test_consume_oauth_state_deletes_expired_states(self, repo, seed):
        now = datetime.now(UTC)
        await repo.create_oauth_state(
            scope=seed.owner_a_scope,
            provider="slack",
            state="expired-state",
            expires_at=now - timedelta(minutes=1),
        )
        await repo.create_oauth_state(
            scope=seed.owner_a_scope,
            provider="slack",
            state="active-state",
            expires_at=now + timedelta(minutes=5),
        )

        consumed = await repo.consume_oauth_state(provider="slack", state="expired-state", now=now)

        assert consumed is None
        async with repo.session_factory() as session:
            states = (await session.execute(select(ChannelOAuthStateRow))).scalars().all()
        assert [state.state_hash for state in states] == [repo.hash_state("active-state")]

    @pytest.mark.anyio
    async def test_count_oauth_states_active_only_and_delete_expired(self, repo, seed):
        now = datetime.now(UTC)
        await repo.create_oauth_state(
            scope=seed.owner_a_scope,
            provider="slack",
            state="expired-state",
            expires_at=now - timedelta(minutes=1),
        )
        await repo.create_oauth_state(
            scope=seed.owner_a_scope,
            provider="slack",
            state="active-state",
            expires_at=now + timedelta(minutes=5),
        )

        assert (
            await repo.count_oauth_states(
                scope=seed.owner_a_scope,
                provider="slack",
                active_only=True,
                now=now,
            )
            == 1
        )
        assert await repo.delete_expired_oauth_states(now=now) == 1
        assert (
            await repo.count_oauth_states(
                scope=seed.owner_a_scope,
                provider="slack",
            )
            == 1
        )
        # Pin that the surviving row is the active one (an inverted expiry
        # predicate would delete the active row, still return 1, and pass above).
        async with repo.session_factory() as session:
            survivors = (await session.execute(select(ChannelOAuthStateRow))).scalars().all()
        assert [row.state_hash for row in survivors] == [repo.hash_state("active-state")]

    @pytest.mark.anyio
    async def test_create_oauth_state_within_cap_enforces_pending_cap(self, repo, seed):
        now = datetime.now(UTC)
        expires = now + timedelta(minutes=5)

        for i in range(3):
            inserted = await repo.create_oauth_state_within_cap(
                scope=seed.owner_a_scope,
                provider="slack",
                state=f"code-{i}",
                expires_at=expires,
                max_pending=3,
                now=now,
            )
            assert inserted is True

        # Cap reached: the next issuance is rejected and nothing is inserted.
        assert (
            await repo.create_oauth_state_within_cap(
                scope=seed.owner_a_scope,
                provider="slack",
                state="code-over",
                expires_at=expires,
                max_pending=3,
                now=now,
            )
            is False
        )
        assert (
            await repo.count_oauth_states(
                scope=seed.owner_a_scope,
                provider="slack",
                active_only=True,
                now=now,
            )
            == 3
        )

        # Expired rows are pruned and free up capacity; a different owner is unaffected.
        assert (
            await repo.create_oauth_state_within_cap(
                scope=seed.owner_b_scope,
                provider="slack",
                state="bob-1",
                expires_at=expires,
                max_pending=3,
                now=now,
            )
            is True
        )

    @pytest.mark.anyio
    async def test_create_oauth_state_within_cap_ignores_expired_rows(self, repo, seed):
        now = datetime.now(UTC)
        # Three already-expired rows must not count against the cap.
        for i in range(3):
            await repo.create_oauth_state(
                scope=seed.owner_a_scope,
                provider="slack",
                state=f"old-{i}",
                expires_at=now - timedelta(minutes=1),
            )

        inserted = await repo.create_oauth_state_within_cap(
            scope=seed.owner_a_scope,
            provider="slack",
            state="fresh",
            expires_at=now + timedelta(minutes=5),
            max_pending=3,
            now=now,
        )
        assert inserted is True
        assert (
            await repo.count_oauth_states(
                scope=seed.owner_a_scope,
                provider="slack",
                active_only=True,
                now=now,
            )
            == 1
        )

    @pytest.mark.anyio
    async def test_create_oauth_state_within_cap_does_not_leak_under_concurrency(self, repo, seed):
        """Concurrent issuance for one owner cannot push past the cap (willem #1)."""
        import anyio

        now = datetime.now(UTC)
        expires = now + timedelta(minutes=5)
        results: list[bool] = []

        async def issue(state: str) -> None:
            results.append(
                await repo.create_oauth_state_within_cap(
                    scope=seed.owner_a_scope,
                    provider="slack",
                    state=state,
                    expires_at=expires,
                    max_pending=3,
                    now=now,
                )
            )

        async with anyio.create_task_group() as tg:
            for i in range(8):
                tg.start_soon(issue, f"code-{i}")

        assert sum(1 for ok in results if ok) == 3
        assert (
            await repo.count_oauth_states(
                scope=seed.owner_a_scope,
                provider="slack",
                active_only=True,
                now=now,
            )
            == 3
        )

    @pytest.mark.anyio
    async def test_consume_oauth_state_is_one_time_even_under_concurrent_consumers(self, repo, seed):
        import anyio

        now = datetime.now(UTC)
        await repo.create_oauth_state(
            scope=seed.owner_a_scope,
            provider="slack",
            state="bind-once",
            expires_at=now + timedelta(minutes=5),
        )

        results: list = []

        async def consume():
            results.append(await repo.consume_oauth_state(provider="slack", state="bind-once", now=now))

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            tg.start_soon(consume)

        consumed = [result for result in results if result is not None]
        assert len(consumed) == 1
        assert consumed[0]["owner_user_id"] == seed.owner_a_scope.owner_user_id
        assert consumed[0]["project_id"] == seed.owner_a_scope.project_id

    @pytest.mark.anyio
    async def test_upsert_connection_retries_as_update_when_concurrent_insert_wins(self, repo, seed):
        """A losing concurrent INSERT retries as an UPDATE instead of raising IntegrityError."""
        first = await repo.upsert_connection(
            scope=seed.owner_a_scope,
            provider="slack",
            external_account_id="U-race",
            workspace_id="T-race",
            status="frozen",
        )

        real_factory = repo.session_factory

        class _EmptyResult:
            @staticmethod
            def scalar_one_or_none():
                return None

        class MissFirstSelectSession:
            """Make the initial identity SELECT miss, as if a concurrent writer inserted after it."""

            def __init__(self, session):
                self._session = session
                self._missed = False

            def __getattr__(self, name):
                return getattr(self._session, name)

            async def execute(self, *args, **kwargs):
                result = await self._session.execute(*args, **kwargs)
                if not self._missed:
                    self._missed = True
                    return _EmptyResult()
                return result

            async def __aenter__(self):
                await self._session.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self._session.__aexit__(*args)

        repo.session_factory = lambda: MissFirstSelectSession(real_factory())
        try:
            second = await repo.upsert_connection(
                scope=seed.owner_a_scope,
                provider="slack",
                external_account_id="U-race",
                workspace_id="T-race",
                status="connected",
            )
        finally:
            repo.session_factory = real_factory

        assert second["id"] == first["id"]
        assert second["status"] == "connected"
        connections = await repo.list_connections(seed.owner_a_scope)
        assert len(connections) == 1
