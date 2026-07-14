from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.dialects import postgresql

from app.private_work.context import PrivateWorkContext, strip_private_client_fields
from app.private_work.errors import PrivateWorkForbidden, PrivateWorkNotFound
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.models import ProjectRole
from deerflow.runtime import PrivateResourceScope


@pytest.fixture
def project_context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=7,
        request_id="req-private-work",
    )


def test_private_work_context_can_only_derive_from_exact_project_context(project_context: ProjectContext) -> None:
    context = PrivateWorkContext.from_project(project_context)
    constructor_fields = {
        "user_id": project_context.user_id,
        "project_id": project_context.project_id,
        "membership_id": project_context.membership_id,
        "role": project_context.role,
        "capabilities": project_context.capabilities,
        "membership_version": project_context.membership_version,
        "request_id": project_context.request_id,
    }

    assert context.project_id == project_context.project_id
    assert context.user_id == project_context.user_id
    assert context.resource_scope == PrivateResourceScope(
        project_id=str(project_context.project_id),
        owner_user_id=str(project_context.user_id),
        membership_version=project_context.membership_version,
    )
    with pytest.raises(FrozenInstanceError):
        context.membership_version = 999  # type: ignore[misc]

    with pytest.raises(TypeError):
        PrivateWorkContext(**constructor_fields)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be derived"):
        PrivateWorkContext(_factory_key=object(), **constructor_fields)  # type: ignore[arg-type]

    with pytest.raises(TypeError):
        type("ForgedPrivateWorkContext", (PrivateWorkContext,), {})

    class ForgedProjectContext(ProjectContext):
        pass

    forged = ForgedProjectContext(**project_context.__dict__)
    for source in (project_context.__dict__, forged, SimpleNamespace(**project_context.__dict__)):
        with pytest.raises(PrivateWorkNotFound):
            PrivateWorkContext.from_project(source)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_transaction_resolver_uses_joined_project_membership_lock_without_owning_transaction() -> None:
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    result = SimpleNamespace(
        all=lambda: [
            SimpleNamespace(
                project_id=project_id,
                membership_id=membership_id,
                role="runner",
                membership_version=5,
            )
        ]
    )
    session = MagicMock()
    session.execute = AsyncMock(return_value=result)
    session.begin.side_effect = AssertionError("nested begin")
    session.commit = AsyncMock(side_effect=AssertionError("unexpected commit"))
    session.rollback = AsyncMock(side_effect=AssertionError("unexpected rollback"))

    resolved = await resolve_project_context_in_transaction(
        session,
        uuid.uuid4(),
        project_id,
        "req-locked",
        lock=True,
    )

    assert resolved.project_id == project_id
    assert resolved.membership_id == membership_id
    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "projects.status = 'active'" in sql
    assert "projects.is_suspended IS false" in sql
    assert "project_memberships.status = 'active'" in sql
    assert "FOR UPDATE OF projects, project_memberships" in sql
    session.begin.assert_not_called()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


def test_strip_private_client_fields_drops_every_authority_field() -> None:
    cleaned = strip_private_client_fields(
        {
            "user_id": "attacker",
            "project_id": "attacker",
            "owner_user_id": "attacker",
            "membership_id": "attacker",
            "role": "admin",
            "system_role": "system_admin",
            "capabilities": ["shared_assets.execute"],
            "membership_version": 999,
            "project_context": {},
            "private_work_context": {},
            "__private_scope": {},
            "__another_internal_key": "attacker",
            "model_name": "allowed-model",
        }
    )

    assert cleaned == {"model_name": "allowed-model"}


@pytest.mark.asyncio
async def test_revalidator_fails_closed_for_stale_or_invalid_scope(monkeypatch: pytest.MonkeyPatch, project_context: ProjectContext) -> None:
    context = PrivateWorkContext.from_project(project_context)
    revalidator = PrivateWorkRevalidator()

    async def stale(*_args: object, **_kwargs: object) -> ProjectContext:
        return ProjectContext(
            **{
                **project_context.__dict__,
                "membership_version": project_context.membership_version + 1,
            }
        )

    monkeypatch.setattr("app.private_work.revalidation.resolve_project_context_in_transaction", stale)
    with pytest.raises(PrivateWorkNotFound):
        await revalidator.require(object(), context)

    async def missing(*_args: object, **_kwargs: object) -> ProjectContext:
        from app.projects.errors import ProjectNotFound

        raise ProjectNotFound()

    monkeypatch.setattr("app.private_work.revalidation.resolve_project_context_in_transaction", missing)
    with pytest.raises(PrivateWorkNotFound):
        await revalidator.require(object(), context, lock=True)


@pytest.mark.asyncio
async def test_revalidator_returns_forbidden_only_for_missing_capability(monkeypatch: pytest.MonkeyPatch, project_context: ProjectContext) -> None:
    context = PrivateWorkContext.from_project(project_context)
    current = ProjectContext(
        **{
            **project_context.__dict__,
            "capabilities": frozenset(),
        }
    )

    async def resolved(*_args: object, **_kwargs: object) -> ProjectContext:
        return current

    monkeypatch.setattr("app.private_work.revalidation.resolve_project_context_in_transaction", resolved)
    with pytest.raises(PrivateWorkForbidden):
        await PrivateWorkRevalidator().require(object(), context, Capability.PRIVATE_WORK_CREATE)
