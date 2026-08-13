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
from deerflow.agents.memory.dream import (
    DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES,
    EMPTY_MEMORY_DOCUMENT,
    render_empty_memory_document,
)
from deerflow.config.memory_config import MemoryConfig
from deerflow.error_codes import MemoryAuthorityUnavailable
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
        self.events: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()

    async def execute(self, _statement):
        self.execute_calls += 1
        self.events.append("snapshot")
        return _Result(self.snapshot)


class _Personalization:
    def __init__(self, session, *, enabled: bool = True) -> None:
        self.session = session
        self.enabled = enabled

    async def read_memory(self, _user_id):
        self.session.events.append("preference")
        return SimpleNamespace(memory_enabled=self.enabled, version=4)


class _Runs:
    def __init__(self, session, *, thread_id: str, job_id: uuid.UUID) -> None:
        self.session = session
        self.thread_id = thread_id
        self.job_id = job_id

    async def assert_execution_active(self, **_kwargs):
        self.session.events.append("job_run_lock")
        return False

    async def get(self, **_kwargs):
        self.session.events.append("run_read")
        return SimpleNamespace(thread_id=self.thread_id, job_id=self.job_id)


class _Threads:
    def __init__(
        self,
        session,
        *,
        thread_id: str,
        project_id: uuid.UUID,
        owner_user_id: str,
        state: str,
    ) -> None:
        self.session = session
        self.thread_id = thread_id
        self.project_id = project_id
        self.owner_user_id = owner_user_id
        self.state = state

    async def get(self, *, scope, thread_id: str, lock: bool):
        self.session.events.append("thread_lock")
        assert scope.project_id == str(self.project_id)
        assert scope.owner_user_id == self.owner_user_id
        assert thread_id == self.thread_id
        assert lock is True
        if self.state == "missing":
            return None
        return SimpleNamespace(
            thread_id=self.thread_id,
            project_id=self.project_id,
            owner_user_id=self.owner_user_id,
            frozen_at=(object() if self.state == "frozen" else None),
            deleted_at=(object() if self.state == "deleted" else None),
        )


def _authority_parts(
    *,
    snapshot,
    memory_enabled: bool = True,
    thread_state: str = "active",
):
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

    def threads_builder(current_session):
        return _Threads(
            current_session,
            thread_id=thread_id,
            project_id=project_id,
            owner_user_id=str(user_id),
            state=thread_state,
        )

    authority = PrivateRunMemoryAuthority(
        factory,
        context=context,
        claim=claim,
        thread_id=thread_id,
        namespace="default",
        memory_config=MemoryConfig(enabled=True, max_injection_tokens=2_000),
        personalization_repository_builder=personalization_builder,
        run_repository_builder=runs_builder,
        thread_repository_builder=threads_builder,
    )
    return authority, project, session


@pytest.mark.asyncio
async def test_worker_authority_returns_only_the_frozen_document_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sections = ("协作方式", "架构边界", "当前目标")
    content = render_empty_memory_document(sections)
    row = SimpleNamespace(
        document_version=7,
        content=content,
        content_digest=hashlib.sha256(content.encode()).hexdigest(),
        sections=list(sections),
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
        sections=sections,
    )
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_worker_authority_locks_live_thread_before_run_job_and_preference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, project, session = _authority_parts(
        snapshot=None,
        memory_enabled=False,
    )

    async def resolve(*_args, **_kwargs):
        session.events.append("project_membership_lock")
        return project

    async def active(*_args, **_kwargs):
        session.events.append("run_active_read")
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
    assert session.events == [
        "project_membership_lock",
        "thread_lock",
        "run_active_read",
        "job_run_lock",
        "run_read",
        "preference",
    ]
    assert session.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_state", ["missing", "frozen", "deleted"])
async def test_worker_authority_rejects_a_non_live_locked_thread(
    monkeypatch: pytest.MonkeyPatch,
    thread_state: str,
) -> None:
    authority, project, session = _authority_parts(
        snapshot=None,
        thread_state=thread_state,
    )

    async def resolve(*_args, **_kwargs):
        session.events.append("project_membership_lock")
        return project

    async def active(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("Run authority must not be read after Thread revocation")

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
    assert session.events == ["project_membership_lock", "thread_lock"]
    assert session.execute_calls == 0


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
        sections=list(DEFAULT_MEMORY_DOCUMENT_SECTION_TITLES),
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

    with pytest.raises(MemoryAuthorityUnavailable):
        await authority.load_snapshot()
