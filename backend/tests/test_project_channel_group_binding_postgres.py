from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from support.m3_shared_assets import M3Scenario

from app.channel_group_bindings.errors import GroupBindingNotFound
from app.channel_group_bindings.identity import AuditChannelGroupIdentityHasher
from app.channel_group_bindings.models import (
    CreateGroupBindingChallenge,
    UpdateGroupBinding,
)
from app.channel_group_bindings.service import ProjectChannelGroupBindingService
from app.channels.feishu import FeishuChannel
from app.channels.message_bus import MessageBus, OutboundMessage
from app.channels.service import _make_group_identity_candidates
from app.channels.store import ChannelStore
from app.private_work.thread_repository import PrivateThreadRepository, ThreadAgentRef
from app.reliability.owner_refs import AuditHmacKeyring
from app.shared_assets.agent_service import CreateAgent
from deerflow.persistence.channel_connections.group_challenge_model import (
    ProjectChannelGroupBindingChallengeRow,
)
from deerflow.persistence.channel_connections.group_model import (
    ChannelExternalPrincipalRow,
    ProjectChannelGroupBindingRow,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ChannelConversationRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.channel_connections.project_instance_repository import (
    ProjectChannelInstanceRepository,
)
from deerflow.persistence.channel_connections.sql import ChannelConnectionRepository
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.user.model import UserRow
from deerflow.runtime.private_scope import PrivateResourceScope

_NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)
_HEX_64 = re.compile(r"[0-9a-f]{64}")


