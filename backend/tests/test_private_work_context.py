from __future__ import annotations

import asyncio
import copy
import dataclasses
import pickle
import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.private_work.context import PrivateWorkContext, strip_private_client_fields
from app.private_work.errors import PrivateWorkForbidden, PrivateWorkNotFound
from app.private_work.revalidation import PrivateWorkRevalidator
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext, resolve_project_context_in_transaction
from app.projects.membership_repository import MembershipRepository
from app.projects.models import ProjectRole
from deerflow.persistence.projects.model import ProjectMembershipRow, ProjectRow
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


@pytest.mark.parametrize(
    ("clone", "message"),
    [
        pytest.param(copy.copy, "cannot be cloned or serialized", id="copy"),
        pytest.param(copy.deepcopy, "cannot be cloned or serialized", id="deepcopy"),
        pytest.param(dataclasses.replace, "must be derived", id="dataclasses-replace"),
        pytest.param(pickle.dumps, "cannot be cloned or serialized", id="pickle"),
    ],
)
def test_private_work_context_rejects_ordinary_clone_and_serialization(
    project_context: ProjectContext,
    clone,
    message: str,
) -> None:
    context = PrivateWorkContext.from_project(project_context)

    with pytest.raises(TypeError, match=message):
        clone(context)


@pytest.mark.parametrize("hook", ["__getstate__", "__reduce__", "__reduce_ex__", "__setstate__"])
def test_private_work_context_rejects_pickle_and_state_hooks(project_context: ProjectContext, hook: str) -> None:
    context = PrivateWorkContext.from_project(project_context)
    state = [getattr(context, field.name) for field in dataclasses.fields(context)]

    with pytest.raises(TypeError, match="cannot be cloned or serialized"):
        if hook == "__reduce_ex__":
            context.__reduce_ex__(5)
        elif hook == "__setstate__":
            context.__setstate__(state)
        else:
            getattr(context, hook)()


def _fabricate_private_work_context(project_context: ProjectContext, *, populate: bool) -> PrivateWorkContext:
    fabricated = object.__new__(PrivateWorkContext)
    if populate:
        values = {
            "user_id": project_context.user_id,
            "project_id": project_context.project_id,
            "membership_id": project_context.membership_id,
            "role": project_context.role,
            "capabilities": project_context.capabilities,
            "membership_version": project_context.membership_version,
            "request_id": "req-fabricated",
        }
        for name, value in values.items():
            object.__setattr__(fabricated, name, value)
    return fabricated


def test_private_work_context_resource_scope_rejects_direct_fabrication(project_context: ProjectContext) -> None:
    fabricated = _fabricate_private_work_context(project_context, populate=True)

    with pytest.raises(PrivateWorkNotFound) as exc_info:
        _ = fabricated.resource_scope

    assert exc_info.value.request_id == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("populate", [False, True])
async def test_revalidator_rejects_direct_fabrication_before_reading_authority(
    monkeypatch: pytest.MonkeyPatch,
    project_context: ProjectContext,
    populate: bool,
) -> None:
    fabricated = _fabricate_private_work_context(project_context, populate=populate)

    async def unexpected_resolve(*_args: object, **_kwargs: object) -> ProjectContext:
        raise AssertionError("resolver must not receive an unissued context")

    monkeypatch.setattr("app.private_work.revalidation.resolve_project_context_in_transaction", unexpected_resolve)
    with pytest.raises(PrivateWorkNotFound) as exc_info:
        await PrivateWorkRevalidator().require(object(), fabricated)

    assert exc_info.value.request_id == "unknown"


@pytest.mark.asyncio
async def test_revalidator_rejects_tampered_issued_context_before_reading_authority(
    monkeypatch: pytest.MonkeyPatch,
    project_context: ProjectContext,
) -> None:
    context = PrivateWorkContext.from_project(project_context)
    object.__setattr__(context, "project_id", uuid.uuid4())

    async def unexpected_resolve(*_args: object, **_kwargs: object) -> ProjectContext:
        raise AssertionError("resolver must not receive a context with broken provenance")

    monkeypatch.setattr("app.private_work.revalidation.resolve_project_context_in_transaction", unexpected_resolve)
    with pytest.raises(PrivateWorkNotFound) as exc_info:
        await PrivateWorkRevalidator().require(object(), context)

    assert exc_info.value.request_id == "unknown"


