from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.private_work.memory_dream_service as service_module
from app.personalization.repository import AccountMemoryPreference
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkNotFound, PrivateWorkUnavailable
from app.private_work.memory_dream_service import (
    MemoryDreamAdmissionService,
    MemoryDreamSchedulerService,
)
from app.private_work.memory_service import PrivateMemoryDocumentService
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.system_runtime_settings.models import AgentRuntimePolicyValue, MemoryPolicy
from deerflow.agents.memory.dream import EMPTY_MEMORY_DOCUMENT
from deerflow.persistence.private_work.memory_document_repository import (
    MemoryDocumentScope,
    MemoryDreamAdmissionRecord,
)
from deerflow.runtime.context_compaction import ThreadCompactionResult

NOW = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)


class _Personalization:
    async def read_memory(self, _owner, *, for_update: bool = False):
        assert for_update is True
        return AccountMemoryPreference(memory_enabled=True, version=8)


class _ProjectContext:
    def __init__(self) -> None:
        self.required = []

    def require(self, capability) -> None:
        self.required.append(capability)


class _Transaction:
    def __init__(self, session: _Session) -> None:
        self.session = session

    async def __aenter__(self):
        self.session.transaction_depth += 1
        return self

    async def __aexit__(self, *_args):
        self.session.transaction_depth -= 1
        return False


class _Session:
    def __init__(self) -> None:
        self.transaction_depth = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self) -> _Transaction:
        return _Transaction(self)

    def begin_nested(self) -> _Transaction:
        return _Transaction(self)

    def in_transaction(self) -> bool:
        return self.transaction_depth > 0


class _Repository:
    def __init__(self, scopes: tuple[MemoryDocumentScope, ...]) -> None:
        self.scopes = scopes
        self.list_kwargs = None
        self.admissions = []

    async def list_due_scopes(self, **kwargs):
        self.list_kwargs = kwargs
        return self.scopes

    async def admit_dream(self, scope, **kwargs):
        self.admissions.append((scope, kwargs))
        return MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=uuid.uuid4(),
            history_count=20,
        )

    async def is_scope_due(self, _scope_value, **_kwargs):
        return True


def _scope(index: int = 1) -> MemoryDocumentScope:
    return MemoryDocumentScope(
        project_id=uuid.UUID(f"{index:08d}-1111-4111-8111-111111111111"),
        owner_user_id=f"{index:08d}-2222-4222-8222-222222222222",
        namespace="default",
    )


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.UUID(_scope().owner_user_id),
            project_id=_scope().project_id,
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-dream-service",
        )
    )


def _runtime():
    policy = AgentRuntimePolicyValue(
        memory=MemoryPolicy(
            enabled=True,
            model_name="dream-model",
            dream_interval_minutes=45,
            max_injection_tokens=3_000,
        )
    )
    model = SimpleNamespace(
        model=SimpleNamespace(id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")),
        version=SimpleNamespace(
            id=uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            payload_checksum="c" * 64,
        ),
    )
    return policy, 23, model


def _service(repository: _Repository) -> MemoryDreamAdmissionService:
    return MemoryDreamAdmissionService(
        repository_builder=lambda _session, **_kwargs: repository,
        personalization_repository_builder=lambda _session: _Personalization(),
        job_repository_builder=lambda _session: object(),
    )


@pytest.mark.asyncio
async def test_platform_runtime_freezes_one_locked_policy_pointer_and_model_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def materialize(session, section, *, for_update=False):
        calls.append((session, section, for_update))
        return _runtime()[0], 23

    class Models:
        def __init__(self, session) -> None:
            self.session = session

        async def resolve_active_model(self, model_ref, *, load_envelope):
            calls.append((self.session, model_ref, load_envelope))
            return _runtime()[2]

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)
    session = object()

    policy, revision, model = await MemoryDreamAdmissionService._platform_runtime(session)

    assert policy == _runtime()[0]
    assert revision == 23
    assert model == _runtime()[2]
    assert calls == [
        (session, service_module.RuntimePolicySection.AGENT_RUNTIME, True),
        (session, "dream-model", False),
    ]


@pytest.mark.asyncio
async def test_manual_admission_freezes_exact_four_field_runtime_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository(())
    service = _service(repository)

    async def runtime(_session):
        return _runtime()

    monkeypatch.setattr(service, "_platform_runtime", runtime)

    result = await service.admit(
        object(),
        _scope(),
        trigger="manual_dream",
        now=NOW,
    )

    assert result.disposition == "queued"
    _scope_value, kwargs = repository.admissions[0]
    frozen = kwargs["frozen"]
    assert frozen.preference_version == 8
    assert frozen.policy_revision == 23
    assert frozen.model_config_id == _runtime()[2].model.id
    assert frozen.model_version_id == _runtime()[2].version.id
    assert frozen.model_payload_checksum == "c" * 64


