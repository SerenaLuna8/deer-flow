from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from support.m3_shared_assets import M3Scenario

from app.private_work.retention_purge import purge_project_channel_guest_scope
from deerflow.persistence.channel_connections.group_challenge_model import (
    ProjectChannelGroupBindingChallengeRow,
)
from deerflow.persistence.channel_connections.group_model import (
    ChannelExternalPrincipalRow,
    ProjectChannelGroupBindingRow,
)
from deerflow.persistence.channel_connections.model import (
    ChannelConnectionRow,
    ProjectChannelInstanceRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.user.model import UserRow


@pytest.mark.anyio
async def test_project_guest_purge_is_project_scoped_and_removes_orphan_guest_identity() -> None:
    project_id = uuid.uuid4()
    guest_user_id = str(uuid.uuid4())
    membership_id = uuid.uuid4()
    statements: list[str] = []

    class _Rows:
        def all(self):
            return [
                SimpleNamespace(
                    membership_id=membership_id,
                    user_id=guest_user_id,
                )
            ]

    class _Result:
        def all(self):
            return []

    class _Session:
        calls = 0

        async def execute(self, statement, _parameters=None):
            statements.append(str(statement))
            self.calls += 1
            if self.calls == 1:
                return _Rows()
            return _Result()

        @asynccontextmanager
        async def begin_nested(self):
            yield

        async def flush(self):
            return None

    await purge_project_channel_guest_scope(
        _Session(),  # type: ignore[arg-type]
        project_id=project_id,
    )

    joined = "\n".join(statements)
    assert "project_memberships.role = :role_1" in statements[0]
    assert "project_memberships.project_id = :project_id_1" in statements[0]
    assert "DELETE FROM channel_oauth_states WHERE project_id=:project_id" in joined
    assert "DELETE FROM channel_conversations WHERE project_id=:project_id" in joined
    assert "DELETE FROM channel_connections WHERE project_id=:project_id" in joined
    assert "DELETE FROM project_channel_group_binding_challenges WHERE project_id=:project_id" in joined
    assert "DELETE FROM channel_external_principals WHERE project_id=:project_id" in joined
    assert "DELETE FROM project_channel_group_bindings WHERE project_id=:project_id" in joined
    assert "DELETE FROM project_memberships" in joined
    assert "project_memberships.role = :role_1" in joined
    assert "DELETE FROM users" in joined
    assert "users.principal_type = :principal_type_1" in joined
    assert "NOT (EXISTS (SELECT project_memberships.id" in joined


@pytest.mark.anyio
async def test_project_shared_purge_starts_with_guest_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.private_work import retention_purge

    project_id = uuid.uuid4()
    calls: list[str] = []

    async def _guest(*_args, **_kwargs):
        calls.append("guest")

    class _Stop(RuntimeError):
        pass

    class _Session:
        async def execute(self, *_args, **_kwargs):
            calls.append("shared")
            raise _Stop

    monkeypatch.setattr(
        retention_purge,
        "purge_project_channel_guest_scope",
        _guest,
    )

    with pytest.raises(_Stop):
        await retention_purge.purge_project_shared_scope(
            _Session(),  # type: ignore[arg-type]
            project_id=project_id,
        )

    assert calls[:2] == ["guest", "shared"]


@pytest.mark.postgres
@pytest.mark.anyio
async def test_project_guest_purge_preserves_human_and_other_project(
    migrated_postgres_database_url: str,
) -> None:
    scenario = await M3Scenario.create(migrated_postgres_database_url)
    try:
        await scenario.bootstrap_system_catalog()
        assert scenario.system_agent_id is not None
        now = datetime.now(UTC)
        target_project_id = scenario.project_admin.project_id
        other_project_id = scenario.other_project_admin.project_id
        target_admin_id = str(scenario.project_admin.user_id)
        other_admin_id = str(scenario.other_project_admin.user_id)
        target_guest_id = str(uuid.uuid4())
        other_guest_id = str(uuid.uuid4())
        target_membership_id = uuid.uuid4()
        other_membership_id = uuid.uuid4()
        target_instance_id = uuid.uuid4()
        other_instance_id = uuid.uuid4()
        target_binding_id = uuid.uuid4()
        other_binding_id = uuid.uuid4()
        target_principal_id = uuid.uuid4()
        other_principal_id = uuid.uuid4()
        target_challenge_id = uuid.uuid4()

        async with scenario.session_factory() as session, session.begin():
            for guest_id in (target_guest_id, other_guest_id):
                session.add(
                    UserRow(
                        id=guest_id,
                        email=None,
                        password_hash=None,
                        principal_type="channel_guest",
                        system_role="user",
                        oauth_provider=None,
                        oauth_id=None,
                        needs_setup=False,
                        token_version=0,
                    )
                )
            await session.flush()
            session.add_all(
                [
                    ProjectMembershipRow(
                        id=target_membership_id,
                        project_id=target_project_id,
                        user_id=target_guest_id,
                        role="channel_guest",
                    ),
                    ProjectMembershipRow(
                        id=other_membership_id,
                        project_id=other_project_id,
                        user_id=other_guest_id,
                        role="channel_guest",
                    ),
                ]
            )
            session.add_all(
                [
                    ProjectChannelInstanceRow(
                        id=target_instance_id,
                        project_id=target_project_id,
                        provider="feishu",
                        display_name="Target Feishu",
                        desired_status="enabled",
                        observed_status="running",
                        public_config={},
                        provider_identity_digest="1" * 64,
                        created_by_user_id=target_admin_id,
                        updated_by_user_id=target_admin_id,
                    ),
                    ProjectChannelInstanceRow(
                        id=other_instance_id,
                        project_id=other_project_id,
                        provider="feishu",
                        display_name="Other Feishu",
                        desired_status="enabled",
                        observed_status="running",
                        public_config={},
                        provider_identity_digest="2" * 64,
                        created_by_user_id=other_admin_id,
                        updated_by_user_id=other_admin_id,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ProjectChannelGroupBindingRow(
                        id=target_binding_id,
                        project_id=target_project_id,
                        channel_instance_id=target_instance_id,
                        provider="feishu",
                        external_group_ref="3" * 64,
                        external_group_name="Target Group",
                        agent_scope="system",
                        agent_asset_id=scenario.system_agent_id,
                        created_by_user_id=target_admin_id,
                        updated_by_user_id=target_admin_id,
                    ),
                    ProjectChannelGroupBindingRow(
                        id=other_binding_id,
                        project_id=other_project_id,
                        channel_instance_id=other_instance_id,
                        provider="feishu",
                        external_group_ref="4" * 64,
                        external_group_name="Other Group",
                        agent_scope="system",
                        agent_asset_id=scenario.system_agent_id,
                        created_by_user_id=other_admin_id,
                        updated_by_user_id=other_admin_id,
                    ),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ChannelExternalPrincipalRow(
                        id=target_principal_id,
                        project_id=target_project_id,
                        group_binding_id=target_binding_id,
                        external_account_ref="5" * 64,
                        principal_user_id=target_guest_id,
                        membership_id=target_membership_id,
                    ),
                    ChannelExternalPrincipalRow(
                        id=other_principal_id,
                        project_id=other_project_id,
                        group_binding_id=other_binding_id,
                        external_account_ref="6" * 64,
                        principal_user_id=other_guest_id,
                        membership_id=other_membership_id,
                    ),
                    ProjectChannelGroupBindingChallengeRow(
                        id=target_challenge_id,
                        project_id=target_project_id,
                        channel_instance_id=target_instance_id,
                        provider="feishu",
                        code_digest="7" * 64,
                        agent_asset_id=scenario.system_agent_id,
                        agent_scope="system",
                        membership_id=scenario.project_admin.membership_id,
                        membership_version=scenario.project_admin.membership_version,
                        created_by_user_id=target_admin_id,
                        expires_at=now + timedelta(hours=1),
                    ),
                ]
            )
            session.add_all(
                [
                    ChannelConnectionRow(
                        id=target_principal_id.hex,
                        project_id=target_project_id,
                        owner_user_id=target_guest_id,
                        provider="feishu",
                        channel_instance_id=target_instance_id,
                        status="connected",
                        external_account_id="5" * 64,
                        workspace_id="3" * 64,
                        scopes_json=[],
                        capabilities_json={},
                        metadata_json={"group_binding_id": str(target_binding_id)},
                        created_at=now,
                        updated_at=now,
                    ),
                    ChannelConnectionRow(
                        id=f"human-{uuid.uuid4().hex}",
                        project_id=target_project_id,
                        owner_user_id=target_admin_id,
                        provider="feishu",
                        channel_instance_id=target_instance_id,
                        status="connected",
                        external_account_id="human-account",
                        workspace_id="human-workspace",
                        scopes_json=[],
                        capabilities_json={},
                        metadata_json={},
                        created_at=now,
                        updated_at=now,
                    ),
                    ChannelConnectionRow(
                        id=other_principal_id.hex,
                        project_id=other_project_id,
                        owner_user_id=other_guest_id,
                        provider="feishu",
                        channel_instance_id=other_instance_id,
                        status="connected",
                        external_account_id="6" * 64,
                        workspace_id="4" * 64,
                        scopes_json=[],
                        capabilities_json={},
                        metadata_json={"group_binding_id": str(other_binding_id)},
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )

        async with scenario.session_factory() as session, session.begin():
            await purge_project_channel_guest_scope(
                session,
                project_id=target_project_id,
            )

        async with scenario.session_factory() as session:
            assert (
                await session.get(
                    ProjectChannelGroupBindingChallengeRow,
                    target_challenge_id,
                )
                is None
            )
            assert await session.get(ProjectChannelGroupBindingRow, target_binding_id) is None
            assert await session.get(ChannelExternalPrincipalRow, target_principal_id) is None
            assert await session.get(ProjectMembershipRow, target_membership_id) is None
            assert await session.get(UserRow, target_guest_id) is None
            assert await session.get(ChannelConnectionRow, target_principal_id.hex) is None
            assert (
                await session.scalar(
                    select(ChannelConnectionRow).where(
                        ChannelConnectionRow.project_id == target_project_id,
                        ChannelConnectionRow.owner_user_id == target_admin_id,
                    )
                )
                is not None
            )
            assert await session.get(ProjectChannelGroupBindingRow, other_binding_id) is not None
            assert await session.get(ChannelExternalPrincipalRow, other_principal_id) is not None
            assert await session.get(ProjectMembershipRow, other_membership_id) is not None
            assert await session.get(UserRow, other_guest_id) is not None
            assert await session.get(ChannelConnectionRow, other_principal_id.hex) is not None
    finally:
        await scenario.close()