@pytest.mark.asyncio
async def test_transaction_resolver_locks_project_then_membership_without_owning_transaction() -> None:
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    project_result = SimpleNamespace(scalar_one_or_none=lambda: project_id)
    membership_result = SimpleNamespace(
        scalar_one_or_none=lambda: SimpleNamespace(
            id=membership_id,
            role="runner",
            version=5,
        )
    )
    session = MagicMock()
    session.execute = AsyncMock(side_effect=[project_result, membership_result])
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
    assert session.execute.await_count == 2
    statements = [call.args[0] for call in session.execute.await_args_list]
    sql = [str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})) for statement in statements]
    assert "FROM projects" in sql[0]
    assert "projects.status = 'active'" in sql[0]
    assert "projects.is_suspended IS false" in sql[0]
    assert "project_memberships" not in sql[0]
    assert "FOR UPDATE OF projects" in sql[0]
    assert "FROM project_memberships" in sql[1]
    assert "project_memberships.status = 'active'" in sql[1]
    assert "FOR UPDATE OF project_memberships" in sql[1]
    session.begin.assert_not_called()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.postgres
async def test_locked_resolver_and_membership_mutation_compete_in_project_then_membership_order(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    user_id, project_id, membership_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    membership_locked = asyncio.Event()
    release_membership = asyncio.Event()
    resolver_complete = asyncio.Event()
    release_resolver = asyncio.Event()
    tasks: list[asyncio.Task[object]] = []

    async def hold_membership_only() -> None:
        async with factory() as session:
            async with session.begin():
                await session.execute(select(ProjectMembershipRow.id).where(ProjectMembershipRow.id == membership_id).with_for_update(of=ProjectMembershipRow))
                membership_locked.set()
                await release_membership.wait()

    async def resolve_and_hold_project() -> ProjectContext:
        async with factory() as session:
            async with session.begin():
                resolved = await resolve_project_context_in_transaction(
                    session,
                    user_id,
                    project_id,
                    "req-lock-order",
                    lock=True,
                )
                resolver_complete.set()
                await release_resolver.wait()
                return resolved

    async def run_existing_membership_mutation_lock(context: ProjectContext) -> None:
        async with factory() as session:
            async with session.begin():
                await MembershipRepository(session).lock_project_and_member(context, membership_id)

    async def wait_until_project_is_locked() -> None:
        for _ in range(50):
            try:
                async with factory() as session:
                    async with session.begin():
                        await session.execute(select(ProjectRow.id).where(ProjectRow.id == project_id).with_for_update(nowait=True, of=ProjectRow))
            except DBAPIError:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("resolver did not lock the project before waiting on membership")

    async def assert_still_blocked(task: asyncio.Task[object]) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.1)

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                    VALUES (:id,:email,'user',:now,false,0)"""
                ),
                {"id": str(user_id), "email": "private-lock@example.com", "now": datetime.now(UTC)},
            )
            await connection.execute(
                text(
                    """INSERT INTO projects (id,slug,display_name,created_by_user_id)
                    VALUES (:id,'private-lock-order','Private lock order',:user_id)"""
                ),
                {"id": project_id, "user_id": str(user_id)},
            )
            await connection.execute(
                text(
                    """INSERT INTO project_memberships (id,project_id,user_id,role)
                    VALUES (:id,:project_id,:user_id,'admin')"""
                ),
                {"id": membership_id, "project_id": project_id, "user_id": str(user_id)},
            )

        context = ProjectContext(
            user_id=user_id,
            project_id=project_id,
            membership_id=membership_id,
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="req-mutation-lock-order",
        )
        membership_task = asyncio.create_task(hold_membership_only())
        tasks.append(membership_task)
        await asyncio.wait_for(membership_locked.wait(), timeout=2)

        resolver_task = asyncio.create_task(resolve_and_hold_project())
        tasks.append(resolver_task)
        await asyncio.wait_for(wait_until_project_is_locked(), timeout=2)

        mutation_task = asyncio.create_task(run_existing_membership_mutation_lock(context))
        tasks.append(mutation_task)
        await assert_still_blocked(mutation_task)

        release_membership.set()
        await asyncio.wait_for(resolver_complete.wait(), timeout=2)
        await assert_still_blocked(mutation_task)

        release_resolver.set()
        resolved = await asyncio.wait_for(resolver_task, timeout=2)
        await asyncio.wait_for(mutation_task, timeout=2)
        await asyncio.wait_for(membership_task, timeout=2)
        assert resolved.membership_id == membership_id
    finally:
        release_membership.set()
        release_resolver.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


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


def test_strip_private_client_fields_recurses_through_mappings_lists_and_tuples() -> None:
    cleaned = strip_private_client_fields(
        {
            "safe": {
                "project_id": "attacker",
                "user_role": "system_admin",
                "items": [
                    {"owner_user_id": "attacker", "value": 1},
                    (
                        {"role": "admin", "value": 2},
                        {"__private_scope": {}, "nested": {"user_role": "admin", "value": 3}},
                    ),
                ],
            },
            "resource_scope": {"project_id": "attacker"},
        }
    )

    assert cleaned == {
        "safe": {
            "items": [
                {"value": 1},
                ({"value": 2}, {"nested": {"value": 3}}),
            ]
        }
    }


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