@pytest.mark.asyncio
async def test_scheduler_uses_dream_interval_and_admits_each_due_scope_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scopes = (_scope(1), _scope(2))
    repository = _Repository(scopes)
    service = _service(repository)
    contexts = []

    async def policy(_session, *, for_update):
        assert for_update is False
        return _runtime()[0], _runtime()[1]

    async def runtime(_session):
        return _runtime()

    async def resolve(*_args, **_kwargs):
        context = _ProjectContext()
        contexts.append(context)
        return context

    monkeypatch.setattr(service, "_platform_policy", policy)
    monkeypatch.setattr(service, "_platform_runtime", runtime)
    monkeypatch.setattr(
        service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )

    session = _Session()
    async with session.begin():
        due = await service.list_due_scopes(session, now=NOW, max_jobs=7)
    results = []
    for scope in due:
        async with session.begin():
            results.append(
                await service.admit_scheduled_scope(
                    session,
                    scope,
                    now=NOW,
                )
            )

    assert all(result.disposition == "queued" for result in results)
    assert repository.list_kwargs == {
        "now": NOW,
        "interval_minutes": 45,
        "limit": 7,
    }
    assert [value[0] for value in repository.admissions] == list(scopes)
    assert all(value[1]["trigger"] == "auto_dream" for value in repository.admissions)
    assert all(context.required for context in contexts)


@pytest.mark.asyncio
async def test_scheduler_discovers_without_row_locks_then_uses_one_transaction_per_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[object, ...]] = []
    scopes = (_scope(1), _scope(2))

    class Session(_Session):
        def __init__(self, identity: int) -> None:
            super().__init__()
            self.identity = identity

    class SessionFactory:
        def __init__(self) -> None:
            self.sessions: list[Session] = []

        def __call__(self) -> Session:
            session = Session(len(self.sessions))
            self.sessions.append(session)
            return session

    class Repository:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def list_due_scopes(self, **_kwargs):
            events.append(("due", self.session.identity))
            return scopes

        async def admit_dream(self, _scope_value, **_kwargs):
            events.append(("document", self.session.identity))
            return MemoryDreamAdmissionRecord(
                disposition="queued",
                job_id=uuid.uuid4(),
                history_count=1,
            )

        async def is_scope_due(self, _scope_value, **kwargs):
            assert kwargs == {"now": NOW, "interval_minutes": 45}
            events.append(("due_scope", self.session.identity))
            return True

    class Personalization:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def read_memory(self, _owner, *, for_update=False):
            assert for_update is True
            events.append(("preference", self.session.identity))
            return AccountMemoryPreference(memory_enabled=True, version=8)

    async def materialize(session, _section, *, for_update=False):
        events.append(("policy", session.identity, for_update))
        return _runtime()[0], 23

    class Models:
        def __init__(self, session: Session) -> None:
            self.session = session

        async def resolve_active_model(self, _model_ref, *, load_envelope):
            assert load_envelope is False
            events.append(("model", self.session.identity))
            return _runtime()[2]

    async def resolve(session, *_args, **kwargs):
        assert kwargs["lock"] is True
        events.append(("project", session.identity))
        return _ProjectContext()

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)
    monkeypatch.setattr(
        service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    factory = SessionFactory()
    admission = MemoryDreamAdmissionService(
        repository_builder=lambda session, **_kwargs: Repository(session),
        personalization_repository_builder=Personalization,
        job_repository_builder=lambda _session: object(),
    )
    scheduler = MemoryDreamSchedulerService(
        factory,
        admission=admission,
        max_jobs_per_poll=7,
    )

    assert await scheduler.admit_due(now=NOW) == 2
    assert len(factory.sessions) == 3
    assert events == [
        ("policy", 0, False),
        ("due", 0),
        ("project", 1),
        ("policy", 1, True),
        ("model", 1),
        ("due_scope", 1),
        ("preference", 1),
        ("document", 1),
        ("project", 2),
        ("policy", 2, True),
        ("model", 2),
        ("due_scope", 2),
        ("preference", 2),
        ("document", 2),
    ]


@pytest.mark.asyncio
async def test_scheduler_rechecks_due_with_the_locked_current_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = _scope()
    intervals: list[int] = []

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def list_due_scopes(self, **kwargs):
            intervals.append(kwargs["interval_minutes"])
            return (scope,)

        async def is_scope_due(self, _scope_value, **kwargs):
            intervals.append(kwargs["interval_minutes"])
            return False

        async def admit_dream(self, *_args, **_kwargs):
            raise AssertionError("stale interval must not admit Dream")

    class Personalization:
        def __init__(self, _session) -> None:
            pass

        async def read_memory(self, *_args, **_kwargs):
            raise AssertionError("due recheck must precede the user preference lock")

    async def materialize(_session, _section, *, for_update=False):
        policy, revision, _model = _runtime()
        interval = 120 if for_update else 15
        return (
            policy.model_copy(update={"memory": policy.memory.model_copy(update={"dream_interval_minutes": interval})}),
            revision,
        )

    class Models:
        def __init__(self, _session) -> None:
            pass

        async def resolve_active_model(self, *_args, **_kwargs):
            return _runtime()[2]

    async def resolve(*_args, **_kwargs):
        return _ProjectContext()

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_with_revision_in_session",
        staticmethod(materialize),
    )
    monkeypatch.setattr(service_module, "SystemModelRepository", Models)
    monkeypatch.setattr(
        service_module,
        "resolve_project_context_in_transaction",
        resolve,
    )
    scheduler = MemoryDreamSchedulerService(
        lambda: _Session(),
        admission=MemoryDreamAdmissionService(
            repository_builder=lambda session, **_kwargs: Repository(session),
            personalization_repository_builder=Personalization,
            job_repository_builder=lambda _session: object(),
        ),
    )

    assert await scheduler.admit_due(now=NOW) == 0
    assert intervals == [15, 120]


