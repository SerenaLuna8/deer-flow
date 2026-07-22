from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from support.m4_private_work import (
    LANGGRAPH_CHECKPOINT_TABLES,
    PRIVATE_PERSISTENCE_TABLES,
    M4ReleaseScenario,
    dump_table_bytes,
    m4_release_database_ready,
)

from app.channels.store import ChannelStore
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.authorization import (
    PrivateRunAuthorizationBoundary,
    PrivateRunAuthorizationService,
)
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.errors import (
    PrivateWorkAssetStale,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
)
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.memory_service import PrivateMemoryService
from app.private_work.run_admission import PrivateRunAdmissionService
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from deerflow.agents.memory.storage import create_empty_memory
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelCredentialCipher,
)
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.private_work.file_repository import PrivateFileRepository
from deerflow.persistence.private_work.model import PrivateArtifactRow
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.sandbox.sandbox import AuthorizationRevoked


async def _payload_chunks(payload: bytes):
    yield payload


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_m4_release_database_is_at_final_head_and_cutover_ready(
    migrated_postgres_database_url: str,
) -> None:
    assert await m4_release_database_ready(migrated_postgres_database_url)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_creates_project_and_system_agent_threads(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        project_thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-project-thread",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        system_thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-system-thread",
            agent=ThreadAgentRef(scenario.seed.system_agent_id, "system"),
        )

        assert project_thread.agent_asset_id == scenario.seed.project_agent_id
        assert system_thread.agent_asset_id == scenario.seed.system_agent_id
        assert {item.thread_id for item in await scenario.thread_service.search(scenario.seed.owner_a)} == {
            "release-project-thread",
            "release-system-thread",
        }
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_thread_is_hidden_across_owner_project_and_outsider(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        created = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-isolated-thread",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )

        assert await scenario.thread_service.get(scenario.seed.owner_a, created.thread_id) == created
        assert await scenario.thread_service.get(scenario.seed.owner_b, created.thread_id) is None
        assert await scenario.thread_service.get(scenario.seed.project_b_owner_a, created.thread_id) is None
        with pytest.raises(PrivateWorkNotFound):
            await scenario.thread_service.get(scenario.outsider, created.thread_id)
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_channel_store_is_postgres_scoped_and_multi_process_safe(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        for thread_id in ("release-channel-first", "release-channel-second"):
            await scenario.thread_service.create(
                scenario.seed.owner_a,
                thread_id=thread_id,
                agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
            )
        repository = ChannelConnectionRepository(scenario.seed.factory)
        connection = await repository.upsert_connection(
            scope=scenario.seed.owner_a_scope,
            provider="feishu",
            external_account_id="release-open-id",
            workspace_id="release-chat",
        )
        store_a = ChannelStore(repository)
        store_b = ChannelStore(repository)

        writes = await asyncio.gather(
            store_a.set_thread_id(
                "feishu",
                "release-chat",
                "release-channel-first",
                topic_id="release-message",
                connection_id=connection["id"],
                scope=scenario.seed.owner_a_scope,
            ),
            store_b.set_thread_id(
                "feishu",
                "release-chat",
                "release-channel-second",
                topic_id="release-message",
                connection_id=connection["id"],
                scope=scenario.seed.owner_a_scope,
            ),
        )

        assert sorted(writes) == [False, True]
        persisted = await store_a.get_thread_id(
            "feishu",
            "release-chat",
            topic_id="release-message",
            connection_id=connection["id"],
            scope=scenario.seed.owner_a_scope,
        )
        assert persisted in {"release-channel-first", "release-channel-second"}
        for denied_scope in (
            scenario.seed.owner_b_scope,
            scenario.seed.project_b_owner_a_scope,
        ):
            assert (
                await store_a.get_thread_id(
                    "feishu",
                    "release-chat",
                    topic_id="release-message",
                    connection_id=connection["id"],
                    scope=denied_scope,
                )
                is None
            )
        assert (
            await store_a.get_thread_id(
                "slack",
                "release-chat",
                topic_id="release-message",
                connection_id=connection["id"],
                scope=scenario.seed.owner_a_scope,
            )
            is None
        )
        entries = await store_a.list_entries(
            "feishu",
            connection_id=connection["id"],
            scope=scenario.seed.owner_a_scope,
        )
        assert len(entries) == 1
        assert entries[0]["thread_id"] == persisted
        assert (
            await store_a.list_entries(
                "feishu",
                connection_id=connection["id"],
                scope=scenario.seed.owner_b_scope,
            )
            == []
        )
        assert not await store_a.remove(
            "feishu",
            "release-chat",
            "release-message",
            connection_id=connection["id"],
            scope=scenario.seed.owner_b_scope,
        )
        assert await store_a.remove(
            "feishu",
            "release-chat",
            "release-message",
            connection_id=connection["id"],
            scope=scenario.seed.owner_a_scope,
        )
        assert (
            await store_a.get_thread_id(
                "feishu",
                "release-chat",
                topic_id="release-message",
                connection_id=connection["id"],
                scope=scenario.seed.owner_a_scope,
            )
            is None
        )
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_viewer_reads_and_deletes_own_thread_but_cannot_create(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        with pytest.raises(PrivateWorkForbidden):
            await scenario.thread_service.create(
                scenario.seed.viewer,
                thread_id="release-viewer-denied",
                agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
            )

        async with scenario.seed.factory() as session, session.begin():
            owned = await PrivateThreadRepository(session).create(
                scope=scenario.seed.viewer.resource_scope,
                thread_id="release-viewer-owned",
                agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
            )
        assert await scenario.thread_service.get(scenario.seed.viewer, owned.thread_id) == owned
        with pytest.raises(PrivateWorkForbidden):
            await PrivateRunAdmissionService(scenario.seed.factory).admit(
                scenario.seed.viewer,
                owned.thread_id,
                PrivateRunCreate(),
            )
        async with scenario.seed.factory() as session:
            denied_rows = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM runs WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM run_asset_versions WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM run_mcp_grant_snapshots WHERE thread_id=:thread_id)"""
                    ),
                    {"thread_id": owned.thread_id},
                )
            ).one()
        assert tuple(denied_rows) == (0, 0, 0)
        await scenario.thread_service.delete(
            scenario.seed.viewer,
            owned.thread_id,
            expected_version=owned.version,
        )
        assert await scenario.thread_service.get(scenario.seed.viewer, owned.thread_id) is None
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_owner_run_event_feedback_happy_path_is_scope_isolated(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-run-thread",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        admitted = await PrivateRunAdmissionService(scenario.seed.factory).admit(
            scenario.seed.owner_a,
            thread.thread_id,
            PrivateRunCreate(metadata={"source": "m4-release-gate"}),
        )
        events = DbRunEventStore(scenario.seed.factory)
        await events.put(
            scope=scenario.seed.owner_a_scope,
            thread_id=thread.thread_id,
            run_id=admitted.run.run_id,
            event_type="llm.ai.response",
            category="message",
            content="release answer",
        )
        feedback = await FeedbackRepository(scenario.seed.factory).create(
            scope=scenario.seed.owner_a_scope,
            run_id=admitted.run.run_id,
            thread_id=thread.thread_id,
            message_id="release-message",
            rating=1,
        )

        assert admitted.run.status == "pending"
        assert admitted.snapshot.assets[0].asset_id == scenario.seed.project_agent_id
        assert [row["content"] for row in await events.list_messages(thread.thread_id, scope=scenario.seed.owner_a_scope)] == ["release answer"]
        assert await events.list_messages(thread.thread_id, scope=scenario.seed.owner_b_scope) == []
        assert await events.list_messages(thread.thread_id, scope=scenario.seed.project_b_owner_a_scope) == []
        repository = FeedbackRepository(scenario.seed.factory)
        assert await repository.get(feedback["feedback_id"], scope=scenario.seed.owner_a_scope) is not None
        assert await repository.get(feedback["feedback_id"], scope=scenario.seed.owner_b_scope) is None
        assert await repository.get(feedback["feedback_id"], scope=scenario.seed.project_b_owner_a_scope) is None
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_exact_agent_snapshots_checkpoint_scope_and_revocation_fail_closed(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        admitted_runs = []
        for thread_id, agent in (
            ("release-exact-project", ThreadAgentRef(scenario.seed.project_agent_id, "project")),
            ("release-exact-system", ThreadAgentRef(scenario.seed.system_agent_id, "system")),
        ):
            thread = await scenario.thread_service.create(
                scenario.seed.owner_a,
                thread_id=thread_id,
                agent=agent,
            )
            admitted = await PrivateRunAdmissionService(scenario.seed.factory).admit(
                scenario.seed.owner_a,
                thread.thread_id,
                PrivateRunCreate(),
            )
            runtime = await PrivateAssetRuntime(scenario.seed.factory).materialize(
                scenario.seed.owner_a,
                admitted,
            )
            assert runtime.safe_manifest.agent_asset_id == agent.asset_id
            assert runtime.agent_version_id == admitted.snapshot.assets[0].version_id
            await runtime.aclose()
            admitted_runs.append(admitted)

        raw_tuple = await scenario.raw_checkpointer.aget_tuple(
            {
                "configurable": {
                    "thread_id": "release-exact-project",
                    "checkpoint_ns": "",
                }
            }
        )
        assert raw_tuple is not None
        assert raw_tuple.metadata["deerflow_private_scope"] == {
            "project_id": str(scenario.seed.owner_a.project_id),
            "owner_user_id": str(scenario.seed.owner_a.user_id),
        }

        stale_thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-stale-generation",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        stale = await PrivateRunAdmissionService(scenario.seed.factory).admit(
            scenario.seed.owner_a,
            stale_thread.thread_id,
            PrivateRunCreate(),
        )
        async with scenario.seed.factory() as session, session.begin():
            await session.execute(text("UPDATE asset_catalog_state SET generation=generation+1 WHERE id=1"))
        with pytest.raises(PrivateWorkAssetStale):
            await PrivateAssetRuntime(scenario.seed.factory).materialize(
                scenario.seed.owner_a,
                stale,
            )

        admitted = admitted_runs[0]
        boundary = PrivateRunAuthorizationBoundary(
            scenario.seed.factory,
            project_id=scenario.seed.owner_a.project_id,
            owner_user_id=str(scenario.seed.owner_a.user_id),
            run_id=admitted.run.run_id,
        )
        await boundary.before_model_call()
        async with scenario.seed.factory() as session, session.begin():
            assert await PrivateRunAuthorizationService.mark_revoked(
                session,
                project_id=scenario.seed.owner_a.project_id,
                owner_user_id=str(scenario.seed.owner_a.user_id),
            )
        with pytest.raises(AuthorizationRevoked):
            await boundary.before_checkpoint_write()
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_file_artifact_happy_path_and_viewer_file_boundary(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-file-thread",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        admitted = await PrivateRunAdmissionService(scenario.seed.factory).admit(
            scenario.seed.owner_a,
            thread.thread_id,
            PrivateRunCreate(),
        )
        payload = b"M4 private artifact\n"
        service = PrivateFileService(scenario.seed.factory)
        file_record = await service.upload(
            scenario.seed.owner_a,
            thread_id=thread.thread_id,
            logical_path="outputs/release.txt",
            media_type="text/plain",
            chunks=_payload_chunks(payload),
            kind="output",
            created_by_run_id=admitted.run.run_id,
        )
        artifact_id = uuid.uuid4()
        async with scenario.seed.factory() as session, session.begin():
            session.add(
                PrivateArtifactRow(
                    id=artifact_id,
                    project_id=scenario.seed.owner_a.project_id,
                    owner_user_id=str(scenario.seed.owner_a.user_id),
                    thread_id=thread.thread_id,
                    run_id=admitted.run.run_id,
                    file_id=file_record.id,
                    display_name="release.txt",
                    media_type="text/plain",
                    artifact_metadata={"logical_path": "outputs/release.txt"},
                )
            )
        stream = await PrivateFileStreamer(scenario.seed.factory).stream_artifact(
            scenario.seed.owner_a,
            thread_id=thread.thread_id,
            artifact_id=artifact_id,
        )
        assert b"".join([chunk async for chunk in stream.body]) == payload
        with pytest.raises(PrivateWorkNotFound):
            await PrivateFileStreamer(scenario.seed.factory).stream_artifact(
                scenario.seed.owner_b,
                thread_id=thread.thread_id,
                artifact_id=artifact_id,
            )

        async with scenario.seed.factory() as session, session.begin():
            viewer_thread = await PrivateThreadRepository(session).create(
                scope=scenario.seed.viewer.resource_scope,
                thread_id="release-viewer-file",
                agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
            )
            repository = PrivateFileRepository(session)
            staged = await repository.stage(
                scope=scenario.seed.viewer.resource_scope,
                thread_id=viewer_thread.thread_id,
                kind="upload",
                logical_path="uploads/viewer.txt",
                media_type="text/plain",
            )
            viewer_file = await repository.finalize(
                scope=scenario.seed.viewer.resource_scope,
                thread_id=viewer_thread.thread_id,
                file_id=staged.id,
                expected_size=0,
                expected_sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        assert [item.id for item in await service.list_ready(scenario.seed.viewer, thread_id=viewer_thread.thread_id)] == [viewer_file.id]
        with pytest.raises(PrivateWorkForbidden):
            await service.upload(
                scenario.seed.viewer,
                thread_id=viewer_thread.thread_id,
                logical_path="uploads/denied.txt",
                media_type="text/plain",
                chunks=_payload_chunks(b"denied"),
            )
        assert await service.delete_ready(
            scenario.seed.viewer,
            thread_id=viewer_thread.thread_id,
            file_id=viewer_file.id,
        )
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_memory_connection_and_secret_zero_persistence(
    migrated_postgres_database_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    secret_sentinel = "m4-release-plaintext-secret-sentinel"
    try:
        memory_service = PrivateMemoryService(scenario.seed.factory)
        initial = await memory_service.list(scenario.seed.owner_a)
        memory = create_empty_memory()
        memory["user"]["workContext"]["summary"] = "release owner memory"
        saved = await memory_service.import_memory(
            scenario.seed.owner_a,
            memory,
            expected_version=initial.version,
        )
        assert saved.memory["user"]["workContext"]["summary"] == "release owner memory"
        assert (await memory_service.list(scenario.seed.owner_b)).memory["user"]["workContext"]["summary"] == ""
        assert (await memory_service.list(scenario.seed.project_b_owner_a)).memory["user"]["workContext"]["summary"] == ""
        viewer_memory = await memory_service.list(scenario.seed.viewer)
        with pytest.raises(PrivateWorkForbidden):
            await memory_service.import_memory(
                scenario.seed.viewer,
                create_empty_memory(),
                expected_version=viewer_memory.version,
            )

        connection_repository = ChannelConnectionRepository(
            scenario.seed.factory,
            cipher=ChannelCredentialCipher.from_key("m4-release-cipher-key"),
        )
        connection_service = ProjectConnectionService(
            scenario.seed.factory,
            repository=connection_repository,
        )
        challenge = await connection_service.begin_connect(
            scenario.seed.owner_a,
            "slack",
            scenario.seed.project_agent_id,
        )
        connection = await connection_service.complete_callback(
            "slack",
            challenge.state,
            "release-external-account",
            "release-workspace",
        )
        assert [item["id"] for item in await connection_service.list(scenario.seed.owner_a)] == [connection["id"]]
        assert await connection_service.list(scenario.seed.owner_b) == []
        assert await connection_service.list(scenario.seed.project_b_owner_a) == []
        assert await connection_service.list(scenario.seed.viewer) == []
        with pytest.raises(PrivateWorkForbidden):
            await connection_service.begin_connect(
                scenario.seed.viewer,
                "slack",
                scenario.seed.project_agent_id,
            )
        with pytest.raises(PrivateWorkForbidden):
            await connection_service.disconnect(
                scenario.seed.viewer,
                str(connection["id"]),
            )
        assert await connection_repository.store_credentials(
            scope=scenario.seed.owner_a_scope,
            connection_id=str(connection["id"]),
            access_token=secret_sentinel,
            refresh_token=f"refresh-{secret_sentinel}",
            extra={"nested": secret_sentinel},
        )
        assert (
            await connection_repository.get_credentials(
                scope=scenario.seed.owner_a_scope,
                connection_id=str(connection["id"]),
            )
        )["access_token"] == secret_sentinel

        async with scenario.seed.engine.connect() as database:
            for table in PRIVATE_PERSISTENCE_TABLES + LANGGRAPH_CHECKPOINT_TABLES:
                assert secret_sentinel.encode() not in await dump_table_bytes(database, table)
        assert secret_sentinel not in caplog.text
    finally:
        await scenario.close()
