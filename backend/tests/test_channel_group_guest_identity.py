from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.gateway.auth.models import User
from app.gateway.auth.repositories.base import UserNotFoundError
from app.gateway.auth.repositories.sql import SQLUserRepository
from app.projects.capabilities import Capability
from app.projects.context import _project_context_from_values
from app.projects.errors import ProjectNotFound
from app.projects.membership_repository import MembershipRepository
from app.projects.models import ProjectRole
from app.quotas.reconciliation import QuotaReconciler
from deerflow.persistence.channel_connections import (
    ChannelExternalPrincipalRow,
    ProjectChannelGroupBindingChallengeRow,
    ProjectChannelGroupBindingRow,
)
from deerflow.persistence.projects.model import ProjectMembershipRow
from deerflow.persistence.user.model import UserRow


def _constraint_names(table) -> set[str]:
    return {item.name for item in table.constraints if item.name is not None}


def _index_names(table) -> set[str]:
    return {item.name for item in table.indexes if item.name is not None}


def test_channel_guest_user_shape_is_non_login_and_database_enforced() -> None:
    table = UserRow.__table__

    assert table.c.email.nullable is True
    assert table.c.principal_type.nullable is False
    assert str(table.c.principal_type.server_default.arg) == "'human'"
    assert {
        "ck_users_principal_type",
        "ck_users_channel_guest_identity",
        "ck_users_oauth_identity_shape",
        "uq_users_id_principal_type",
    } <= _constraint_names(table)
    email_index = next(item for item in table.indexes if item.name == "ix_users_email")
    assert email_index.unique is True
    assert "email IS NOT NULL" in str(email_index.dialect_options["postgresql"]["where"])

    guest = UserRow(
        id="00000000-0000-0000-0000-000000000001",
        email=None,
        password_hash=None,
        principal_type="channel_guest",
        system_role="user",
    )
    with pytest.raises(UserNotFoundError):
        SQLUserRepository._row_to_user(guest)

    with pytest.raises(ValidationError):
        User(email="guest@example.invalid", principal_type="channel_guest")


def test_group_binding_and_external_principal_tables_have_private_identity_closure() -> None:
    challenge = ProjectChannelGroupBindingChallengeRow.__table__
    binding = ProjectChannelGroupBindingRow.__table__
    principal = ChannelExternalPrincipalRow.__table__

    assert challenge.name == "project_channel_group_binding_challenges"
    assert {
        "pk_project_channel_group_binding_challenges",
        "uq_project_channel_group_binding_challenges_code_digest",
        "ck_project_channel_group_binding_challenges_digest",
        "fk_project_channel_group_binding_challenges_instance",
        "fk_project_channel_group_binding_challenges_membership",
        "fk_project_channel_group_binding_challenges_creator_membership",
        "fk_project_channel_group_binding_challenges_agent",
    } <= _constraint_names(challenge)
    assert {
        "ix_project_channel_group_binding_challenges_pending",
        "ix_project_channel_group_binding_challenges_membership",
    } <= _index_names(challenge)

    assert binding.name == "project_channel_group_bindings"
    assert set(binding.c.keys()) == {
        "id",
        "project_id",
        "channel_instance_id",
        "provider",
        "external_group_ref",
        "external_group_name",
        "agent_scope",
        "agent_asset_id",
        "status",
        "revision",
        "created_by_user_id",
        "updated_by_user_id",
        "first_activity_at",
        "last_activity_at",
        "created_at",
        "updated_at",
        "deleted_at",
    }
    assert {
        "ck_project_channel_group_bindings_provider",
        "ck_project_channel_group_bindings_external_ref",
        "ck_project_channel_group_bindings_agent_scope",
        "ck_project_channel_group_bindings_status",
        "ck_project_channel_group_bindings_revision",
        "uq_project_channel_group_bindings_project_id",
    } <= _constraint_names(binding)
    assert {
        "uq_project_channel_group_bindings_live_group",
        "ix_project_channel_group_bindings_project_status",
    } <= _index_names(binding)
    assert any(
        isinstance(item, ForeignKeyConstraint) and item.name == "fk_project_channel_group_bindings_instance" and {element.parent.name for element in item.elements} == {"project_id", "channel_instance_id", "provider"}
        for item in binding.constraints
    )

    assert principal.name == "channel_external_principals"
    assert {
        "principal_type",
        "membership_role",
        "external_account_ref",
        "principal_user_id",
        "membership_id",
    } <= set(principal.c.keys())
    assert {
        "ck_channel_external_principals_external_ref",
        "ck_channel_external_principals_type",
        "ck_channel_external_principals_membership_role",
        "ck_channel_external_principals_status",
        "uq_channel_external_principals_group_account",
    } <= _constraint_names(principal)
    assert any(
        isinstance(item, ForeignKeyConstraint) and item.name == "fk_channel_external_principals_guest_user" and {element.parent.name for element in item.elements} == {"principal_user_id", "principal_type"} for item in principal.constraints
    )
    assert any(
        isinstance(item, ForeignKeyConstraint)
        and item.name == "fk_channel_external_principals_guest_membership"
        and {element.parent.name for element in item.elements} == {"project_id", "principal_user_id", "membership_id", "membership_role"}
        for item in principal.constraints
    )

    assert any(isinstance(item, UniqueConstraint) and item.name == "uq_project_memberships_guest_identity" for item in ProjectMembershipRow.__table__.constraints)
    assert any(isinstance(item, (CheckConstraint, Index)) for item in principal.constraints | principal.indexes)


