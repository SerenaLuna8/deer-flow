from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from support.private_thread_seed import (
    TEST_MODEL_REF,
    PrivateThreadSeed,
    seed_private_thread_database,
)

import app.channels.manager as channel_manager_module
from app.automations.dispatcher import (
    AdmittedAutomationOccurrence,
    AutomationDefinitionRef,
    AutomationDispatcher,
)
from app.automations.errors import AutomationUnavailable
from app.automations.models import AutomationCreate
from app.automations.service import ProjectAutomationService
from app.channels.manager import ChannelManager
from app.channels.message_bus import InboundMessage, MessageBus, OutboundMessage
from app.private_work.connection_inbound import (
    ProjectInboundDispatcher,
    ResolvedInboundPrivateWork,
)
from app.private_work.errors import LegacyAdmissionBusy, PrivateWorkTooLarge
from app.private_work.inbound_dedupe import PrivateRunInboundDelivery
from app.private_work.run_admission import (
    PrivateRunAdmissionServerContext,
    PrivateRunAdmissionService,
    PrivateRunInboundAuthority,
)
from app.private_work.run_repository import PrivateRunCreate
from app.private_work.thread_repository import (
    PrivateThreadRepository,
    ThreadAgentRef,
)
from app.projects.context import ProjectContext
from app.shared_assets.agent_service import AgentService
from app.shared_assets.models import AgentPayload
from deerflow.persistence.channel_connections import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ChannelInboundDeliveryRow,
)
from deerflow.persistence.private_work import RunAssetVersionRow
from deerflow.persistence.run.model import RunRow
from deerflow.persistence.scheduled_task_runs import ScheduledTaskRunRow
from deerflow.persistence.scheduled_tasks import ScheduledTaskRow


class _AcceptingAgentCatalogValidator:
    async def validate(self, *_args: object, **_kwargs: object) -> None:
        return None


def _project_context(seed: PrivateThreadSeed) -> ProjectContext:
    private = seed.owner_a
    return ProjectContext(
        user_id=private.user_id,
        project_id=private.project_id,
        membership_id=private.membership_id,
        role=private.role,
        capabilities=private.capabilities,
        membership_version=private.membership_version,
        request_id=private.request_id,
    )


async def _create_thread(
    seed: PrivateThreadSeed,
    thread_id: str,
) -> None:
    async with seed.factory() as session, session.begin():
        await PrivateThreadRepository(session).create(
            scope=seed.owner_a_scope,
            thread_id=thread_id,
            agent=ThreadAgentRef(seed.project_agent_id, "project"),
        )


async def _replace_agent_definition(seed: PrivateThreadSeed) -> uuid.UUID:
    service = AgentService(
        seed.factory,
        catalog_validator=_AcceptingAgentCatalogValidator(),
    )
    context = _project_context(seed)
    result = await service.replace_definition(
        context,
        seed.project_agent_id,
        AgentPayload(
            description="Current Definition entry-point test Agent",
            soul="DEFINITION_TWO",
            model_ref=TEST_MODEL_REF,
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=(),
            payload_schema_version=4,
        ),
        expected_asset_version=1,
    )
    return result.definition.definition_id


