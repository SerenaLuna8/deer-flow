from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import CheckConstraint
from sqlalchemy.dialects import postgresql

from app.automations.dispatcher import (
    AutomationDefinitionRef,
    AutomationDispatcher,
)
from app.private_work.account_private_lifecycle import (
    AccountPrivateGeneration,
    AccountPrivateLifecycle,
    AccountPrivateLifecycleClosed,
    AccountPrivatePurgeFence,
    AccountPrivateScopeChanged,
    LockedAccountPrivateScope,
)
from app.private_work.memory_seal_service import MemorySealAdmissionService
from app.private_work.skill_builder_run_admission import (
    SkillBuilderRunAdmissionService,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.invitation_service import InvitationService
from app.projects.models import CreateProject, ProjectRole
from app.projects.repository import ProjectRepository
from deerflow.persistence.user.model import UserRow

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FULL_SCHEMA_PATH = BACKEND_ROOT / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def test_account_private_lifecycle_fields_match_the_schema_v1_snapshot() -> None:
    table = UserRow.__table__

    state = table.c.private_retention_state
    generation = table.c.private_retention_generation
    effective_at = table.c.private_retention_effective_at

    assert state.nullable is False
    assert state.default.arg == "active"
    assert str(state.server_default.arg) == "'active'"
    assert generation.nullable is False
    assert generation.default.arg == 1
    assert str(generation.server_default.arg) == "1"
    assert effective_at.nullable is True

    checks = {constraint.name: _normalized(constraint.sqltext) for constraint in table.constraints if isinstance(constraint, CheckConstraint)}
    assert checks["ck_users_private_retention_state"] == ("private_retention_state IN ('active', 'pending_deletion', 'purged')")
    assert checks["ck_users_private_retention_generation"] == ("private_retention_generation >= 1")
    assert checks["ck_users_private_retention_effective_at"] == (
        "(private_retention_state = 'pending_deletion' AND private_retention_effective_at IS NOT NULL) OR (private_retention_state IN ('active', 'purged') AND private_retention_effective_at IS NULL)"
    )

    schema = FULL_SCHEMA_PATH.read_text(encoding="utf-8")
    assert "private_retention_state VARCHAR(24) DEFAULT 'active' NOT NULL" in schema
    assert "private_retention_generation BIGINT DEFAULT 1 NOT NULL" in schema
    assert "private_retention_effective_at TIMESTAMP WITH TIME ZONE" in schema
    assert ("CONSTRAINT ck_users_private_retention_state CHECK (private_retention_state IN ('active', 'pending_deletion', 'purged'))") in schema
    assert ("CONSTRAINT ck_users_private_retention_generation CHECK (private_retention_generation >= 1)") in schema
    assert (
        "CONSTRAINT ck_users_private_retention_effective_at CHECK "
        "((private_retention_state = 'pending_deletion' AND "
        "private_retention_effective_at IS NOT NULL) OR "
        "(private_retention_state IN ('active', 'purged') AND "
        "private_retention_effective_at IS NULL))"
    ) in schema


class _LifecycleSession:
    def __init__(self, row: object | None) -> None:
        self.row = row
        self.statement = None

    def in_transaction(self) -> bool:
        return True

    async def scalar(self, statement):
        self.statement = statement
        return self.row


@pytest.mark.asyncio
async def test_active_account_private_lifecycle_returns_locked_generation() -> None:
    owner_user_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    session = _LifecycleSession(
        SimpleNamespace(
            id=str(owner_user_id),
            private_retention_state="active",
            private_retention_generation=7,
        )
    )

    generation = await AccountPrivateLifecycle().require_active_after_membership(
        session,  # type: ignore[arg-type]
        owner_user_id,
    )

    assert generation == AccountPrivateGeneration(
        owner_user_id=str(owner_user_id),
        generation=7,
    )
    assert session.statement is not None
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert sql.endswith("FOR SHARE OF users")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending_deletion", "purged"])
async def test_closed_account_private_lifecycle_fails_closed(state: str) -> None:
    owner_user_id = uuid.UUID("22222222-2222-4222-8222-222222222222")
    session = _LifecycleSession(
        SimpleNamespace(
            id=str(owner_user_id),
            private_retention_state=state,
            private_retention_generation=8,
        )
    )

    with pytest.raises(AccountPrivateLifecycleClosed):
        await AccountPrivateLifecycle().require_active_after_membership(
            session,  # type: ignore[arg-type]
            owner_user_id,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending_deletion", "purged"])
async def test_governance_reactivation_invalidates_old_account_purge_generation(
    state: str,
) -> None:
    owner_user_id = uuid.UUID("12121212-1212-4212-8212-121212121212")
    row = SimpleNamespace(
        id=str(owner_user_id),
        private_retention_state=state,
        private_retention_generation=12,
        private_retention_effective_at=(datetime.now(UTC) if state == "pending_deletion" else None),
    )
    session = _LifecycleSession(row)

    generation = await AccountPrivateLifecycle().reactivate_after_membership(
        session,  # type: ignore[arg-type]
        owner_user_id,
    )

    assert generation == AccountPrivateGeneration(
        owner_user_id=str(owner_user_id),
        generation=13,
    )
    assert row.private_retention_state == "active"
    assert row.private_retention_effective_at is None
    assert session.statement is not None
    sql = str(
        session.statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert sql.endswith("FOR NO KEY UPDATE OF users")


@pytest.mark.asyncio
async def test_account_purge_transition_is_generation_bound_and_idempotent() -> None:
    owner_user_id = uuid.UUID("13131313-1313-4313-8313-131313131313")
    project_id = uuid.UUID("14141414-1414-4414-8414-141414141414")
    membership_id = uuid.UUID("15151515-1515-4515-8515-151515151515")
    effective_at = datetime(2026, 8, 25, 3, 0, tzinfo=UTC)
    row = SimpleNamespace(
        id=str(owner_user_id),
        private_retention_state="active",
        private_retention_generation=6,
        private_retention_effective_at=None,
    )
    locked = LockedAccountPrivateScope(
        owner_user_id=str(owner_user_id),
        project_ids=(project_id,),
        membership_ids=(membership_id,),
        state="active",
        generation=6,
        _user_row=row,
    )
    session = _LifecycleSession(row)

    fence = await AccountPrivateLifecycle().begin_purge_after_memberships(
        session,  # type: ignore[arg-type]
        locked,
        effective_at=effective_at,
    )

    assert fence == AccountPrivatePurgeFence(
        owner_user_id=str(owner_user_id),
        generation=7,
        effective_at=effective_at,
        project_ids=(project_id,),
        membership_ids=(membership_id,),
    )
    assert row.private_retention_state == "pending_deletion"
    assert row.private_retention_generation == 7
    assert row.private_retention_effective_at == effective_at

    same_locked = LockedAccountPrivateScope(
        owner_user_id=str(owner_user_id),
        project_ids=(project_id,),
        membership_ids=(membership_id,),
        state="pending_deletion",
        generation=7,
        _user_row=row,
    )
    assert (
        await AccountPrivateLifecycle().begin_purge_after_memberships(
            session,  # type: ignore[arg-type]
            same_locked,
            effective_at=effective_at,
        )
        == fence
    )


class _ScalarRows:
    def __init__(self, values: tuple[uuid.UUID, ...]) -> None:
        self._values = values

    def all(self) -> list[uuid.UUID]:
        return list(self._values)


class _StableScopeSession:
    def __init__(
        self,
        *,
        project_ids: tuple[uuid.UUID, ...],
        membership_ids: tuple[uuid.UUID, ...],
        user_row: object,
    ) -> None:
        self.project_ids = project_ids
        self.membership_ids = membership_ids
        self.user_row = user_row
        self.events: list[str] = []

    def in_transaction(self) -> bool:
        return True

    async def scalars(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "FROM projects" in sql:
            self.events.append("lock-projects")
            return _ScalarRows(self.project_ids)
        if "FROM project_memberships" in sql:
            self.events.append("lock-memberships")
            return _ScalarRows(self.membership_ids)
        raise AssertionError(sql)

    async def scalar(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        assert "FROM users" in sql
        assert sql.endswith("FOR NO KEY UPDATE OF users")
        self.events.append("lock-user")
        return self.user_row


class _StableScopeLifecycle(AccountPrivateLifecycle):
    def __init__(
        self,
        snapshots: list[tuple[tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]],
    ) -> None:
        self.snapshots = snapshots

    async def _read_scope_coordinates(self, _session, _owner_user_id):
        return self.snapshots.pop(0)


@pytest.mark.asyncio
async def test_account_purge_locks_stable_scope_project_membership_user_order() -> None:
    owner_user_id = uuid.UUID("16161616-1616-4616-8616-161616161616")
    projects = (
        uuid.UUID("17171717-1717-4717-8717-171717171717"),
        uuid.UUID("18181818-1818-4818-8818-181818181818"),
    )
    memberships = (
        uuid.UUID("19191919-1919-4919-8919-191919191919"),
        uuid.UUID("20202020-2020-4020-8020-202020202020"),
    )
    row = SimpleNamespace(
        id=str(owner_user_id),
        private_retention_state="active",
        private_retention_generation=4,
    )
    session = _StableScopeSession(
        project_ids=projects,
        membership_ids=memberships,
        user_row=row,
    )
    lifecycle = _StableScopeLifecycle(
        [(projects, memberships), (projects, memberships)],
    )

    locked = await lifecycle.lock_stable_scope_for_purge(
        session,  # type: ignore[arg-type]
        owner_user_id,
    )

    assert locked.owner_user_id == str(owner_user_id)
    assert locked.project_ids == projects
    assert locked.membership_ids == memberships
    assert locked.state == "active"
    assert locked.generation == 4
    assert session.events == [
        "lock-projects",
        "lock-memberships",
        "lock-user",
    ]


@pytest.mark.asyncio
async def test_account_purge_retries_instead_of_locking_new_project_after_user() -> None:
    owner_user_id = uuid.UUID("21212121-2121-4121-8121-212121212121")
    project_a = uuid.UUID("22222222-2222-4222-8222-222222222220")
    project_b = uuid.UUID("22222222-2222-4222-8222-222222222221")
    membership_a = uuid.UUID("23232323-2323-4323-8323-232323232323")
    row = SimpleNamespace(
        id=str(owner_user_id),
        private_retention_state="active",
        private_retention_generation=4,
    )
    session = _StableScopeSession(
        project_ids=(project_a,),
        membership_ids=(membership_a,),
        user_row=row,
    )
    lifecycle = _StableScopeLifecycle(
        [
            ((project_a,), (membership_a,)),
            ((project_a, project_b), (membership_a,)),
        ],
    )

    with pytest.raises(AccountPrivateScopeChanged):
        await lifecycle.lock_stable_scope_for_purge(
            session,  # type: ignore[arg-type]
            owner_user_id,
        )

    assert session.events == [
        "lock-projects",
        "lock-memberships",
        "lock-user",
    ]


class _GuardReached(RuntimeError):
    pass


class _ProjectCreateTransaction:
    def __init__(self, session: _ProjectCreateSession) -> None:
        self.session = session

    async def __aenter__(self) -> None:
        self.session.transaction_open = True

    async def __aexit__(self, *_args) -> None:
        self.session.transaction_open = False


class _ProjectCreateSession:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.added: list[object] = []
        self.transaction_open = False

    def begin(self) -> _ProjectCreateTransaction:
        return _ProjectCreateTransaction(self)

    def in_transaction(self) -> bool:
        return self.transaction_open

    def add(self, row: object) -> None:
        if getattr(row, "id", None) is None:
            setattr(row, "id", uuid.uuid4())
        self.added.append(row)
        self.events.append(f"add:{type(row).__name__}")

    async def flush(self) -> None:
        self.events.append("flush")


class _StoppingAccountPrivateLifecycle:
    async def require_active_after_membership(
        self,
        session: _ProjectCreateSession,
        owner_user_id: uuid.UUID | str,
    ) -> AccountPrivateGeneration:
        assert session.in_transaction()
        assert str(owner_user_id) == str(OWNER_USER_ID)
        session.events.append("account-private-guard")
        raise _GuardReached


class _StoppingAccountPrivateReactivation:
    async def reactivate_after_membership(
        self,
        session,
        owner_user_id: uuid.UUID | str,
    ) -> AccountPrivateGeneration:
        assert str(owner_user_id) == str(OWNER_USER_ID)
        session.events.append("account-private-reactivate")
        raise _GuardReached


OWNER_USER_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.mark.asyncio
async def test_project_creation_checks_account_after_membership_before_children() -> None:
    session = _ProjectCreateSession()
    repository = ProjectRepository(
        session,  # type: ignore[arg-type]
        account_private_lifecycle=_StoppingAccountPrivateReactivation(),  # type: ignore[arg-type]
    )

    with pytest.raises(_GuardReached):
        await repository.create_with_admin(
            OWNER_USER_ID,
            CreateProject("lifecycle-order", "Lifecycle Order"),
            "account-private-lifecycle-order",
        )

    assert session.events == [
        "add:ProjectRow",
        "flush",
        "add:ProjectMembershipRow",
        "flush",
        "account-private-reactivate",
    ]
    assert session.in_transaction() is False


class _SkillBuilderGuardSession:
    def __init__(self) -> None:
        self.events: list[str] = []

    def in_transaction(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_skill_builder_admission_checks_account_before_asset_or_thread_work() -> None:
    session = _SkillBuilderGuardSession()
    lifecycle = _StoppingAccountPrivateLifecycle()
    service = SkillBuilderRunAdmissionService(
        lambda: None,
        account_private_lifecycle=lifecycle,
    )
    context = ProjectContext(
        user_id=OWNER_USER_ID,
        project_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        membership_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id="skill-builder-account-lifecycle",
    )
    design_id = uuid.UUID("66666666-6666-4666-8666-666666666666")
    design = SimpleNamespace(
        id=design_id,
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
    )
    operation = SimpleNamespace(
        project_id=context.project_id,
        owner_user_id=str(context.user_id),
        session_id=design_id,
        operation_kind="turn",
        status="in_progress",
        run_id=None,
    )

    with pytest.raises(_GuardReached):
        await service.admit_in_session(
            session,  # type: ignore[arg-type]
            context,
            design,  # type: ignore[arg-type]
            operation,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            turn_message="build",
            model_name=None,
            thinking_enabled=None,
            reasoning_effort=None,
        )

    assert session.events == ["account-private-guard"]


class _AutomationGuardSession(_SkillBuilderGuardSession):
    async def execute(self, _statement, _parameters=None):
        self.events.append("advisory-lock")
        return None


@pytest.mark.asyncio
async def test_automation_admission_checks_account_before_task_or_thread_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _AutomationGuardSession()
    context = ProjectContext(
        user_id=OWNER_USER_ID,
        project_id=uuid.UUID("77777777-7777-4777-8777-777777777777"),
        membership_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=4,
        request_id="automation-account-lifecycle",
    )

    async def resolve_context(*_args, **_kwargs) -> ProjectContext:
        session.events.append("project-membership-lock")
        return context

    monkeypatch.setattr(
        "app.automations.dispatcher.resolve_project_context_in_transaction",
        resolve_context,
    )
    service = AutomationDispatcher(
        lambda: None,  # type: ignore[arg-type]
        account_private_lifecycle=_StoppingAccountPrivateLifecycle(),
    )

    with pytest.raises(_GuardReached):
        await service.admit_occurrence_in_session(
            session,  # type: ignore[arg-type]
            AutomationDefinitionRef(
                project_id=context.project_id,
                owner_user_id=str(context.user_id),
                task_id="task-1",
                membership_version=context.membership_version,
            ),
            scheduled_for=datetime.now(UTC),
        )

    assert session.events == [
        "advisory-lock",
        "project-membership-lock",
        "account-private-guard",
    ]


@pytest.mark.asyncio
async def test_memory_seal_admission_checks_account_before_thread_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _SkillBuilderGuardSession()
    context = ProjectContext(
        user_id=OWNER_USER_ID,
        project_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        membership_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=2,
        request_id="memory-seal-account-lifecycle",
    )

    async def resolve_context(*_args, **_kwargs) -> ProjectContext:
        session.events.append("project-membership-lock")
        return context

    monkeypatch.setattr(
        "app.private_work.memory_seal_service.resolve_project_context_in_transaction",
        resolve_context,
    )
    service = MemorySealAdmissionService(
        account_private_lifecycle=_StoppingAccountPrivateLifecycle(),
    )

    with pytest.raises(_GuardReached):
        await service.admit_thread(
            session,  # type: ignore[arg-type]
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            thread_id="thread-1",
            now=datetime.now(UTC),
        )

    assert session.events == [
        "project-membership-lock",
        "account-private-guard",
    ]


class _InvitationLifecycleRepository:
    def __init__(self) -> None:
        self.session = _SkillBuilderGuardSession()

    async def redeem_locked(self, project, _invitation, *, user_id, now):
        del now
        self.session.events.append("membership-upsert")
        return SimpleNamespace(
            role=ProjectRole.ADMIN,
            invitation_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            project_id=project.id,
            project_slug=project.slug,
        )

    async def lock_membership(self, project_id, user_id):
        del project_id, user_id
        self.session.events.append("membership-lock")
        return SimpleNamespace(
            id=uuid.uuid4(),
            version=3,
            activation_generation=2,
        )


@pytest.mark.asyncio
async def test_invitation_redeem_reactivates_account_after_membership_lock() -> None:
    repository = _InvitationLifecycleRepository()
    service = InvitationService(
        repository,  # type: ignore[arg-type]
        account_private_lifecycle=_StoppingAccountPrivateReactivation(),
    )

    with pytest.raises(_GuardReached):
        await service._redeem_locked(
            SimpleNamespace(id=uuid.uuid4(), slug="lifecycle"),
            object(),
            user_id=OWNER_USER_ID,
            now=datetime.now(UTC),
            request_id="invitation-lifecycle-order",
        )

    assert repository.session.events == [
        "membership-upsert",
        "membership-lock",
        "account-private-reactivate",
    ]