def test_full_schema_declares_guest_identity_and_group_tables() -> None:
    schema = (Path(__file__).parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql").read_text(encoding="utf-8")

    assert "principal_type VARCHAR(16) DEFAULT 'human' NOT NULL" in schema
    assert "CREATE TABLE project_channel_group_bindings" in schema
    assert "CREATE TABLE project_channel_group_binding_challenges" in schema
    assert "CREATE TABLE channel_external_principals" in schema
    assert "role IN ('admin', 'editor', 'runner', 'viewer', 'channel_guest')" in schema
    assert "CREATE UNIQUE INDEX uq_project_channel_group_bindings_live_group" in schema


def test_guest_and_group_metadata_compiles_for_postgresql() -> None:
    import deerflow.persistence.models  # noqa: F401

    for table in (
        UserRow.__table__,
        ProjectMembershipRow.__table__,
        ProjectChannelGroupBindingChallengeRow.__table__,
        ProjectChannelGroupBindingRow.__table__,
        ChannelExternalPrincipalRow.__table__,
    ):
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert f"CREATE TABLE {table.name}" in ddl


def test_channel_guest_is_not_a_public_project_role() -> None:
    assert ProjectRole.CHANNEL_GUEST.value == "channel_guest"


def test_membership_view_rejects_channel_guest_even_if_called_directly() -> None:
    guest = UserRow(
        id=str(uuid.uuid4()),
        email=None,
        password_hash=None,
        principal_type="channel_guest",
        system_role="user",
    )
    membership = SimpleNamespace(
        id=uuid.uuid4(),
        role="channel_guest",
        status="active",
        version=1,
        created_at=datetime.now(UTC),
    )

    with pytest.raises(ProjectNotFound):
        MembershipRepository._view(membership, guest)


def test_resolved_channel_guest_context_has_only_runtime_capabilities() -> None:
    context = _project_context_from_values(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role_value="channel_guest",
        membership_version=1,
        request_id="channel-guest",
    )

    assert context.role is ProjectRole.CHANNEL_GUEST
    assert context.capabilities == frozenset(
        {
            Capability.PRIVATE_WORK_CREATE,
            Capability.PRIVATE_WORK_READ_OWN,
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    )
    assert Capability.PROJECT_ENTER not in context.capabilities
    assert Capability.PROJECT_CHANNELS_MANAGE not in context.capabilities
    assert Capability.AUTOMATION_MANAGE_OWN not in context.capabilities


def test_channel_guest_can_execute_private_runs_but_not_automations() -> None:
    from app.automations.execution_authority import (
        _EXECUTABLE_ROLES as automation_roles,
    )
    from app.private_work.authorization import (
        _EXECUTABLE_ROLES as private_run_roles,
    )
    from deerflow.runtime.events.store.db import (
        _EXECUTABLE_ROLES as stream_roles,
    )

    assert ProjectRole.CHANNEL_GUEST.value in private_run_roles
    assert ProjectRole.CHANNEL_GUEST.value in stream_roles
    assert ProjectRole.CHANNEL_GUEST.value not in automation_roles


class _EmptyResult:
    @staticmethod
    def scalar_one_or_none():
        return None

    def scalars(self):
        return self

    @staticmethod
    def first():
        return None


class _AuthStatementSession:
    def __init__(self) -> None:
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _EmptyResult()

    async def scalar(self, statement):
        self.statements.append(statement)
        return 0


class _NoRowsMembershipSession:
    def __init__(self) -> None:
        self.statements = []

    @asynccontextmanager
    async def begin(self):
        yield

    async def execute(self, statement):
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: [])


@pytest.mark.asyncio
async def test_auth_repository_id_email_and_oauth_lookups_are_human_only() -> None:
    session = _AuthStatementSession()

    @asynccontextmanager
    async def session_factory():
        yield session

    repository = SQLUserRepository(session_factory)  # type: ignore[arg-type]
    assert await repository.get_user_by_id(str(uuid.uuid4())) is None
    assert await repository.get_user_by_email("member@example.com") is None
    assert await repository.get_user_by_oauth("oidc", "subject") is None
    assert await repository.count_users() == 0
    assert await repository.count_admin_users() == 0

    compiled = [str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})) for statement in session.statements]
    assert len(compiled) == 5
    assert all("users.principal_type = 'human'" in statement for statement in compiled)


@pytest.mark.asyncio
async def test_member_quota_reconciliation_excludes_channel_guests() -> None:
    session = AsyncMock()
    session.scalar.return_value = 0
    reconciler = object.__new__(QuotaReconciler)

    assert (
        await reconciler._expected(
            session,
            uuid.uuid4(),
            "members",
            "lifetime",
        )
        == 0
    )
    statement = session.scalar.await_args.args[0]
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "project_memberships.role != 'channel_guest'" in compiled


@pytest.mark.asyncio
async def test_public_member_listing_filters_guest_principals_in_sql() -> None:
    session = _NoRowsMembershipSession()
    repository = MembershipRepository(session)  # type: ignore[arg-type]
    context = _project_context_from_values(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role_value="admin",
        membership_version=1,
        request_id="list-members",
    )

    with pytest.raises(ProjectNotFound):
        await repository.list_members(context)

    assert len(session.statements) == 1
    compiled = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "project_memberships.role != 'channel_guest'" in compiled
    assert "users.principal_type = 'human'" in compiled