def _assert_hmac(value: str, *raw_values: str) -> None:
    assert _HEX_64.fullmatch(value)
    for raw_value in raw_values:
        assert raw_value not in value


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_group_binding_isolates_guest_owners_and_freezes_lifecycle(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    try:
        await scenario.bootstrap_system_catalog()
        assert scenario.project_agent_id is not None
        second_agent = await scenario.agents.create_asset(
            scenario.project_admin,
            CreateAgent("m3-group-second-agent", "M3 Group Second Agent"),
        )
        second_agent_draft = await scenario.agents.create_version(
            scenario.project_admin,
            second_agent.id,
            scenario._agent_payload("M3 Group Second Agent"),
            expected_asset_version=second_agent.version,
        )
        await scenario.agents.publish(
            scenario.project_admin,
            second_agent.id,
            second_agent_draft.id,
            expected_asset_version=second_agent.version + 1,
        )

        instance_id = uuid.uuid4()
        admin_user_id = str(scenario.project_admin.user_id)
        async with scenario.session_factory() as session, session.begin():
            session.add(
                ProjectChannelInstanceRow(
                    id=instance_id,
                    project_id=scenario.project_admin.project_id,
                    provider="feishu",
                    display_name="Project Feishu",
                    desired_status="enabled",
                    observed_status="running",
                    public_config={"app_id": "cli_public_only"},
                    provider_identity_digest="d" * 64,
                    created_by_user_id=admin_user_id,
                    updated_by_user_id=admin_user_id,
                )
            )

        raw_group_id = "oc_raw_group_pg_sentinel"
        raw_sender_a = "ou_raw_sender_a_pg_sentinel"
        raw_sender_b = "ou_raw_sender_b_pg_sentinel"
        raw_topic_id = "om_raw_topic_pg_sentinel"
        raw_bind_code = "bind-project-code-1234567890"
        identity_hasher = AuditChannelGroupIdentityHasher(
            AuditHmacKeyring(
                active_key_id="group-binding-pg",
                _keys={"group-binding-pg": b"g" * 32},
            )
        )
        service = ProjectChannelGroupBindingService(
            scenario.session_factory,
            identity_hasher=identity_hasher,
            clock=lambda: _NOW,
            code_factory=lambda: raw_bind_code,
        )

        challenge = await service.create_challenge(
            scenario.project_admin,
            CreateGroupBindingChallenge(
                provider="feishu",
                agent_asset_id=scenario.project_agent_id,
                agent_scope="project",
            ),
        )
        assert challenge.command == f"/bind-project {raw_bind_code}"

        binding = await service.complete_challenge(
            provider="feishu",
            channel_instance_id=instance_id,
            code=raw_bind_code,
            chat_id=raw_group_id,
            sender_id=raw_sender_a,
            display_name="研发群",
        )
        assert binding.display_name == "研发群"
        assert binding.status == "active"

        runtime_repository = ProjectChannelInstanceRepository()
        async with scenario.session_factory() as runtime_session:
            async with runtime_session.begin():
                await runtime_repository.set_observed_status(
                    runtime_session,
                    channel_instance_id=instance_id,
                    observed_status="stopped",
                    last_error_code=None,
                )
                blocked_guest = asyncio.create_task(
                    service.resolve_or_create_guest(
                        provider="feishu",
                        channel_instance_id=instance_id,
                        chat_id=raw_group_id,
                        sender_id="ou_runtime_stop_race_pg_sentinel",
                        topic_id=raw_topic_id,
                    )
                )
                await asyncio.sleep(0.05)
                assert blocked_guest.done() is False
            with pytest.raises(GroupBindingNotFound):
                await asyncio.wait_for(blocked_guest, timeout=5)
            async with runtime_session.begin():
                await runtime_repository.set_observed_status(
                    runtime_session,
                    channel_instance_id=instance_id,
                    observed_status="running",
                    last_error_code=None,
                )

        sender_a_first = await service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=instance_id,
            chat_id=raw_group_id,
            sender_id=raw_sender_a,
            topic_id=raw_topic_id,
        )
        sender_a_second = await service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=instance_id,
            chat_id=raw_group_id,
            sender_id=raw_sender_a,
            topic_id=raw_topic_id,
        )
        sender_b = await service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=instance_id,
            chat_id=raw_group_id,
            sender_id=raw_sender_b,
            topic_id=raw_topic_id,
        )

        assert sender_a_second == sender_a_first
        assert sender_a_first["owner_user_id"] == sender_a_first["account_id"]
        assert sender_b["owner_user_id"] == sender_b["account_id"]
        assert sender_a_first["owner_user_id"] != sender_b["owner_user_id"]
        assert sender_a_first["id"] != sender_b["id"]
        assert sender_a_first["project_id"] == sender_b["project_id"] == str(scenario.project_admin.project_id)
        assert sender_a_first["channel_instance_id"] == sender_b["channel_instance_id"] == str(instance_id)
        _assert_hmac(sender_a_first["resolved_conversation_id"], raw_group_id)
        _assert_hmac(sender_a_first["resolved_topic_id"], raw_topic_id)

        async with scenario.session_factory() as session:
            stored_challenge = (await session.execute(select(ProjectChannelGroupBindingChallengeRow).where(ProjectChannelGroupBindingChallengeRow.project_id == scenario.project_admin.project_id))).scalar_one()
            stored_binding = await session.get(ProjectChannelGroupBindingRow, binding.id)
            principals = tuple((await session.execute(select(ChannelExternalPrincipalRow).where(ChannelExternalPrincipalRow.group_binding_id == binding.id).order_by(ChannelExternalPrincipalRow.id))).scalars())
            connections = tuple(
                (
                    await session.execute(
                        select(ChannelConnectionRow)
                        .where(
                            ChannelConnectionRow.project_id == scenario.project_admin.project_id,
                            ChannelConnectionRow.channel_instance_id == instance_id,
                        )
                        .order_by(ChannelConnectionRow.id)
                    )
                ).scalars()
            )
            guest_user_ids = tuple(principal.principal_user_id for principal in principals)
            memberships = tuple(
                (
                    await session.execute(
                        select(ProjectMembershipRow)
                        .where(
                            ProjectMembershipRow.project_id == scenario.project_admin.project_id,
                            ProjectMembershipRow.user_id.in_(guest_user_ids),
                        )
                        .order_by(ProjectMembershipRow.id)
                    )
                ).scalars()
            )
            users = tuple((await session.execute(select(UserRow).where(UserRow.id.in_(guest_user_ids)).order_by(UserRow.id))).scalars())

        assert stored_binding is not None
        assert stored_challenge.consumed_at == _NOW
        _assert_hmac(stored_challenge.code_digest, raw_bind_code)
        _assert_hmac(stored_binding.external_group_ref, raw_group_id)
        assert len(principals) == len(connections) == len(memberships) == len(users) == 2
        assert {principal.principal_user_id for principal in principals} == {
            str(sender_a_first["owner_user_id"]),
            str(sender_b["owner_user_id"]),
        }
        assert {connection.owner_user_id for connection in connections} == {
            str(sender_a_first["owner_user_id"]),
            str(sender_b["owner_user_id"]),
        }
        assert all(principal.status == "active" for principal in principals)
        assert all(connection.status == "connected" for connection in connections)
        assert all(membership.role == "channel_guest" for membership in memberships)
        assert all(user.principal_type == "channel_guest" for user in users)
        assert all(user.email is None and user.password_hash is None and user.oauth_provider is None and user.oauth_id is None for user in users)
        for principal in principals:
            _assert_hmac(principal.external_account_ref, raw_sender_a, raw_sender_b)
        for connection in connections:
            _assert_hmac(connection.external_account_id, raw_sender_a, raw_sender_b)
            _assert_hmac(connection.workspace_id, raw_group_id)
        persisted_projection = repr(
            (
                stored_challenge.code_digest,
                stored_binding.external_group_ref,
                principals,
                connections,
                memberships,
                users,
            )
        )
        assert raw_bind_code not in persisted_projection
        assert raw_group_id not in persisted_projection
        assert raw_sender_a not in persisted_projection
        assert raw_sender_b not in persisted_projection
        assert raw_topic_id not in persisted_projection

        # A personal /connect row can coexist with the group guest row. Make
        # it newer so the legacy fuzzy lookup would choose the human owner,
        # then prove the attached exact connection remains authoritative.
        personal_connection_id = f"personal-{uuid.uuid4().hex}"
        async with scenario.session_factory() as session, session.begin():
            session.add(
                ChannelConnectionRow(
                    id=personal_connection_id,
                    project_id=scenario.project_admin.project_id,
                    owner_user_id=admin_user_id,
                    provider="feishu",
                    channel_instance_id=instance_id,
                    status="connected",
                    external_account_id=raw_sender_a,
                    workspace_id=raw_group_id,
                    scopes_json=[],
                    capabilities_json={},
                    metadata_json={},
                    created_at=_NOW + timedelta(seconds=1),
                    updated_at=_NOW + timedelta(seconds=1),
                )
            )
        connection_repository = ChannelConnectionRepository(
            scenario.session_factory,
            external_identity_candidates=(_make_group_identity_candidates(identity_hasher)),
        )
        fuzzy = await connection_repository.find_connection_by_external_identity(
            provider="feishu",
            channel_instance_id=instance_id,
            external_account_id=raw_sender_a,
            workspace_id=raw_group_id,
        )
        exact_guest = await connection_repository.find_connection_by_external_identity(
            provider="feishu",
            channel_instance_id=instance_id,
            external_account_id=raw_sender_a,
            workspace_id=raw_group_id,
            expected_connection_id=str(sender_a_first["id"]),
            expected_scope=PrivateResourceScope(
                project_id=str(sender_a_first["project_id"]),
                owner_user_id=str(sender_a_first["owner_user_id"]),
                membership_version=int(
                    sender_a_first["membership_version"],
                ),
            ),
        )
        assert fuzzy is not None and fuzzy["id"] == personal_connection_id
        assert exact_guest is not None
        assert exact_guest["id"] == sender_a_first["id"]
        assert exact_guest["owner_user_id"] == sender_a_first["owner_user_id"]
        async with scenario.session_factory() as session, session.begin():
            await session.execute(
                delete(ChannelConnectionRow).where(
                    ChannelConnectionRow.id == personal_connection_id,
                )
            )

        # Feishu reply/card aliases must stay pseudonymous as well. This
        # covers the outbound continuity path that appends aliases after the
        # initial guest resolver has already created the conversation.
        guest_scope = PrivateResourceScope(
            project_id=str(sender_a_first["project_id"]),
            owner_user_id=str(sender_a_first["owner_user_id"]),
            membership_version=int(sender_a_first["membership_version"]),
        )
        thread_id = str(uuid.uuid4())
        async with scenario.session_factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=guest_scope,
                thread_id=thread_id,
                agent=ThreadAgentRef(scenario.project_agent_id, "project"),
                metadata={"source": "channel"},
            )
        raw_aliases = {
            "om_raw_source_pg_sentinel",
            "om_raw_card_pg_sentinel",
            "om_raw_root_pg_sentinel",
            "om_raw_parent_pg_sentinel",
        }
        feishu = FeishuChannel(
            MessageBus(),
            {
                "app_id": "test",
                "app_secret": "test",
                "channel_store": ChannelStore(connection_repository),
                "channel_group_binding_service": service,
                "channel_instance_id": str(instance_id),
            },
        )
        await feishu._remember_thread_mapping(
            OutboundMessage(
                channel_name="feishu",
                channel_instance_id=str(instance_id),
                chat_id=raw_group_id,
                thread_id=thread_id,
                text="done",
                thread_ts="om_raw_source_pg_sentinel",
                connection_id=str(sender_a_first["id"]),
                private_scope=guest_scope,
                resolved_conversation_id=str(
                    sender_a_first["resolved_conversation_id"],
                ),
                resolved_topic_id=str(sender_a_first["resolved_topic_id"]),
                metadata={
                    "root_id": "om_raw_root_pg_sentinel",
                    "parent_id": "om_raw_parent_pg_sentinel",
                },
            ),
            "om_raw_source_pg_sentinel",
            "om_raw_card_pg_sentinel",
        )
        async with scenario.session_factory() as session:
            alias_rows = tuple(
                (
                    await session.execute(
                        select(ChannelConversationRow).where(
                            ChannelConversationRow.project_id == scenario.project_admin.project_id,
                            ChannelConversationRow.owner_user_id == str(sender_a_first["owner_user_id"]),
                            ChannelConversationRow.connection_id == str(sender_a_first["id"]),
                            ChannelConversationRow.thread_id == thread_id,
                        )
                    )
                ).scalars()
            )
        assert alias_rows
        for row in alias_rows:
            _assert_hmac(row.external_conversation_id, raw_group_id)
            _assert_hmac(
                row.external_topic_id,
                raw_topic_id,
                *raw_aliases,
            )
        alias_projection = repr(alias_rows)
        assert raw_group_id not in alias_projection
        assert raw_topic_id not in alias_projection
        assert all(raw_alias not in alias_projection for raw_alias in raw_aliases)

        await service.update(
            scenario.project_admin,
            binding.id,
            UpdateGroupBinding(
                expected_revision=binding.revision,
                agent_asset_id=second_agent.id,
                agent_scope="project",
            ),
        )
        # Simulate an in-flight Agent A request resuming at the mapping write
        # boundary after the Admin has switched the binding to Agent B. The
        # repository must reject the stale Thread instead of recreating the
        # mapping that the Agent change just removed.
        assert (
            await connection_repository.set_thread_id(
                scope=guest_scope,
                connection_id=str(sender_a_first["id"]),
                provider="feishu",
                external_conversation_id=str(
                    sender_a_first["resolved_conversation_id"],
                ),
                external_topic_id=str(sender_a_first["resolved_topic_id"]),
                thread_id=thread_id,
            )
            is False
        )
        async with scenario.session_factory() as session:
            changed_connections = tuple(
                (
                    await session.execute(
                        select(ChannelConnectionRow).where(
                            ChannelConnectionRow.project_id == scenario.project_admin.project_id,
                            ChannelConnectionRow.channel_instance_id == instance_id,
                        )
                    )
                ).scalars()
            )
            stale_conversations = tuple(
                (
                    await session.execute(
                        select(ChannelConversationRow).where(
                            ChannelConversationRow.project_id == scenario.project_admin.project_id,
                            ChannelConversationRow.connection_id.in_(tuple(connection.id for connection in changed_connections)),
                        )
                    )
                ).scalars()
            )
        assert changed_connections
        assert all(connection.metadata_json["agent_asset_id"] == str(second_agent.id) and connection.metadata_json["agent_scope"] == "project" for connection in changed_connections)
        assert stale_conversations == ()

        rebound_thread_id = str(uuid.uuid4())
        async with scenario.session_factory() as session, session.begin():
            await PrivateThreadRepository(session).create(
                scope=guest_scope,
                thread_id=rebound_thread_id,
                agent=ThreadAgentRef(second_agent.id, "project"),
                metadata={"source": "channel"},
            )
        await feishu._remember_thread_mapping(
            OutboundMessage(
                channel_name="feishu",
                channel_instance_id=str(instance_id),
                chat_id=raw_group_id,
                thread_id=rebound_thread_id,
                text="done",
                thread_ts="om_rebind_source_pg_sentinel",
                connection_id=str(sender_a_first["id"]),
                private_scope=guest_scope,
                resolved_conversation_id=str(
                    sender_a_first["resolved_conversation_id"],
                ),
                resolved_topic_id=str(sender_a_first["resolved_topic_id"]),
                metadata={},
            ),
            "om_rebind_source_pg_sentinel",
        )

        rebind_code = "bind-project-rebind-code-1234567890"
        rebind_service = ProjectChannelGroupBindingService(
            scenario.session_factory,
            identity_hasher=identity_hasher,
            clock=lambda: _NOW + timedelta(seconds=1),
            code_factory=lambda: rebind_code,
        )
        rebind_challenge = await rebind_service.create_challenge(
            scenario.project_admin,
            CreateGroupBindingChallenge(
                provider="feishu",
                agent_asset_id=scenario.project_agent_id,
                agent_scope="project",
            ),
        )
        assert rebind_challenge.command == f"/bind-project {rebind_code}"
        rebound = await rebind_service.complete_challenge(
            provider="feishu",
            channel_instance_id=instance_id,
            code=rebind_code,
            chat_id=raw_group_id,
            sender_id=raw_sender_a,
            display_name="研发群",
        )
        async with scenario.session_factory() as session:
            rebound_connections = tuple(
                (
                    await session.execute(
                        select(ChannelConnectionRow).where(
                            ChannelConnectionRow.project_id == scenario.project_admin.project_id,
                            ChannelConnectionRow.channel_instance_id == instance_id,
                        )
                    )
                ).scalars()
            )
            rebound_conversations = tuple(
                (
                    await session.execute(
                        select(ChannelConversationRow).where(
                            ChannelConversationRow.project_id == scenario.project_admin.project_id,
                            ChannelConversationRow.connection_id.in_(tuple(connection.id for connection in rebound_connections)),
                        )
                    )
                ).scalars()
            )
        assert all(connection.metadata_json["agent_asset_id"] == str(scenario.project_agent_id) and connection.metadata_json["agent_scope"] == "project" for connection in rebound_connections)
        assert rebound_conversations == ()

        concurrent_sender = "ou_concurrent_first_guest_pg_sentinel"
        concurrent_guest, concurrent_agent_change = await asyncio.wait_for(
            asyncio.gather(
                rebind_service.resolve_or_create_guest(
                    provider="feishu",
                    channel_instance_id=instance_id,
                    chat_id=raw_group_id,
                    sender_id=concurrent_sender,
                    topic_id=raw_topic_id,
                ),
                service.update(
                    scenario.project_admin,
                    binding.id,
                    UpdateGroupBinding(
                        expected_revision=rebound.revision,
                        agent_asset_id=second_agent.id,
                        agent_scope="project",
                    ),
                ),
            ),
            timeout=10,
        )
        async with scenario.session_factory() as session:
            concurrent_connection = await session.get(
                ChannelConnectionRow,
                str(concurrent_guest["id"]),
            )
        assert concurrent_connection is not None
        assert concurrent_connection.metadata_json["agent_asset_id"] == str(
            second_agent.id,
        )
        assert concurrent_connection.metadata_json["agent_scope"] == "project"

        disabled = await service.update(
            scenario.project_admin,
            binding.id,
            UpdateGroupBinding(
                expected_revision=concurrent_agent_change.revision,
                enabled=False,
            ),
        )
        assert disabled.status == "disabled"
        with pytest.raises(GroupBindingNotFound):
            await service.resolve_or_create_guest(
                provider="feishu",
                channel_instance_id=instance_id,
                chat_id=raw_group_id,
                sender_id=raw_sender_a,
            )
        async with scenario.session_factory() as session:
            frozen_principals = tuple((await session.execute(select(ChannelExternalPrincipalRow).where(ChannelExternalPrincipalRow.group_binding_id == binding.id))).scalars())
            frozen_connections = tuple(
                (
                    await session.execute(
                        select(ChannelConnectionRow).where(
                            ChannelConnectionRow.project_id == scenario.project_admin.project_id,
                            ChannelConnectionRow.channel_instance_id == instance_id,
                        )
                    )
                ).scalars()
            )
        assert all(principal.status == "frozen" for principal in frozen_principals)
        assert all(connection.status == "frozen" and connection.frozen_at == _NOW for connection in frozen_connections)

        enabled = await service.update(
            scenario.project_admin,
            binding.id,
            UpdateGroupBinding(expected_revision=disabled.revision, enabled=True),
        )
        sender_a_after_enable = await service.resolve_or_create_guest(
            provider="feishu",
            channel_instance_id=instance_id,
            chat_id=raw_group_id,
            sender_id=raw_sender_a,
        )
        assert sender_a_after_enable["id"] == sender_a_first["id"]
        assert sender_a_after_enable["owner_user_id"] == sender_a_first["owner_user_id"]
        assert sender_a_after_enable["status"] == "connected"

        await service.delete(
            scenario.project_admin,
            binding.id,
            expected_revision=enabled.revision,
        )
        with pytest.raises(GroupBindingNotFound):
            await service.resolve_or_create_guest(
                provider="feishu",
                channel_instance_id=instance_id,
                chat_id=raw_group_id,
                sender_id=raw_sender_a,
            )
        async with scenario.session_factory() as session:
            deleted = await session.get(ProjectChannelGroupBindingRow, binding.id)
            final_connections = tuple(
                (
                    await session.execute(
                        select(ChannelConnectionRow).where(
                            ChannelConnectionRow.project_id == scenario.project_admin.project_id,
                            ChannelConnectionRow.channel_instance_id == instance_id,
                        )
                    )
                ).scalars()
            )
        assert deleted is not None
        assert deleted.deleted_at == _NOW
        assert deleted.status == "disabled"
        assert all(connection.status == "frozen" for connection in final_connections)
    finally:
        await scenario.close()
