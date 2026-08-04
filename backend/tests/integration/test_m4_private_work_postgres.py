from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from langchain_core.messages import AIMessage, HumanMessage
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from support.m4_private_work import (
    LANGGRAPH_CHECKPOINT_TABLES,
    PRIVATE_PERSISTENCE_TABLES,
    M4ReleaseScenario,
    dump_table_bytes,
    m4_release_database_ready,
)

from app.channels.store import ChannelStore
from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers.private_work import router
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.authorization import (
    PrivateRunAuthorizationBoundary,
    PrivateRunAuthorizationService,
)
from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.checkpoint_state import (
    bind_scoped_checkpoint_state,
    checkpoint_config,
    snapshot_checkpoint_id,
)
from app.private_work.connection_service import ProjectConnectionService
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkForbidden,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.inbound_dedupe import PrivateRunInboundDelivery
from app.private_work.memory_service import PrivateMemoryService
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
    PrivateRunInboundAuthority,
)
from app.private_work.run_repository import PrivateRunCreate, PrivateRunRepository
from app.private_work.thread_repository import (
    PrivateThreadRecord,
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.private_work.thread_service import PrivateThreadService
from app.projects.context import ProjectContext
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.binding_service import BindingService
from app.shared_assets.bootstrap import bootstrap_system_assets
from app.shared_assets.models import AssetKind, AssetSelection, SkillArchiveFile
from app.shared_assets.skill_service import CreateSkill, SkillService
from deerflow.agents.memory.storage import create_empty_memory
from deerflow.agents.middlewares.summarization_middleware import ContextCompactionResult
from deerflow.config.app_config import AppConfig
from deerflow.config.quota_config import QuotaConfig
from deerflow.persistence.channel_connections import (
    ChannelConnectionRepository,
    ChannelConversationRow,
    ChannelCredentialCipher,
)
from deerflow.persistence.feedback import FeedbackRepository
from deerflow.persistence.private_work.file_repository import PrivateFileRepository
from deerflow.persistence.private_work.model import PrivateArtifactRow
from deerflow.persistence.projects import ProjectDefaultAgentRow
from deerflow.persistence.shared_assets import (
    AgentRow,
    AgentVersionSkillRefRow,
    ProjectSystemAgentBindingRow,
    SkillVersionRow,
)
from deerflow.runtime.events.store.db import DbRunEventStore
from deerflow.sandbox.sandbox import AuthorizationRevoked


async def _payload_chunks(payload: bytes):
    yield payload


def _compaction_app_config(
    database_url: str,
    mode: str,
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            "database": {
                "url": database_url,
                "checkpoint_channel_mode": mode,
                "checkpoint_delta": {"snapshot_frequency": 100},
            },
            "summarization": {
                "enabled": True,
                "model_name": "release-summary-model",
                "keep": {"type": "messages", "value": 2},
            },
        }
    )


class _DeterministicCompactionMiddleware:
    """Exercise persistence and authorization without calling a provider."""

    def __init__(self, observed_message_ids: list[tuple[str | None, ...]]) -> None:
        self._observed_message_ids = observed_message_ids

    async def acompact_state(self, state, _runtime, *, force: bool = False):
        assert force is True
        messages = list(state["messages"])
        self._observed_message_ids.append(tuple(message.id for message in messages))
        if len(messages) < 3:
            return None
        return ContextCompactionResult(
            summary_text="release-summary: request-one and answer-one",
            messages_to_summarize=tuple(messages[:-2]),
            preserved_messages=tuple(messages[-2:]),
            total_tokens=123,
        )


def _compaction_service(scenario: M4ReleaseScenario) -> ProjectChatControlService:
    return ProjectChatControlService(
        scenario.seed.factory,
        scenario.project_checkpointer,
        scenario.thread_service,
        DbRunEventStore(scenario.seed.factory),
    )


async def _seed_compaction_messages(
    scenario: M4ReleaseScenario,
    *,
    thread_id: str,
    app_config: AppConfig,
):
    accessor = bind_scoped_checkpoint_state(
        scenario.project_checkpointer,
        scenario.seed.owner_a,
        app_config,
        as_node="release_compaction_seed",
    )
    messages = (
        HumanMessage(content="request one", id="release-human-1"),
        AIMessage(content="answer one", id="release-ai-1"),
        HumanMessage(content="request two", id="release-human-2"),
        AIMessage(content="answer two", id="release-ai-2"),
    )
    for message in messages:
        await accessor.aupdate(
            checkpoint_config(thread_id),
            {"messages": [message]},
            as_node="release_compaction_seed",
        )
    return accessor, messages


