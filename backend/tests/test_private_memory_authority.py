from __future__ import annotations

import uuid
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.private_work.context import PrivateWorkContext
from app.private_work.memory_authority import PrivateRunMemoryAuthority
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.reliability.execution import PrivateRunExecutionBoundary
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.sandbox.sandbox import AuthorizationRevoked


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Session:
    def begin(self):
        return _Transaction()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


def _context(
    *,
    role: ProjectRole = ProjectRole.RUNNER,
    capabilities=None,
) -> tuple[ProjectContext, PrivateWorkContext]:
    project = ProjectContext(
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        membership_id=uuid.uuid4(),
        role=role,
        capabilities=(capabilities_for(role) if capabilities is None else frozenset(capabilities)),
        membership_version=7,
        request_id="memory-authority-test",
    )
    return project, PrivateWorkContext.from_project(project)


def _claim(context: PrivateWorkContext, *, run_id: str = "run-1") -> JobClaim:
    return JobClaim(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        lease_token="lease-token",
        job_type="private_run",
        scope=JobScope(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
        ),
        run_id=run_id,
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
    )


@pytest.mark.anyio
async def test_memory_authority_revalidates_and_loads_in_one_transaction(
    monkeypatch,
) -> None:
    import app.private_work.memory_authority as module

    current, context = _context()
    claim = _claim(context)
    session = _Session()
    calls: list[tuple[str, object]] = []
    snapshot = object()

    async def resolve(session_arg, *args, **kwargs):
        calls.append(("membership", session_arg))
        assert kwargs["lock"] is True
        return current

    class Authorization:
        @staticmethod
        async def is_active(session_arg, **kwargs):
            calls.append(("run-authorization", session_arg))
            assert kwargs == {
                "project_id": context.project_id,
                "owner_user_id": str(context.user_id),
                "run_id": claim.run_id,
                "lock": False,
            }
            return True

    class Runs:
        def __init__(self, session_arg):
            assert session_arg is session

        async def assert_execution_active(self, **kwargs):
            calls.append(("lease", session))
            assert kwargs == {
                "scope": context.resource_scope,
                "run_id": claim.run_id,
                "job_id": claim.job_id,
                "lease_token": claim.lease_token,
            }
            return False

        async def get(self, **kwargs):
            calls.append(("exact-run", session))
            assert kwargs == {
                "scope": context.resource_scope,
                "run_id": claim.run_id,
                "lock": False,
            }
            return SimpleNamespace(
                thread_id="thread-1",
                job_id=claim.job_id,
            )

    class Memories:
        def __init__(self, session_arg):
            assert session_arg is session

        async def load(self, **kwargs):
            calls.append(("memory", session))
            assert kwargs == {
                "scope": context.resource_scope,
                "namespace": "default",
                "lock": True,
            }
            return snapshot

    monkeypatch.setattr(
        module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(module, "PrivateRunAuthorizationService", Authorization)
    monkeypatch.setattr(module, "PrivateRunRepository", Runs)
    monkeypatch.setattr(module, "PrivateMemoryRepository", Memories)

    authority = PrivateRunMemoryAuthority(
        lambda: session,
        context=context,
        claim=claim,
        thread_id="thread-1",
        namespace="default",
    )

    assert await authority.load_snapshot() is snapshot
    assert [name for name, _session in calls] == [
        "membership",
        "run-authorization",
        "lease",
        "exact-run",
        "memory",
    ]
    assert all(observed is session for _name, observed in calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure",
    [
        "missing-capability",
        "membership-version-changed",
        "inactive-run",
        "cancel-requested",
        "wrong-thread",
        "lease-error",
        "database-error",
    ],
)
async def test_memory_authority_fails_closed_without_loading(
    monkeypatch,
    failure: str,
) -> None:
    import app.private_work.memory_authority as module

    current, context = _context(
        capabilities=() if failure == "missing-capability" else capabilities_for(ProjectRole.RUNNER),
    )
    if failure == "membership-version-changed":
        current = replace(
            current,
            membership_version=current.membership_version + 1,
        )
    claim = _claim(context)
    session = _Session()
    memory_loads = 0

    async def resolve(*_args, **_kwargs):
        if failure == "database-error":
            raise RuntimeError("database-secret-detail")
        return current

    class Authorization:
        @staticmethod
        async def is_active(*_args, **_kwargs):
            return failure != "inactive-run"

    class Runs:
        def __init__(self, _session):
            pass

        async def assert_execution_active(self, **_kwargs):
            if failure == "lease-error":
                raise RuntimeError("lease-secret-detail")
            return failure == "cancel-requested"

        async def get(self, **_kwargs):
            return SimpleNamespace(
                thread_id=("another-thread" if failure == "wrong-thread" else "thread-1"),
                job_id=claim.job_id,
            )

    class Memories:
        def __init__(self, _session):
            pass

        async def load(self, **_kwargs):
            nonlocal memory_loads
            memory_loads += 1
            return object()

    monkeypatch.setattr(
        module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(module, "PrivateRunAuthorizationService", Authorization)
    monkeypatch.setattr(module, "PrivateRunRepository", Runs)
    monkeypatch.setattr(module, "PrivateMemoryRepository", Memories)

    authority = PrivateRunMemoryAuthority(
        lambda: session,
        context=context,
        claim=claim,
        thread_id="thread-1",
        namespace="default",
    )

    with pytest.raises(AuthorizationRevoked) as error:
        await authority.load_snapshot()

    assert error.value.__cause__ is None
    assert "secret" not in str(error.value).lower()
    assert memory_loads == 0


@pytest.mark.anyio
async def test_memory_authority_missing_memory_is_read_only(
    monkeypatch,
) -> None:
    import app.private_work.memory_authority as module

    current, context = _context()
    claim = _claim(context)
    session = _Session()

    async def resolve(*_args, **_kwargs):
        return current

    class Authorization:
        @staticmethod
        async def is_active(*_args, **_kwargs):
            return True

    class Runs:
        def __init__(self, _session):
            pass

        async def assert_execution_active(self, **_kwargs):
            return False

        async def get(self, **_kwargs):
            return SimpleNamespace(thread_id="thread-1", job_id=claim.job_id)

    class Memories:
        def __init__(self, _session):
            pass

        async def load(self, **_kwargs):
            return None

        async def create_if_needed(self, **_kwargs):
            raise AssertionError("read-only authority created Memory")

    monkeypatch.setattr(
        module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(module, "PrivateRunAuthorizationService", Authorization)
    monkeypatch.setattr(module, "PrivateRunRepository", Runs)
    monkeypatch.setattr(module, "PrivateMemoryRepository", Memories)

    authority = PrivateRunMemoryAuthority(
        lambda: session,
        context=context,
        claim=claim,
        thread_id="thread-1",
        namespace="default",
    )

    assert await authority.load_snapshot() is None


def test_memory_authority_rejects_mismatched_claim_scope() -> None:
    _project, context = _context()
    claim = _claim(context)
    forged_claim = JobClaim(
        job_id=claim.job_id,
        attempt_id=claim.attempt_id,
        lease_token=claim.lease_token,
        job_type=claim.job_type,
        scope=JobScope(
            project_id=uuid.uuid4(),
            owner_user_id=str(context.user_id),
        ),
        run_id=claim.run_id,
        occurrence_id=claim.occurrence_id,
        retry_safety=claim.retry_safety,
        cancel_requested=claim.cancel_requested,
    )

    with pytest.raises(ValueError, match="claim"):
        PrivateRunMemoryAuthority(
            lambda: _Session(),
            context=context,
            claim=forged_claim,
            thread_id="thread-1",
            namespace="default",
        )


@pytest.mark.anyio
async def test_read_only_tool_boundary_does_not_mark_ambiguous_side_effect(
    monkeypatch,
) -> None:
    _current, context = _context()
    claim = _claim(context)
    boundary = PrivateRunExecutionBoundary(
        lambda: _Session(),
        context=context,
        claim=claim,
    )
    check = AsyncMock()
    monkeypatch.setattr(boundary, "_check", check)

    await boundary.before_read_only_tool_call()

    check.assert_awaited_once_with("before_tool_call")
