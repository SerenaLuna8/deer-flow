from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.channel_group_bindings.errors import GroupBindingConflict
from app.channel_group_bindings.repository import (
    GroupBindingRepositoryConflict,
    PostgresProjectChannelGroupBindingRepository,
)
from app.channel_group_bindings.service import ProjectChannelGroupBindingService
from app.private_work.account_private_lifecycle import AccountPrivateGeneration


class _LifecycleReached(RuntimeError):
    pass


class _Result:
    def __init__(self, *, scalar: object = None, rows: tuple[object, ...] = ()) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def all(self) -> list[object]:
        return list(self._rows)


class _ExistingGuestLifecycle:
    def __init__(self, events: list[str], owner_user_id: str) -> None:
        self._events = events
        self._owner_user_id = owner_user_id

    async def reactivate_after_membership(self, session, owner_user_id):
        assert session.in_transaction()
        assert owner_user_id == self._owner_user_id
        assert self._events == ["pre-read", "project", "membership"]
        self._events.append("user")
        raise _LifecycleReached


class _ContinuingExistingGuestLifecycle:
    def __init__(self, events: list[str], owner_user_id: str) -> None:
        self._events = events
        self._owner_user_id = owner_user_id

    async def reactivate_after_membership(
        self,
        session,
        owner_user_id,
    ) -> AccountPrivateGeneration:
        assert session.in_transaction()
        assert owner_user_id == self._owner_user_id
        assert self._events == ["pre-read", "project", "membership"]
        self._events.append("user")
        return AccountPrivateGeneration(owner_user_id=owner_user_id, generation=4)


class _ExistingGuestSession:
    def __init__(self) -> None:
        self.project_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
        self.instance_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
        self.binding_id = uuid.UUID("33333333-3333-4333-8333-333333333333")
        self.agent_id = uuid.UUID("44444444-4444-4444-8444-444444444444")
        self.principal_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
        self.connection_id = self.principal_id.hex
        self.membership_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
        self.owner_user_id = "77777777-7777-4777-8777-777777777777"
        self.group_ref = "1" * 64
        self.account_ref = "2" * 64
        self.events: list[str] = []

    def in_transaction(self) -> bool:
        return True

    async def execute(self, statement, *_args, **_kwargs) -> _Result:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FROM project_channel_group_bindings" in sql and "FOR " not in sql:
            self.events.append("pre-read")
            snapshot = SimpleNamespace(
                project_id=self.project_id,
                channel_instance_revision=4,
                binding_id=self.binding_id,
                external_group_ref=self.group_ref,
                binding_revision=3,
                agent_asset_id=self.agent_id,
                agent_scope="project",
                principal_id=self.principal_id,
                principal_user_id=self.owner_user_id,
                membership_id=self.membership_id,
                external_account_ref=self.account_ref,
                principal_status="active",
                membership_status="active",
                membership_role="channel_guest",
                membership_version=5,
                membership_activation_generation=9,
                connection_id=self.connection_id,
                connection_project_id=self.project_id,
                connection_owner_user_id=self.owner_user_id,
                connection_instance_id=self.instance_id,
                connection_status="connected",
            )
            return _Result(scalar=self.project_id, rows=(snapshot,))
        if "FROM projects" in sql:
            self.events.append("project")
            return _Result(scalar=self.project_id)
        if "FROM project_memberships" in sql:
            self.events.append("membership")
            return _Result(
                scalar=SimpleNamespace(
                    id=self.membership_id,
                    version=5,
                    activation_generation=9,
                ),
            )
        if "FROM project_channel_instances" in sql:
            self.events.append("instance")
            return _Result(scalar=self.instance_id)
        if "FROM project_channel_group_bindings" in sql and "FOR " in sql:
            self.events.append("binding")
            return _Result(
                scalar=SimpleNamespace(
                    id=self.binding_id,
                    project_id=self.project_id,
                    channel_instance_id=self.instance_id,
                    provider="lark",
                    external_group_ref=self.group_ref,
                    agent_asset_id=self.agent_id,
                    agent_scope="project",
                ),
            )
        if "FROM agents" in sql:
            self.events.append("agent")
            return _Result(scalar=self.agent_id)
        if "pg_advisory_xact_lock" in sql:
            self.events.append("identity")
            return _Result()
        if "FROM channel_external_principals" in sql:
            self.events.append("principal")
            return _Result(scalar=None)
        raise AssertionError(sql)


class _NewGuestRaceSession(_ExistingGuestSession):
    async def execute(self, statement, *_args, **_kwargs) -> _Result:
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FROM project_channel_group_bindings" in sql and "FOR " not in sql:
            self.events.append("pre-read")
            snapshot = SimpleNamespace(
                project_id=self.project_id,
                channel_instance_revision=4,
                binding_id=self.binding_id,
                external_group_ref=self.group_ref,
                binding_revision=3,
                agent_asset_id=self.agent_id,
                agent_scope="project",
                principal_id=None,
                principal_user_id=None,
                membership_id=None,
                external_account_ref=None,
                principal_status=None,
                membership_status=None,
                membership_role=None,
                membership_version=None,
                membership_activation_generation=None,
                connection_id=None,
                connection_project_id=None,
                connection_owner_user_id=None,
                connection_instance_id=None,
                connection_status=None,
            )
            return _Result(rows=(snapshot,))
        if "FROM channel_external_principals" in sql:
            self.events.append("principal-appeared")
            return _Result(scalar=SimpleNamespace(id=self.principal_id))
        return await super().execute(statement, *_args, **_kwargs)