async def _admitted_agent_definition_id(
    seed: PrivateThreadSeed,
    run_id: str,
) -> uuid.UUID:
    async with seed.factory() as session:
        version_id = await session.scalar(
            select(RunAssetVersionRow.version_id).where(
                RunAssetVersionRow.project_id == seed.owner_a.project_id,
                RunAssetVersionRow.owner_user_id == str(seed.owner_a.user_id),
                RunAssetVersionRow.run_id == run_id,
                RunAssetVersionRow.asset_kind == "agent",
                RunAssetVersionRow.dependency_order == 0,
            )
        )
    assert isinstance(version_id, uuid.UUID)
    return version_id


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_reuse_thread_automation_resolves_latest_agent_definition_at_occurrence_admission(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"automation-reuse-{uuid.uuid4()}"
    now = datetime.now(UTC)
    scheduled_for = now + timedelta(hours=1)
    try:
        await _create_thread(seed, thread_id)
        task = await ProjectAutomationService(
            seed.factory,
            clock=lambda: now,
            min_once_delay_seconds=0,
        ).create(
            seed.owner_a,
            AutomationCreate(
                title="Reuse Current Definition",
                prompt="Use the Agent that is current when this occurrence runs.",
                context_mode="reuse_thread",
                thread_id=thread_id,
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
                schedule_type="once",
                schedule_spec={"run_at": scheduled_for.isoformat()},
                timezone="UTC",
            ),
        )
        current_definition_id = await _replace_agent_definition(seed)

        result = await AutomationDispatcher(seed.factory).admit_occurrence(
            AutomationDefinitionRef(
                project_id=seed.owner_a.project_id,
                owner_user_id=str(seed.owner_a.user_id),
                task_id=task.id,
                membership_version=seed.owner_a.membership_version,
            ),
            scheduled_for=scheduled_for,
            manual_idempotency_key=uuid.uuid4(),
        )

        assert isinstance(result, AdmittedAutomationOccurrence)
        assert result.run.thread_id == thread_id
        assert (
            await _admitted_agent_definition_id(
                seed,
                result.run.run_id,
            )
            == current_definition_id
        )
    finally:
        await seed.engine.dispose()


class _ResolvedInbound:
    def __init__(self, resolved: ResolvedInboundPrivateWork) -> None:
        self._resolved = resolved

    async def resolve(self, *_args: object, **_kwargs: object) -> ResolvedInboundPrivateWork:
        return self._resolved


class _BusyRunSnapshots:
    async def create_run_with_snapshot_in_session(
        self,
        _session: object,
        context: object,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        raise LegacyAdmissionBusy(context.request_id)


class _OversizeRunSnapshots:
    async def create_run_with_snapshot_in_session(
        self,
        _session: object,
        context: object,
        *_args: object,
        **_kwargs: object,
    ) -> None:
        raise PrivateWorkTooLarge(context.request_id)


@pytest.mark.parametrize(
    "snapshots",
    [_BusyRunSnapshots(), _OversizeRunSnapshots()],
    ids=["busy", "oversize"],
)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_scheduler_legacy_admission_failure_remains_due_without_terminal(
    migrated_postgres_database_url: str,
    snapshots: object,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    now = datetime.now(UTC)
    scheduled_for = now + timedelta(hours=1)
    try:
        task = await ProjectAutomationService(
            seed.factory,
            clock=lambda: now,
            min_once_delay_seconds=0,
        ).create(
            seed.owner_a,
            AutomationCreate(
                title="Retry legacy Admission",
                prompt="Retry this occurrence without terminalizing it.",
                context_mode="fresh_thread_per_run",
                thread_id=None,
                agent_asset_id=seed.project_agent_id,
                agent_scope="project",
                schedule_type="once",
                schedule_spec={"run_at": scheduled_for.isoformat()},
                timezone="UTC",
            ),
        )
        dispatcher = AutomationDispatcher(seed.factory)
        dispatcher._snapshots = snapshots  # type: ignore[assignment]  # noqa: SLF001

        with pytest.raises(AutomationUnavailable):
            await dispatcher.admit_occurrence(
                AutomationDefinitionRef(
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    task_id=task.id,
                    membership_version=seed.owner_a.membership_version,
                ),
                scheduled_for=scheduled_for,
            )

        async with seed.factory() as session:
            occurrence_count = await session.scalar(select(func.count()).select_from(ScheduledTaskRunRow).where(ScheduledTaskRunRow.task_id == task.id))
            persisted_task = await session.get(ScheduledTaskRow, task.id)
        assert occurrence_count == 0
        assert persisted_task is not None
        assert persisted_task.status == "enabled"
        assert persisted_task.next_run_at == scheduled_for
    finally:
        await seed.engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_channel_message_resolves_latest_agent_definition_at_run_admission(
    migrated_postgres_database_url: str,
) -> None:
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"channel-current-{uuid.uuid4()}"
    connection_id = f"conn-{uuid.uuid4()}"
    conversation_id = f"conversation-{uuid.uuid4()}"
    provider_delivery_id = f"delivery-{uuid.uuid4()}"
    authority = PrivateRunInboundAuthority(
        connection_id=connection_id,
        provider="test-channel",
        external_account_id="external-user",
        workspace_id="external-workspace",
        external_conversation_id=conversation_id,
        external_topic_id=None,
    )
    try:
        await _create_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            session.add(
                ChannelConnectionRow(
                    id=connection_id,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    provider=authority.provider,
                    status="connected",
                    external_account_id=authority.external_account_id,
                    workspace_id=authority.workspace_id or "",
                    metadata_json={
                        "agent_asset_id": str(seed.project_agent_id),
                        "agent_scope": "project",
                    },
                )
            )
            session.add(
                ChannelConversationRow(
                    id=f"conversation-row-{uuid.uuid4()}",
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    connection_id=connection_id,
                    provider=authority.provider,
                    external_conversation_id=conversation_id,
                    external_topic_id="",
                    thread_id=thread_id,
                )
            )

        current_definition_id = await _replace_agent_definition(seed)
        resolved = ResolvedInboundPrivateWork(
            account_id=seed.owner_a.user_id,
            context=seed.owner_a,
            connection_id=connection_id,
            thread_id=thread_id,
            created=False,
            authority=authority,
        )

        async def launch(context, resolved_thread_id, message, inbound_authority):
            admitted = await PrivateRunAdmissionService(seed.factory).admit(
                context,
                resolved_thread_id,
                PrivateRunCreate(
                    run_id=f"channel-run-{uuid.uuid4()}",
                    kwargs={
                        "input": {
                            "messages": [
                                {"role": "user", "content": message.text},
                            ]
                        }
                    },
                ),
                server_context=PrivateRunAdmissionServerContext(
                    inbound_authority=inbound_authority,
                    inbound_delivery=PrivateRunInboundDelivery(
                        message.provider_delivery_id,
                    ),
                ),
            )
            return {"run_id": admitted.run.run_id}

        result = await ProjectInboundDispatcher(
            _ResolvedInbound(resolved),
            launch,
        ).dispatch(
            InboundMessage(
                channel_name=authority.provider,
                chat_id=conversation_id,
                user_id=authority.external_account_id,
                text="Resolve the current Agent for this inbound message.",
                connection_id=connection_id,
                owner_user_id=str(seed.owner_a.user_id),
                private_scope=seed.owner_a_scope,
                project_id=str(seed.owner_a.project_id),
                workspace_id=authority.workspace_id,
                resolved_conversation_id=conversation_id,
                provider_delivery_id=provider_delivery_id,
            )
        )

        run_id = result.state["run_id"]
        assert isinstance(run_id, str)
        assert result.resolved.thread_id == thread_id
        assert (
            await _admitted_agent_definition_id(
                seed,
                run_id,
            )
            == current_definition_id
        )
    finally:
        await seed.engine.dispose()


@pytest.mark.parametrize(
    ("snapshots", "expected_calls", "expected_message"),
    [
        (
            _BusyRunSnapshots(),
            2,
            channel_manager_module.LEGACY_ADMISSION_RETRYABLE_MESSAGE,
        ),
        (
            _OversizeRunSnapshots(),
            1,
            PrivateWorkTooLarge.public_message,
        ),
    ],
    ids=["busy-retryable", "oversize-permanent"],
)
@pytest.mark.postgres
@pytest.mark.asyncio
async def test_channel_legacy_admission_failure_keeps_delivery_and_run_unbound(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    snapshots: object,
    expected_calls: int,
    expected_message: str,
) -> None:
    monkeypatch.setattr(
        channel_manager_module,
        "LEGACY_ADMISSION_MAX_RETRIES",
        1,
    )
    monkeypatch.setattr(
        channel_manager_module,
        "LEGACY_ADMISSION_RETRY_DELAY_SECONDS",
        0.01,
    )
    seed = await seed_private_thread_database(migrated_postgres_database_url)
    thread_id = f"channel-legacy-busy-{uuid.uuid4()}"
    run_id = f"channel-run-{uuid.uuid4()}"
    connection_id = f"conn-{uuid.uuid4()}"
    conversation_id = f"conversation-{uuid.uuid4()}"
    delivery = PrivateRunInboundDelivery(f"delivery-{uuid.uuid4()}")
    authority = PrivateRunInboundAuthority(
        connection_id=connection_id,
        provider="test-channel",
        external_account_id="external-user",
        workspace_id="external-workspace",
        external_conversation_id=conversation_id,
        external_topic_id=None,
    )
    try:
        await _create_thread(seed, thread_id)
        async with seed.factory() as session, session.begin():
            session.add(
                ChannelConnectionRow(
                    id=connection_id,
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    provider=authority.provider,
                    status="connected",
                    external_account_id=authority.external_account_id,
                    workspace_id=authority.workspace_id or "",
                    metadata_json={
                        "agent_asset_id": str(seed.project_agent_id),
                        "agent_scope": "project",
                    },
                )
            )
            session.add(
                ChannelConversationRow(
                    id=f"conversation-row-{uuid.uuid4()}",
                    project_id=seed.owner_a.project_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    connection_id=connection_id,
                    provider=authority.provider,
                    external_conversation_id=conversation_id,
                    external_topic_id="",
                    thread_id=thread_id,
                )
            )

        service = PrivateRunAdmissionService(
            seed.factory,
            snapshots=snapshots,  # type: ignore[arg-type]
        )
        calls = 0

        async def launch(context, resolved_thread_id, message, inbound_authority):
            nonlocal calls
            calls += 1
            return await service.admit(
                context,
                resolved_thread_id,
                PrivateRunCreate(
                    run_id=run_id,
                    kwargs={"input": {"messages": [{"role": "user", "content": message.text}]}},
                ),
                server_context=PrivateRunAdmissionServerContext(
                    inbound_authority=inbound_authority,
                    inbound_delivery=PrivateRunInboundDelivery(
                        message.provider_delivery_id,
                    ),
                ),
            )

        resolved = ResolvedInboundPrivateWork(
            account_id=seed.owner_a.user_id,
            context=seed.owner_a,
            connection_id=connection_id,
            thread_id=thread_id,
            created=False,
            authority=authority,
        )
        bus = MessageBus()
        outbound: list[OutboundMessage] = []

        async def capture(message: OutboundMessage) -> None:
            outbound.append(message)

        bus.subscribe_outbound(capture)
        manager = ChannelManager(
            bus,
            None,
            private_inbound_dispatcher=ProjectInboundDispatcher(
                _ResolvedInbound(resolved),
                launch,
            ),
        )
        await manager.start()
        try:
            assert await bus.publish_inbound(
                InboundMessage(
                    channel_name=authority.provider,
                    chat_id=conversation_id,
                    user_id=authority.external_account_id,
                    text="Retry or reject this delivery.",
                    connection_id=connection_id,
                    owner_user_id=str(seed.owner_a.user_id),
                    private_scope=seed.owner_a_scope,
                    project_id=str(seed.owner_a.project_id),
                    workspace_id=authority.workspace_id,
                    resolved_conversation_id=conversation_id,
                    provider_delivery_id=delivery.provider_delivery_id,
                )
            )
            async with asyncio.timeout(2):
                while not outbound:
                    await asyncio.sleep(0)
        finally:
            await manager.stop()

        assert calls == expected_calls
        assert [message.text for message in outbound] == [expected_message]

        async with seed.factory() as session:
            delivery_count = await session.scalar(
                select(func.count())
                .select_from(ChannelInboundDeliveryRow)
                .where(
                    ChannelInboundDeliveryRow.project_id == seed.owner_a.project_id,
                    ChannelInboundDeliveryRow.provider_delivery_digest == delivery.digest,
                )
            )
            run_count = await session.scalar(select(func.count()).select_from(RunRow).where(RunRow.run_id == run_id))
        assert delivery_count == 0
        assert run_count == 0
    finally:
        await seed.engine.dispose()