@pytest.mark.asyncio
async def test_restore_locks_policy_before_user_memory_and_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Revalidator:
        async def require(self, *_args, **kwargs):
            assert kwargs["lock"] is True
            events.append("project")

    class Personalization:
        def __init__(self, _session) -> None:
            pass

        async def read_memory(self, _owner, *, for_update=False):
            assert for_update is True
            events.append("preference")
            return AccountMemoryPreference(memory_enabled=True, version=8)

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def read_version(self, _scope_value, _version):
            events.append("document")
            return SimpleNamespace(content=EMPTY_MEMORY_DOCUMENT)

        async def restore_version(self, *_args, **_kwargs):
            events.append("restore")
            return SimpleNamespace(version=2)

    async def materialize(_session, _section, *, for_update=False):
        assert for_update is True
        events.append("policy")
        return _runtime()[0]

    monkeypatch.setattr(
        service_module.SystemRuntimePolicyMaterializer,
        "materialize_current_in_session",
        staticmethod(materialize),
    )
    service = PrivateMemoryDocumentService(
        lambda: _Session(),
        repository_builder=Repository,
        revalidator=Revalidator(),
        personalization_repository_builder=Personalization,
    )

    result = await service.restore(
        _context(),
        target_version=1,
        expected_current_version=1,
    )

    assert result.version == 2
    assert events == ["project", "policy", "preference", "document", "restore"]


@pytest.mark.asyncio
async def test_scheduler_wrapper_preserves_the_configured_poll_bound() -> None:
    class Admission:
        def __init__(self) -> None:
            self.kwargs = None
            self.sessions = []

        async def list_due_scopes(self, session, **kwargs):
            self.kwargs = (session, kwargs)
            self.sessions.append(session)
            return (_scope(),)

        async def admit_scheduled_scope(self, session, scope, **kwargs):
            self.sessions.append(session)
            assert scope == _scope()
            assert kwargs == {"now": NOW}
            return MemoryDreamAdmissionRecord(
                disposition="queued",
                job_id=uuid.uuid4(),
                history_count=1,
            )

    admission = Admission()
    sessions: list[_Session] = []

    def factory() -> _Session:
        session = _Session()
        sessions.append(session)
        return session

    scheduler = MemoryDreamSchedulerService(
        factory,
        admission=admission,
        max_jobs_per_poll=9,
    )

    assert await scheduler.admit_due(now=NOW) == 1
    assert admission.kwargs == (sessions[0], {"now": NOW, "max_jobs": 9})
    assert admission.sessions == sessions
    assert len(sessions) == 2


class _DreamBarrier:
    def __init__(
        self,
        session: _Session,
        *,
        compactions: list[ThreadCompactionResult],
        ready: list[bool],
        compact_error: Exception | None = None,
    ) -> None:
        self.session = session
        self.compactions = list(compactions)
        self.ready = list(ready)
        self.compact_error = compact_error
        self.events: list[tuple[str, object]] = []

    async def compact(self, context, thread_id, **kwargs):
        assert self.session.in_transaction() is False
        self.events.append(("compact", (context, thread_id, kwargs)))
        if self.compact_error is not None:
            raise self.compact_error
        return self.compactions.pop(0)

    async def lock_and_verify_dream_archive_ready(
        self,
        session,
        context,
        thread_id,
        **kwargs,
    ):
        assert session is self.session
        assert session.in_transaction() is True
        self.events.append(("seal", (context, thread_id, kwargs)))
        return self.ready.pop(0)