class _NewGuestMustNotUseExistingLifecycle:
    async def reactivate_after_membership(self, *_args, **_kwargs):
        raise AssertionError("new guest must not use existing-account lifecycle")


class _ServiceTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args) -> None:
        return None


class _ServiceSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def begin(self) -> _ServiceTransaction:
        return _ServiceTransaction()


class _ConflictRepository:
    async def resolve_or_create_guest(self, *_args, **_kwargs):
        raise GroupBindingRepositoryConflict


class _IdentityHasher:
    def group_refs(self, *_args) -> tuple[str, ...]:
        return ("1" * 64,)

    def account_refs(self, *_args) -> tuple[str, ...]:
        return ("2" * 64,)


@pytest.mark.asyncio
async def test_existing_guest_locks_project_membership_user_before_channel_suffix() -> None:
    session = _ExistingGuestSession()
    lifecycle = _ExistingGuestLifecycle(session.events, session.owner_user_id)
    repository = PostgresProjectChannelGroupBindingRepository(
        account_private_lifecycle=lifecycle,
    )

    with pytest.raises(_LifecycleReached):
        await repository.resolve_or_create_guest(
            session,  # type: ignore[arg-type]
            provider="lark",
            channel_instance_id=session.instance_id,
            external_group_refs=(session.group_ref,),
            external_account_refs=(session.account_ref,),
            now=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert session.events == ["pre-read", "project", "membership", "user"]


@pytest.mark.asyncio
async def test_existing_guest_accepts_asyncpg_uuid_subclasses_at_discovery_boundary() -> None:
    database_uuid_type = type("DatabaseUUID", (uuid.UUID,), {})
    session = _ExistingGuestSession()
    for field in (
        "project_id",
        "instance_id",
        "binding_id",
        "agent_id",
        "principal_id",
        "membership_id",
    ):
        value = getattr(session, field)
        setattr(session, field, database_uuid_type(hex=value.hex))
    lifecycle = _ExistingGuestLifecycle(session.events, session.owner_user_id)
    repository = PostgresProjectChannelGroupBindingRepository(
        account_private_lifecycle=lifecycle,
    )

    with pytest.raises(_LifecycleReached):
        await repository.resolve_or_create_guest(
            session,  # type: ignore[arg-type]
            provider="lark",
            channel_instance_id=session.instance_id,
            external_group_refs=(session.group_ref,),
            external_account_refs=(session.account_ref,),
            now=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert session.events == ["pre-read", "project", "membership", "user"]


@pytest.mark.asyncio
async def test_existing_guest_coordinate_change_fails_without_relocking_account_prefix() -> None:
    session = _ExistingGuestSession()
    lifecycle = _ContinuingExistingGuestLifecycle(
        session.events,
        session.owner_user_id,
    )
    repository = PostgresProjectChannelGroupBindingRepository(
        account_private_lifecycle=lifecycle,
    )

    with pytest.raises(GroupBindingRepositoryConflict):
        await repository.resolve_or_create_guest(
            session,  # type: ignore[arg-type]
            provider="lark",
            channel_instance_id=session.instance_id,
            external_group_refs=(session.group_ref,),
            external_account_refs=(session.account_ref,),
            now=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert session.events == [
        "pre-read",
        "project",
        "membership",
        "user",
        "instance",
        "binding",
        "agent",
        "identity",
        "principal",
    ]
    assert "project" not in session.events[4:]
    assert "membership" not in session.events[4:]


@pytest.mark.asyncio
async def test_principal_appearing_after_new_guest_discovery_retries_via_existing_path() -> None:
    session = _NewGuestRaceSession()
    repository = PostgresProjectChannelGroupBindingRepository(
        account_private_lifecycle=_NewGuestMustNotUseExistingLifecycle(),
    )

    with pytest.raises(GroupBindingRepositoryConflict):
        await repository.resolve_or_create_guest(
            session,  # type: ignore[arg-type]
            provider="lark",
            channel_instance_id=session.instance_id,
            external_group_refs=(session.group_ref,),
            external_account_refs=(session.account_ref,),
            now=SimpleNamespace(),  # type: ignore[arg-type]
        )

    assert session.events == [
        "pre-read",
        "project",
        "instance",
        "binding",
        "agent",
        "identity",
        "principal-appeared",
    ]
    assert "membership" not in session.events
    assert "user" not in session.events


@pytest.mark.asyncio
async def test_inbound_service_exposes_coordinate_race_as_typed_conflict() -> None:
    service = ProjectChannelGroupBindingService(
        _ServiceSession,
        repository=_ConflictRepository(),
        identity_hasher=_IdentityHasher(),  # type: ignore[arg-type]
    )

    with pytest.raises(GroupBindingConflict):
        await service.resolve_or_create_guest(
            "lark",
            uuid.UUID("88888888-8888-4888-8888-888888888888"),
            "chat-1",
            "sender-1",
        )
