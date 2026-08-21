from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from support.private_thread_seed import (
    TEST_MODEL_REF,
    PrivateThreadSeed,
    seed_private_thread_database,
)

from app.automations.dispatcher import (
    AdmittedAutomationOccurrence,
    AutomationDefinitionRef,
    AutomationDispatcher,
)
from app.automations.models import AutomationCreate
from app.automations.service import ProjectAutomationService
from app.channels.message_bus import InboundMessage
from app.private_work.connection_inbound import (
    ProjectInboundDispatcher,
    ResolvedInboundPrivateWork,
)
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
)
from deerflow.persistence.private_work import RunAssetVersionRow


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


async def _activate_agent_v2(seed: PrivateThreadSeed) -> uuid.UUID:
    service = AgentService(
        seed.factory,
        catalog_validator=_AcceptingAgentCatalogValidator(),
    )
    context = _project_context(seed)
    candidate = await service.create_version(
        context,
        seed.project_agent_id,
        AgentPayload(
            description="Current Version entry-point test Agent",
            soul="VERSION_TWO",
            model_ref=TEST_MODEL_REF,
            tool_groups=(),
            skill_refs=(),
            mcp_version_ids=(),
        ),
        expected_asset_version=1,
    )
    await service.activate_version(
        context,
        seed.project_agent_id,
        candidate.id,
        expected_asset_version=2,
    )
    return candidate.id


async def _admitted_agent_version_id(
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
async def test_reuse_thread_automation_resolves_current_agent_at_occurrence_admission(
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
                title="Reuse Current Version",
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
        current_version_id = await _activate_agent_v2(seed)

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
            await _admitted_agent_version_id(
                seed,
                result.run.run_id,
            )
            == current_version_id
        )
    finally:
        await seed.engine.dispose()


class _ResolvedInbound:
    def __init__(self, resolved: ResolvedInboundPrivateWork) -> None:
        self._resolved = resolved

    async def resolve(self, *_args: object, **_kwargs: object) -> ResolvedInboundPrivateWork:
        return self._resolved


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_channel_message_resolves_current_agent_at_run_admission(
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

        current_version_id = await _activate_agent_v2(seed)
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
            await _admitted_agent_version_id(
                seed,
                run_id,
            )
            == current_version_id
        )
    finally:
        await seed.engine.dispose()
