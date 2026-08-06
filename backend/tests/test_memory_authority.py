from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace

import pytest

import app.private_work.memory_authority as authority_module
from app.private_work.context import PrivateWorkContext
from app.private_work.memory_authority import (
    PrivateRunMemoryAuthority,
    PrivateRunMemorySnapshot,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.agents.memory.dream import EMPTY_MEMORY_DOCUMENT
from deerflow.config.memory_config import MemoryConfig
from deerflow.persistence.jobs.sql import JobClaim, JobScope
from deerflow.sandbox.sandbox import AuthorizationRevoked


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        self.execute_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()

    async def execute(self, _statement):
        self.execute_calls += 1
        return _Result(self.snapshot)


class _Personalization:
    def __init__(self, _session, *, enabled: bool = True) -> None:
        self.enabled = enabled

    async def read_memory(self, _user_id):
        return SimpleNamespace(memory_enabled=self.enabled, version=4)


class _Runs:
    def __init__(self, _session, *, thread_id: str, job_id: uuid.UUID) -> None:
        self.thread_id = thread_id
        self.job_id = job_id

    async def assert_execution_active(self, **_kwargs):
        return False

    async def get(self, **_kwargs):
        return SimpleNamespace(thread_id=self.thread_id, job_id=self.job_id)


def _authority_parts(*, snapshot, memory_enabled: bool = True):
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    job_id = uuid.uuid4()
    thread_id = str(uuid.uuid4())
    project = ProjectContext(
        user_id=user_id,
        project_id=project_id,
        membership_id=membership_id,
        role=ProjectRole.ADMIN,
        capabilities=capabilities_for(ProjectRole.ADMIN),
        membership_version=3,
        request_id="memory-authority-test",
    )
    context = PrivateWorkContext.from_project(project)
    claim = JobClaim(
        job_id=job_id,
        attempt_id=uuid.uuid4(),
        lease_token="lease-token",
        job_type="private_run",
        scope=JobScope(project_id, str(user_id)),
        run_id=str(uuid.uuid4()),
        occurrence_id=None,
        retry_safety="safe",
        cancel_requested=False,
        origin_trace_id="a" * 32,
    )
    session = _Session(snapshot)

    def factory():
        return session

    def personalization_builder(current_session):
        return _Personalization(current_session, enabled=memory_enabled)

    def runs_builder(current_session):
        return _Runs(current_session, thread_id=thread_id, job_id=job_id)

    authority = PrivateRunMemoryAuthority(
        factory,
        context=context,
        claim=claim,
        thread_id=thread_id,
        namespace="default",
        memory_config=MemoryConfig(enabled=True, max_injection_tokens=2_000),
        personalization_repository_builder=personalization_builder,
        run_repository_builder=runs_builder,
    )
    return authority, project, session


@pytest.mark.asyncio
async def test_worker_authority_returns_only_the_frozen_document_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = EMPTY_MEMORY_DOCUMENT
    row = SimpleNamespace(
        document_version=7,
        content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(),
    )
    authority, project, session = _authority_parts(snapshot=row)

    async def resolve(*_args, **_kwargs):
        return project

    async def active(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        authority_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(
        authority_module.PrivateRunAuthorizationService,
        "is_active",
        active,
    )

    result = await authority.load_snapshot()

    assert result == PrivateRunMemorySnapshot(
        document_version=7,
        content=content,
        content_digest=row.content_digest,
    )
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_worker_authority_honors_live_account_disable_before_snapshot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, project, session = _authority_parts(
        snapshot=object(),
        memory_enabled=False,
    )

    async def resolve(*_args, **_kwargs):
        return project

    async def active(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        authority_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(
        authority_module.PrivateRunAuthorizationService,
        "is_active",
        active,
    )

    assert await authority.load_snapshot() is None
    assert session.execute_calls == 0


@pytest.mark.asyncio
async def test_worker_authority_fails_closed_on_snapshot_digest_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = SimpleNamespace(
        document_version=2,
        content=EMPTY_MEMORY_DOCUMENT,
        content_digest="0" * 64,
    )
    authority, project, _session = _authority_parts(snapshot=row)

    async def resolve(*_args, **_kwargs):
        return project

    async def active(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        authority_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    monkeypatch.setattr(
        authority_module.PrivateRunAuthorizationService,
        "is_active",
        active,
    )

    with pytest.raises(AuthorizationRevoked):
        await authority.load_snapshot()