class _DreamAdmission:
    def __init__(self, session: _Session) -> None:
        self.session = session
        self.calls: list[tuple[object, object, dict[str, object]]] = []

    async def admit(self, session, scope, **kwargs):
        assert session is self.session
        assert session.in_transaction() is True
        self.calls.append((session, scope, kwargs))
        return MemoryDreamAdmissionRecord(
            disposition="queued",
            job_id=uuid.uuid4(),
            history_count=3,
        )


def _compacted(checkpoint_id: str) -> ThreadCompactionResult:
    return ThreadCompactionResult(
        thread_id="thread-7",
        compacted=True,
        removed_message_count=2,
        preserved_message_count=1,
        summary_updated=True,
        checkpoint_id=checkpoint_id,
    )


def _exhausted() -> ThreadCompactionResult:
    return ThreadCompactionResult(
        thread_id="thread-7",
        compacted=False,
        reason="not_enough_messages",
    )


@pytest.mark.asyncio
async def test_manual_thread_dream_drains_outside_transactions_then_admits_behind_seal() -> None:
    session = _Session()
    barrier = _DreamBarrier(
        session,
        compactions=[_compacted("checkpoint-1"), _compacted("checkpoint-2"), _exhausted()],
        ready=[True],
    )
    admission = _DreamAdmission(session)
    service = PrivateMemoryDocumentService(
        lambda: session,
        dream_admission=admission,
        dream_archive_barrier=barrier,
    )
    config = object()

    result = await service.dream(
        _context(),
        thread_id="thread-7",
        app_config=config,  # type: ignore[arg-type]
    )

    assert result.disposition == "queued"
    assert [event[0] for event in barrier.events] == [
        "compact",
        "compact",
        "compact",
        "seal",
    ]
    assert all(
        event[1][2]["keep"] == ("messages", 0)  # type: ignore[index]
        for event in barrier.events
        if event[0] == "compact"
    )
    assert len(admission.calls) == 1
    assert admission.calls[0][2]["trigger"] == "manual_dream"


@pytest.mark.asyncio
async def test_manual_thread_dream_retries_a_raced_seal_without_fixed_drain_limit() -> None:
    session = _Session()
    barrier = _DreamBarrier(
        session,
        compactions=[_exhausted(), _compacted("checkpoint-race"), _exhausted()],
        ready=[False, True],
    )
    admission = _DreamAdmission(session)
    service = PrivateMemoryDocumentService(
        lambda: session,
        dream_admission=admission,
        dream_archive_barrier=barrier,
    )

    result = await service.dream(
        _context(),
        thread_id="thread-7",
        app_config=object(),  # type: ignore[arg-type]
    )

    assert result.disposition == "queued"
    assert [event[0] for event in barrier.events] == [
        "compact",
        "seal",
        "compact",
        "compact",
        "seal",
    ]


@pytest.mark.asyncio
async def test_manual_thread_dream_fails_closed_before_admission() -> None:
    session = _Session()
    barrier = _DreamBarrier(
        session,
        compactions=[
            ThreadCompactionResult(
                thread_id="thread-7",
                compacted=False,
                reason="compaction_failed",
            )
        ],
        ready=[],
    )
    admission = _DreamAdmission(session)
    service = PrivateMemoryDocumentService(
        lambda: session,
        dream_admission=admission,
        dream_archive_barrier=barrier,
    )

    with pytest.raises(PrivateWorkUnavailable):
        await service.dream(
            _context(),
            thread_id="thread-7",
            app_config=object(),  # type: ignore[arg-type]
        )

    assert admission.calls == []
    assert session.in_transaction() is False


@pytest.mark.asyncio
async def test_manual_thread_dream_requires_server_archive_barrier() -> None:
    session = _Session()
    service = PrivateMemoryDocumentService(
        lambda: session,
        dream_admission=_DreamAdmission(session),
    )

    with pytest.raises(PrivateWorkUnavailable):
        await service.dream(
            _context(),
            thread_id="thread-7",
            app_config=object(),  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_manual_thread_dream_preserves_scoped_not_found() -> None:
    session = _Session()
    barrier = _DreamBarrier(
        session,
        compactions=[],
        ready=[],
        compact_error=PrivateWorkNotFound("memory-dream-service"),
    )
    admission = _DreamAdmission(session)
    service = PrivateMemoryDocumentService(
        lambda: session,
        dream_admission=admission,
        dream_archive_barrier=barrier,
    )

    with pytest.raises(PrivateWorkNotFound):
        await service.dream(
            _context(),
            thread_id="missing-thread",
            app_config=object(),  # type: ignore[arg-type]
        )

    assert admission.calls == []