class _TwoPartyGate:
    """Release exactly two concurrent HTTP handlers from one rendezvous."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()
        self.arrivals = 0

    async def rendezvous(self) -> None:
        async with self._lock:
            self.arrivals += 1
            if self.arrivals == 2:
                self._ready.set()
        await asyncio.wait_for(self._ready.wait(), timeout=10)


class _SynchronizedThreadService(PrivateThreadService):
    def __init__(
        self,
        scenario: M4ReleaseScenario,
        *,
        create_gate: _TwoPartyGate | None = None,
        patch_gate: _TwoPartyGate | None = None,
    ) -> None:
        self._source_session_factory = scenario.seed.factory
        self.session_ids: set[int] = set()
        super().__init__(
            self._recorded_session,
            scenario.project_checkpointer,
        )
        self._create_gate = create_gate
        self._patch_gate = patch_gate

    def _recorded_session(self) -> AsyncSession:
        session = self._source_session_factory()
        self.session_ids.add(id(session))
        return session

    async def create(
        self,
        context: PrivateWorkContext,
        *,
        thread_id: str,
        agent: ThreadAgentRef,
        display_name: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> PrivateThreadRecord:
        if self._create_gate is not None:
            await self._create_gate.rendezvous()
        return await super().create(
            context,
            thread_id=thread_id,
            agent=agent,
            display_name=display_name,
            metadata=metadata,
        )

    async def patch(
        self,
        context: PrivateWorkContext,
        thread_id: str,
        *,
        expected_version: int,
        display_name: str | None,
    ) -> PrivateThreadRecord:
        if self._patch_gate is not None:
            await self._patch_gate.rendezvous()
        return await super().patch(
            context,
            thread_id,
            expected_version=expected_version,
            display_name=display_name,
        )


def _thread_http_app(
    scenario: M4ReleaseScenario,
    service: PrivateThreadService,
) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.private_thread_service = service
    app.state.project_scoped_checkpointer = scenario.project_checkpointer

    async def context_override(
        project_id: uuid.UUID,
        request: Request,
    ) -> PrivateWorkContext:
        del request
        if project_id != scenario.seed.owner_a.project_id:
            raise HTTPException(status_code=404)
        return scenario.seed.owner_a

    app.dependency_overrides[private_work_context] = context_override
    app.dependency_overrides[require_project_private_open] = lambda: None
    return app


async def _thread_http_request(
    app: FastAPI,
    project_id: uuid.UUID,
    method: str,
    suffix: str,
    *,
    body: dict[str, object],
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(
            method,
            f"/api/projects/{project_id}/private-work{suffix}",
            json=body,
        )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_m4_release_database_uses_final_baseline(
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
async def test_thread_create_resolves_project_default_main_and_explicit_override(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        await bootstrap_system_assets(scenario.seed.factory)
        async with scenario.seed.factory() as session:
            main_agent = (
                await session.execute(
                    select(AgentRow).where(
                        AgentRow.source_key == "builtin:agent:project-assistant",
                    )
                )
            ).scalar_one()
            assert main_agent.current_published_version_id is not None
            main_skill_dependencies = tuple(
                (
                    await session.execute(
                        select(
                            SkillVersionRow.skill_id,
                            AgentVersionSkillRefRow.skill_version_id,
                        )
                        .join(
                            SkillVersionRow,
                            SkillVersionRow.id == AgentVersionSkillRefRow.skill_version_id,
                        )
                        .where(
                            AgentVersionSkillRefRow.agent_version_id == main_agent.current_published_version_id,
                        )
                    )
                ).all()
            )

        project_actor = ProjectContext(
            user_id=scenario.seed.owner_a.user_id,
            project_id=scenario.seed.owner_a.project_id,
            membership_id=scenario.seed.owner_a.membership_id,
            role=scenario.seed.owner_a.role,
            capabilities=scenario.seed.owner_a.capabilities,
            membership_version=scenario.seed.owner_a.membership_version,
            request_id=scenario.seed.owner_a.request_id,
        )
        binding_service = BindingService(scenario.seed.factory)
        for skill_id, skill_version_id in main_skill_dependencies:
            await binding_service.enable(
                project_actor,
                AssetSelection(
                    kind=AssetKind.SKILL,
                    asset_id=skill_id,
                    version_id=skill_version_id,
                ),
            )
        await binding_service.enable(
            project_actor,
            AssetSelection(
                kind=AssetKind.AGENT,
                asset_id=main_agent.id,
                version_id=main_agent.current_published_version_id,
            ),
        )

        app = _thread_http_app(scenario, scenario.thread_service)

        main_response = await _thread_http_request(
            app,
            scenario.seed.owner_a.project_id,
            "POST",
            "/threads",
            body={"thread_id": str(uuid.uuid4()), "display_name": "Main fallback"},
        )
        assert main_response.status_code == 201
        assert main_response.json()["agent_asset_id"] == str(main_agent.id)
        assert main_response.json()["agent_scope"] == "system"

        async with scenario.seed.factory() as session:
            async with session.begin():
                session.add(
                    ProjectDefaultAgentRow(
                        project_id=scenario.seed.owner_a.project_id,
                        agent_asset_id=scenario.seed.project_agent_id,
                        revision=1,
                        created_by_user_id=str(scenario.seed.owner_a.user_id),
                        updated_by_user_id=str(scenario.seed.owner_a.user_id),
                    )
                )

        default_response = await _thread_http_request(
            app,
            scenario.seed.owner_a.project_id,
            "POST",
            "/threads",
            body={"thread_id": str(uuid.uuid4()), "display_name": "Configured default"},
        )
        assert default_response.status_code == 201
        assert default_response.json()["agent_asset_id"] == str(scenario.seed.project_agent_id)
        assert default_response.json()["agent_scope"] == "project"

        explicit_response = await _thread_http_request(
            app,
            scenario.seed.owner_a.project_id,
            "POST",
            "/threads",
            body={
                "thread_id": str(uuid.uuid4()),
                "agent_asset_id": str(scenario.seed.system_agent_id),
                "agent_scope": "system",
                "display_name": "Explicit Main override",
            },
        )
        assert explicit_response.status_code == 201
        assert explicit_response.json()["agent_asset_id"] == str(scenario.seed.system_agent_id)
        assert explicit_response.json()["agent_scope"] == "system"

        async with scenario.seed.factory() as session:
            async with session.begin():
                agent = await session.get(AgentRow, scenario.seed.project_agent_id)
                assert agent is not None
                agent.status = "suspended"

        unavailable_response = await _thread_http_request(
            app,
            scenario.seed.owner_a.project_id,
            "POST",
            "/threads",
            body={"thread_id": str(uuid.uuid4()), "display_name": "Unavailable default"},
        )
        assert unavailable_response.status_code == 409
        assert unavailable_response.json()["detail"] == {
            "code": "DEFAULT_AGENT_UNAVAILABLE",
            "message": "The project default Agent is unavailable.",
            "request_id": scenario.seed.owner_a.request_id,
        }

        async with scenario.seed.factory() as session:
            async with session.begin():
                selection = await session.get(
                    ProjectDefaultAgentRow,
                    scenario.seed.owner_a.project_id,
                )
                assert selection is not None
                selection.agent_asset_id = None
                selection.revision += 1
                binding = await session.get(
                    ProjectSystemAgentBindingRow,
                    (
                        scenario.seed.owner_a.project_id,
                        main_agent.id,
                    ),
                )
                assert binding is not None
                binding.enabled = False
                binding.version += 1

        unavailable_main_response = await _thread_http_request(
            app,
            scenario.seed.owner_a.project_id,
            "POST",
            "/threads",
            body={"thread_id": str(uuid.uuid4()), "display_name": "Unavailable Main"},
        )
        assert unavailable_main_response.status_code == 409
        assert unavailable_main_response.json()["detail"] == {
            "code": "DEFAULT_AGENT_UNAVAILABLE",
            "message": "The project default Agent is unavailable.",
            "request_id": scenario.seed.owner_a.request_id,
        }
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_scope_concurrent_explicit_thread_create_has_one_winner(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        gate = _TwoPartyGate()
        service = _SynchronizedThreadService(
            scenario,
            create_gate=gate,
        )
        app = _thread_http_app(
            scenario,
            service,
        )
        thread_id = uuid.uuid4()
        request_base = {
            "thread_id": str(thread_id),
            "agent_asset_id": str(scenario.seed.project_agent_id),
            "agent_scope": "project",
            "metadata": {"contract": "concurrent-create"},
        }

        responses = await asyncio.gather(
            _thread_http_request(
                app,
                scenario.seed.owner_a.project_id,
                "POST",
                "/threads",
                body={**request_base, "display_name": "Concurrent create A"},
            ),
            _thread_http_request(
                app,
                scenario.seed.owner_a.project_id,
                "POST",
                "/threads",
                body={**request_base, "display_name": "Concurrent create B"},
            ),
        )

        assert gate.arrivals == 2
        assert len(service.session_ids) == 2
        assert sorted(response.status_code for response in responses) == [201, 409]
        winner = next(response for response in responses if response.status_code == 201)
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["detail"]["code"] == "PRIVATE_WORK_CONFLICT"

        async with scenario.seed.factory() as session:
            thread_rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT display_name, version
                        FROM threads_meta
                        WHERE project_id=:project_id
                          AND owner_user_id=:owner_user_id
                          AND thread_id=:thread_id
                        """
                        ),
                        {
                            "project_id": scenario.seed.owner_a.project_id,
                            "owner_user_id": str(scenario.seed.owner_a.user_id),
                            "thread_id": str(thread_id),
                        },
                    )
                )
                .mappings()
                .all()
            )
            root_checkpoint_count = await session.scalar(
                text(
                    """
                    SELECT count(*)
                    FROM checkpoints
                    WHERE thread_id=:thread_id
                      AND checkpoint_ns=''
                    """
                ),
                {"thread_id": str(thread_id)},
            )

        assert len(thread_rows) == 1
        assert thread_rows[0]["display_name"] == winner.json()["display_name"]
        assert thread_rows[0]["version"] == 1
        assert root_checkpoint_count == 1
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_version_concurrent_thread_rename_has_one_winner(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    try:
        thread_id = uuid.uuid4()
        created = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=str(thread_id),
            agent=ThreadAgentRef(
                scenario.seed.project_agent_id,
                "project",
            ),
            display_name="Before concurrent rename",
        )
        assert created.version == 1

        gate = _TwoPartyGate()
        service = _SynchronizedThreadService(
            scenario,
            patch_gate=gate,
        )
        app = _thread_http_app(
            scenario,
            service,
        )
        responses = await asyncio.gather(
            _thread_http_request(
                app,
                scenario.seed.owner_a.project_id,
                "PATCH",
                f"/threads/{thread_id}",
                body={
                    "expected_version": 1,
                    "display_name": "Concurrent rename A",
                },
            ),
            _thread_http_request(
                app,
                scenario.seed.owner_a.project_id,
                "PATCH",
                f"/threads/{thread_id}",
                body={
                    "expected_version": 1,
                    "display_name": "Concurrent rename B",
                },
            ),
        )

        assert gate.arrivals == 2
        assert len(service.session_ids) == 2
        assert sorted(response.status_code for response in responses) == [200, 409]
        winner = next(response for response in responses if response.status_code == 200)
        conflict = next(response for response in responses if response.status_code == 409)
        assert conflict.json()["detail"]["code"] == "PRIVATE_WORK_CONFLICT"

        async with scenario.seed.factory() as session:
            final_row = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT display_name, version
                        FROM threads_meta
                        WHERE project_id=:project_id
                          AND owner_user_id=:owner_user_id
                          AND thread_id=:thread_id
                        """
                        ),
                        {
                            "project_id": scenario.seed.owner_a.project_id,
                            "owner_user_id": str(scenario.seed.owner_a.user_id),
                            "thread_id": str(thread_id),
                        },
                    )
                )
                .mappings()
                .one()
            )

        assert final_row["version"] == 2
        assert final_row["display_name"] == winner.json()["display_name"]
        assert final_row["display_name"] in {
            "Concurrent rename A",
            "Concurrent rename B",
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


def _inbound_server_context(
    *,
    connection_id: str,
    topic_id: str,
    delivery_id: str,
) -> PrivateRunAdmissionServerContext:
    return PrivateRunAdmissionServerContext(
        inbound_authority=PrivateRunInboundAuthority(
            connection_id=connection_id,
            provider="slack",
            external_account_id="release-channel-user",
            workspace_id="release-workspace",
            external_conversation_id="release-conversation",
            external_topic_id=topic_id,
        ),
        inbound_delivery=PrivateRunInboundDelivery(delivery_id),
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_channel_delivery_is_atomically_deduped_across_admission_instances(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(
        migrated_postgres_database_url,
    )
    thread_id = "release-channel-delivery-dedupe"
    try:
        await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=thread_id,
            agent=ThreadAgentRef(
                scenario.seed.project_agent_id,
                "project",
            ),
        )
        repository = ChannelConnectionRepository(scenario.seed.factory)
        connection = await repository.upsert_connection(
            scope=scenario.seed.owner_a_scope,
            provider="slack",
            external_account_id="release-channel-user",
            workspace_id="release-workspace",
            metadata={
                "agent_asset_id": str(scenario.seed.project_agent_id),
                "agent_scope": "project",
            },
        )
        assert await repository.set_thread_id(
            scope=scenario.seed.owner_a_scope,
            connection_id=connection["id"],
            provider="slack",
            external_conversation_id="release-conversation",
            external_topic_id="release-topic",
            thread_id=thread_id,
        )
        server_context = _inbound_server_context(
            connection_id=connection["id"],
            topic_id="release-topic",
            delivery_id="Release-Delivery-Opaque",
        )

        first, second = await asyncio.gather(
            PrivateRunAdmissionService(scenario.seed.factory).admit(
                scenario.seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=str(uuid.uuid4())),
                server_context=server_context,
            ),
            PrivateRunAdmissionService(scenario.seed.factory).admit(
                scenario.seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=str(uuid.uuid4())),
                server_context=server_context,
            ),
        )

        assert first.run.run_id == second.run.run_id
        assert sorted(
            (
                first.inbound_delivery_replay,
                second.inbound_delivery_replay,
            )
        ) == [False, True]
        async with scenario.seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM channel_inbound_deliveries
                         WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM runs
                         WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM jobs
                         WHERE run_id IN (
                           SELECT run_id FROM runs
                           WHERE thread_id=:thread_id
                         )),
                        (SELECT count(*) FROM run_asset_versions
                         WHERE thread_id=:thread_id)"""
                    ),
                    {"thread_id": thread_id},
                )
            ).one()
            stored_digest = await session.scalar(
                text(
                    """SELECT provider_delivery_digest
                    FROM channel_inbound_deliveries
                    WHERE thread_id=:thread_id"""
                ),
                {"thread_id": thread_id},
            )
        assert tuple(counts) == (1, 1, 1, 1)
        assert (
            stored_digest
            == PrivateRunInboundDelivery(
                "Release-Delivery-Opaque",
            ).digest
        )
        assert stored_digest != "Release-Delivery-Opaque"

        async with scenario.seed.factory() as session, session.begin():
            await session.execute(
                delete(ChannelConversationRow).where(
                    ChannelConversationRow.connection_id == connection["id"],
                    ChannelConversationRow.external_conversation_id == "release-conversation",
                    ChannelConversationRow.external_topic_id == "release-topic",
                )
            )
        replacement_thread_id = "release-channel-delivery-dedupe-replacement"
        await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=replacement_thread_id,
            agent=ThreadAgentRef(
                scenario.seed.project_agent_id,
                "project",
            ),
        )
        assert await repository.set_thread_id(
            scope=scenario.seed.owner_a_scope,
            connection_id=connection["id"],
            provider="slack",
            external_conversation_id="release-conversation",
            external_topic_id="release-topic",
            thread_id=replacement_thread_id,
        )
        replay = await PrivateRunAdmissionService(
            scenario.seed.factory,
        ).admit(
            scenario.seed.owner_a,
            replacement_thread_id,
            PrivateRunCreate(run_id=str(uuid.uuid4())),
            server_context=server_context,
        )
        assert replay.inbound_delivery_replay is True
        assert replay.run.run_id == first.run.run_id
        async with scenario.seed.factory() as session:
            remapped_counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM channel_inbound_deliveries
                         WHERE connection_id=:connection_id),
                        (SELECT count(*) FROM runs
                         WHERE thread_id IN (:thread_id, :replacement_thread_id)),
                        (SELECT count(*) FROM jobs
                         WHERE run_id IN (
                           SELECT run_id FROM runs
                           WHERE thread_id IN (:thread_id, :replacement_thread_id)
                         ))"""
                    ),
                    {
                        "connection_id": connection["id"],
                        "thread_id": thread_id,
                        "replacement_thread_id": replacement_thread_id,
                    },
                )
            ).one()
        assert tuple(remapped_counts) == (1, 1, 1)
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_same_provider_delivery_id_isolated_by_conversation_topic(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(
        migrated_postgres_database_url,
    )
    try:
        repository = ChannelConnectionRepository(scenario.seed.factory)
        connection = await repository.upsert_connection(
            scope=scenario.seed.owner_a_scope,
            provider="slack",
            external_account_id="release-channel-user",
            workspace_id="release-workspace",
            metadata={
                "agent_asset_id": str(scenario.seed.project_agent_id),
                "agent_scope": "project",
            },
        )
        admitted = []
        for topic_id in ("release-topic-a", "release-topic-b"):
            thread_id = f"release-channel-{topic_id}"
            await scenario.thread_service.create(
                scenario.seed.owner_a,
                thread_id=thread_id,
                agent=ThreadAgentRef(
                    scenario.seed.project_agent_id,
                    "project",
                ),
            )
            assert await repository.set_thread_id(
                scope=scenario.seed.owner_a_scope,
                connection_id=connection["id"],
                provider="slack",
                external_conversation_id="release-conversation",
                external_topic_id=topic_id,
                thread_id=thread_id,
            )
            admitted.append(
                await PrivateRunAdmissionService(
                    scenario.seed.factory,
                ).admit(
                    scenario.seed.owner_a,
                    thread_id,
                    PrivateRunCreate(run_id=str(uuid.uuid4())),
                    server_context=_inbound_server_context(
                        connection_id=connection["id"],
                        topic_id=topic_id,
                        delivery_id="shared-provider-delivery",
                    ),
                )
            )

        assert admitted[0].run.run_id != admitted[1].run.run_id
        assert not any(result.inbound_delivery_replay for result in admitted)
        async with scenario.seed.factory() as session:
            count = await session.scalar(
                text(
                    """SELECT count(*)
                    FROM channel_inbound_deliveries
                    WHERE connection_id=:connection_id"""
                ),
                {"connection_id": connection["id"]},
            )
        assert count == 2
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_channel_delivery_binding_rolls_back_with_failed_admission_hooks(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M4ReleaseScenario.create(
        migrated_postgres_database_url,
    )
    thread_id = "release-channel-delivery-rollback"

    class FailingAudit:
        async def run_admitted(
            self,
            session,
            context,
            run,
            job,
        ) -> None:
            assert session.in_transaction()
            assert run.job_id == job.job_id
            raise PrivateWorkUnavailable(context.request_id)

    try:
        await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=thread_id,
            agent=ThreadAgentRef(
                scenario.seed.project_agent_id,
                "project",
            ),
        )
        repository = ChannelConnectionRepository(scenario.seed.factory)
        connection = await repository.upsert_connection(
            scope=scenario.seed.owner_a_scope,
            provider="slack",
            external_account_id="release-channel-user",
            workspace_id="release-workspace",
            metadata={
                "agent_asset_id": str(scenario.seed.project_agent_id),
                "agent_scope": "project",
            },
        )
        assert await repository.set_thread_id(
            scope=scenario.seed.owner_a_scope,
            connection_id=connection["id"],
            provider="slack",
            external_conversation_id="release-conversation",
            external_topic_id="release-topic",
            thread_id=thread_id,
        )
        server_context = _inbound_server_context(
            connection_id=connection["id"],
            topic_id="release-topic",
            delivery_id="release-delivery-rollback",
        )
        failed_run_id = str(uuid.uuid4())
        with pytest.raises(PrivateWorkUnavailable):
            await PrivateRunAdmissionService(
                scenario.seed.factory,
                audit=FailingAudit(),
            ).admit(
                scenario.seed.owner_a,
                thread_id,
                PrivateRunCreate(run_id=failed_run_id),
                server_context=server_context,
            )

        async with scenario.seed.factory() as session:
            counts = (
                await session.execute(
                    text(
                        """SELECT
                        (SELECT count(*) FROM channel_inbound_deliveries
                         WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM runs
                         WHERE thread_id=:thread_id),
                        (SELECT count(*) FROM jobs
                         WHERE run_id=:run_id),
                        (SELECT count(*) FROM run_asset_versions
                         WHERE thread_id=:thread_id)"""
                    ),
                    {
                        "thread_id": thread_id,
                        "run_id": failed_run_id,
                    },
                )
            ).one()
        assert tuple(counts) == (0, 0, 0, 0)

        retried = await PrivateRunAdmissionService(
            scenario.seed.factory,
        ).admit(
            scenario.seed.owner_a,
            thread_id,
            PrivateRunCreate(run_id=str(uuid.uuid4())),
            server_context=server_context,
        )
        assert retried.inbound_delivery_replay is False
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

        generation_thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id="release-unrelated-generation",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        generation_admitted = await PrivateRunAdmissionService(scenario.seed.factory).admit(
            scenario.seed.owner_a,
            generation_thread.thread_id,
            PrivateRunCreate(),
        )
        project_b_private = scenario.seed.project_b_owner_a
        project_b = ProjectContext(
            user_id=project_b_private.user_id,
            project_id=project_b_private.project_id,
            membership_id=project_b_private.membership_id,
            role=project_b_private.role,
            capabilities=project_b_private.capabilities,
            membership_version=project_b_private.membership_version,
            request_id="release-project-b-skill",
        )
        project_b_skills = SkillService(
            scenario.seed.factory,
            quota=ProjectQuotaEnforcer(
                QuotaService(
                    scenario.seed.factory,
                    QuotaConfig(),
                    source_ref_hasher=AuditHmacKeyring.from_environment(),
                )
            ),
        )
        project_b_skill = await project_b_skills.create_asset(
            project_b,
            CreateSkill(
                slug="release-project-b-skill",
                display_name="Release Project B Skill",
            ),
        )
        project_b_draft = await project_b_skills.create_version_from_archive(
            project_b,
            project_b_skill.id,
            (
                SkillArchiveFile(
                    path="SKILL.md",
                    content=(b"---\nname: release-project-b-skill\ndescription: unrelated project B catalog mutation\n---\n\nKeep project A admitted snapshots valid.\n"),
                    media_type="text/markdown",
                ),
            ),
            expected_asset_version=1,
        )
        await project_b_skills.publish(
            project_b,
            project_b_skill.id,
            project_b_draft.id,
            expected_asset_version=2,
        )
        async with scenario.seed.factory() as session:
            current_generation = await session.scalar(text("SELECT generation FROM asset_catalog_state WHERE id=1"))
        assert current_generation > generation_admitted.snapshot.catalog_generation
        generation_runtime = await PrivateAssetRuntime(scenario.seed.factory).materialize(
            scenario.seed.owner_a,
            generation_admitted,
        )
        await generation_runtime.aclose()

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
@pytest.mark.parametrize("checkpoint_mode", ("full", "delta"))
async def test_manual_compaction_materializes_summary_and_retained_tail_in_postgres(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_mode: str,
) -> None:
    import deerflow.runtime.context_compaction as context_compaction

    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    observed_message_ids: list[tuple[str | None, ...]] = []
    middleware = _DeterministicCompactionMiddleware(observed_message_ids)
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )
    try:
        app_config = _compaction_app_config(
            migrated_postgres_database_url,
            checkpoint_mode,
        )
        thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=f"release-compaction-{checkpoint_mode}",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        accessor, messages = await _seed_compaction_messages(
            scenario,
            thread_id=thread.thread_id,
            app_config=app_config,
        )
        before = await accessor.aget(checkpoint_config(thread.thread_id))
        assert [message.id for message in before.values["messages"]] == [message.id for message in messages]

        raw_before = await scenario.raw_checkpointer.aget_tuple(checkpoint_config(thread.thread_id))
        assert raw_before is not None
        if checkpoint_mode == "delta":
            assert raw_before.metadata["deerflow_checkpoint_channel_mode"] == "delta"
            assert "messages" not in raw_before.checkpoint["channel_values"]
        else:
            assert "deerflow_checkpoint_channel_mode" not in raw_before.metadata
            assert [message.id for message in raw_before.checkpoint["channel_values"]["messages"]] == [message.id for message in messages]

        service = _compaction_service(scenario)
        with pytest.raises(PrivateWorkNotFound):
            await service.compact(
                scenario.seed.owner_b,
                thread.thread_id,
                force=True,
                keep=None,
                app_config=app_config,
            )
        with pytest.raises(PrivateWorkNotFound):
            await service.compact(
                scenario.seed.project_b_owner_a,
                thread.thread_id,
                force=True,
                keep=None,
                app_config=app_config,
            )
        with pytest.raises(PrivateWorkForbidden):
            await service.compact(
                scenario.seed.viewer,
                thread.thread_id,
                force=True,
                keep=None,
                app_config=app_config,
            )
        assert observed_message_ids == []

        result = await service.compact(
            scenario.seed.owner_a,
            thread.thread_id,
            force=True,
            keep=None,
            app_config=app_config,
        )

        assert result.compacted is True
        assert result.removed_message_count == 2
        assert result.preserved_message_count == 2
        assert result.summary_updated is True
        assert result.checkpoint_id is not None
        assert result.total_tokens == 123
        assert observed_message_ids == [tuple(message.id for message in messages)]

        latest = await accessor.aget(checkpoint_config(thread.thread_id))
        assert snapshot_checkpoint_id(latest) == result.checkpoint_id
        assert latest.values["summary_text"] == "release-summary: request-one and answer-one"
        assert [message.id for message in latest.values["messages"]] == [
            "release-human-2",
            "release-ai-2",
        ]
        raw_latest = await scenario.raw_checkpointer.aget_tuple(
            checkpoint_config(
                thread.thread_id,
                checkpoint_id=result.checkpoint_id,
            )
        )
        assert raw_latest is not None
        assert raw_latest.config["configurable"]["checkpoint_id"] == result.checkpoint_id
        if checkpoint_mode == "delta":
            assert raw_latest.metadata["deerflow_checkpoint_channel_mode"] == "delta"
            assert "messages" not in raw_latest.checkpoint["channel_values"]
        else:
            assert "deerflow_checkpoint_channel_mode" not in raw_latest.metadata
            assert [message.id for message in raw_latest.checkpoint["channel_values"]["messages"]] == [
                "release-human-2",
                "release-ai-2",
            ]
        history = await accessor.ahistory(
            checkpoint_config(thread.thread_id),
            limit=2,
        )
        assert [[message.id for message in snapshot.values["messages"]] for snapshot in history] == [
            ["release-human-2", "release-ai-2"],
            [message.id for message in messages],
        ]
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("checkpoint_mode", ("full", "delta"))
async def test_manual_compaction_postgres_compare_and_swap_rejects_a_new_head(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_mode: str,
) -> None:
    import app.private_work.chat_controls as chat_controls
    import deerflow.runtime.context_compaction as context_compaction

    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    observed_message_ids: list[tuple[str | None, ...]] = []
    middleware = _DeterministicCompactionMiddleware(observed_message_ids)
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )
    original_prepare = context_compaction.prepare_thread_compaction
    try:
        app_config = _compaction_app_config(
            migrated_postgres_database_url,
            checkpoint_mode,
        )
        thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=f"release-compaction-cas-{checkpoint_mode}",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        accessor, messages = await _seed_compaction_messages(
            scenario,
            thread_id=thread.thread_id,
            app_config=app_config,
        )
        source = await accessor.aget(checkpoint_config(thread.thread_id))
        source_checkpoint_id = snapshot_checkpoint_id(source)
        assert source_checkpoint_id is not None

        raced_checkpoint_id: str | None = None

        async def prepare_then_advance_head(reader, selected_thread_id, **kwargs):
            nonlocal raced_checkpoint_id
            prepared = await original_prepare(
                reader,
                selected_thread_id,
                **kwargs,
            )
            raced = await accessor.aupdate(
                checkpoint_config(selected_thread_id),
                {"title": "newer concurrent head"},
                as_node="release_compaction_seed",
            )
            raced_checkpoint_id = raced["configurable"]["checkpoint_id"]
            return prepared

        monkeypatch.setattr(
            chat_controls,
            "prepare_thread_compaction",
            prepare_then_advance_head,
        )

        with pytest.raises(PrivateWorkConflict):
            await _compaction_service(scenario).compact(
                scenario.seed.owner_a,
                thread.thread_id,
                force=True,
                keep=None,
                app_config=app_config,
            )

        assert raced_checkpoint_id is not None
        assert raced_checkpoint_id != source_checkpoint_id
        assert observed_message_ids == [tuple(message.id for message in messages)]
        current = await accessor.aget(checkpoint_config(thread.thread_id))
        assert snapshot_checkpoint_id(current) == raced_checkpoint_id
        assert current.values["title"] == "newer concurrent head"
        assert [message.id for message in current.values["messages"]] == [message.id for message in messages]
        assert not current.values.get("summary_text")
    finally:
        await scenario.close()


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "finalization_status"),
    (
        ("pending", "pending"),
        ("running", "pending"),
        ("success", "finalizing"),
    ),
)
async def test_manual_compaction_rejects_incomplete_postgres_run(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
    finalization_status: str,
) -> None:
    import deerflow.runtime.context_compaction as context_compaction

    scenario = await M4ReleaseScenario.create(migrated_postgres_database_url)
    observed_message_ids: list[tuple[str | None, ...]] = []
    middleware = _DeterministicCompactionMiddleware(observed_message_ids)
    monkeypatch.setattr(
        context_compaction,
        "_create_compaction_middleware",
        lambda **_kwargs: middleware,
    )
    try:
        app_config = _compaction_app_config(
            migrated_postgres_database_url,
            "full",
        )
        thread = await scenario.thread_service.create(
            scenario.seed.owner_a,
            thread_id=f"release-compaction-active-{run_status}-{finalization_status}",
            agent=ThreadAgentRef(scenario.seed.project_agent_id, "project"),
        )
        accessor, messages = await _seed_compaction_messages(
            scenario,
            thread_id=thread.thread_id,
            app_config=app_config,
        )
        before = await accessor.aget(checkpoint_config(thread.thread_id))
        before_checkpoint_id = snapshot_checkpoint_id(before)
        assert before_checkpoint_id is not None

        run_id = str(uuid.uuid4())
        async with scenario.seed.factory() as session, session.begin():
            await PrivateRunRepository(session).create(
                scope=scenario.seed.owner_a.resource_scope,
                thread_id=thread.thread_id,
                request=PrivateRunCreate(
                    run_id=run_id,
                    status=run_status,
                    model_name="release-model",
                ),
            )
            if finalization_status == "finalizing":
                await session.execute(
                    text("UPDATE runs SET finalization_status='finalizing' WHERE project_id=:project_id AND owner_user_id=:owner_user_id AND run_id=:run_id"),
                    {
                        "project_id": scenario.seed.owner_a.project_id,
                        "owner_user_id": str(scenario.seed.owner_a.user_id),
                        "run_id": run_id,
                    },
                )

        with pytest.raises(PrivateWorkConflict):
            await _compaction_service(scenario).compact(
                scenario.seed.owner_a,
                thread.thread_id,
                force=True,
                keep=None,
                app_config=app_config,
            )

        assert observed_message_ids == []
        current = await accessor.aget(checkpoint_config(thread.thread_id))
        assert snapshot_checkpoint_id(current) == before_checkpoint_id
        assert [message.id for message in current.values["messages"]] == [message.id for message in messages]
        assert not current.values.get("summary_text")
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
        challenge = await connection_service.begin_legacy_connect(
            scenario.seed.owner_a,
            "slack",
            scenario.seed.project_agent_id,
        )
        connection = await connection_service.complete_callback(
            "slack",
            challenge.state,
            "release-external-account",
            "release-workspace",
            channel_instance_id="slack",
        )
        assert [item["id"] for item in await connection_service.list(scenario.seed.owner_a)] == [connection["id"]]
        assert await connection_service.list(scenario.seed.owner_b) == []
        assert await connection_service.list(scenario.seed.project_b_owner_a) == []
        assert await connection_service.list(scenario.seed.viewer) == []
        with pytest.raises(PrivateWorkForbidden):
            await connection_service.begin_legacy_connect(
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
