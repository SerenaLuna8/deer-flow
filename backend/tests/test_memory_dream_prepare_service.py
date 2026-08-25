from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import pytest

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.memory_dream_prepare_service import MemoryDreamPrepareService
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.memory_dream_prepare_repository import (
    MemoryDreamPrepareAdmission,
    MemoryDreamPrepareConflict,
    MemoryDreamPrepareNotFound,
    MemoryDreamPrepareRecord,
)
from deerflow.persistence.user.private_lifecycle import AccountPrivateGeneration


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return _Transaction()


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-dream-prepare-service",
        )
    )


def _record(
    *,
    job_id: uuid.UUID | None = None,
    dream_job_id: uuid.UUID | None = None,
    status: str = "queued",
    phase: str = "queued",
) -> MemoryDreamPrepareRecord:
    return MemoryDreamPrepareRecord(
        job_id=job_id or uuid.uuid4(),
        thread_id="thread-prepare",
        phase=phase,  # type: ignore[arg-type]
        compacted_passes=0,
        dream_job_id=dream_job_id,
        history_count=None,
        admission_kind=None,
        result_disposition="queued",
        job_status=status,
        public_error_code=None,
        cancel_requested=False,
        updated_at=datetime(2026, 8, 13, tzinfo=UTC),
    )


class _Revalidator:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[tuple[Capability, ...], bool]] = []

    async def require(self, _session, _context, *capabilities, lock=False):
        self.calls.append((capabilities, lock))
        if self.error is not None:
            raise self.error


class _AccountPrivateLifecycle:
    async def require_active_after_membership(self, _session, owner_user_id):
        return AccountPrivateGeneration(
            owner_user_id=str(owner_user_id),
            generation=9,
        )


class _Repository:
    def __init__(self, record: MemoryDreamPrepareRecord) -> None:
        self.record = record
        self.calls: list[tuple[str, object]] = []
        self.error: Exception | None = None

    async def admit(self, _scope, **kwargs):
        self.calls.append(("admit", kwargs))
        if self.error is not None:
            raise self.error
        return MemoryDreamPrepareAdmission("queued", self.record)

    async def read(self, _scope, job_id):
        self.calls.append(("read", job_id))
        if self.error is not None:
            raise self.error
        return self.record

    async def read_latest(self, _scope, **kwargs):
        self.calls.append(("latest", kwargs))
        if self.error is not None:
            raise self.error
        return self.record

    async def request_cancel(self, _scope, **kwargs):
        self.calls.append(("cancel", kwargs))
        if self.error is not None:
            raise self.error
        return self.record


class _Jobs:
    def __init__(self) -> None:
        self.requested: list[tuple[object, uuid.UUID, dict[str, object]]] = []
        self.settled: list[tuple[object, uuid.UUID, dict[str, object]]] = []

    async def request_cancel(self, scope, job_id, **kwargs):
        self.requested.append((scope, job_id, kwargs))
        return True

    async def settle_requested_cancel(self, scope, job_id, **kwargs):
        self.settled.append((scope, job_id, kwargs))
        return True


class _Memory:
    def __init__(self, *, settled: bool = True) -> None:
        self.settled = settled
        self.cancelled: list[tuple[object, uuid.UUID, dict[str, object]]] = []

    async def request_dream_cancel(self, scope, job_id, **kwargs):
        self.cancelled.append((scope, job_id, kwargs))
        return self.settled


class _Audit:
    def __init__(self) -> None:
        self.settled: list[dict[str, object]] = []

    async def memory_dream_settled(self, _session, **kwargs):
        self.settled.append(kwargs)
        return True


def _service(
    repository: _Repository,
    *,
    jobs: _Jobs | None = None,
    revalidator: _Revalidator | None = None,
    audit: _Audit | None = None,
) -> MemoryDreamPrepareService:
    jobs = jobs or _Jobs()
    return MemoryDreamPrepareService(
        lambda: _Session(),  # type: ignore[arg-type]
        repository_builder=lambda _session, *, jobs: repository,
        job_repository_builder=lambda _session: jobs,
        revalidator=revalidator or _Revalidator(),  # type: ignore[arg-type]
        account_private_lifecycle=_AccountPrivateLifecycle(),
        audit=audit,
    )


@pytest.mark.asyncio
async def test_prepare_service_admits_once_and_recovers_latest_status() -> None:
    context = _context()
    record = _record()
    repository = _Repository(record)
    revalidator = _Revalidator()
    service = _service(repository, revalidator=revalidator)
    operation_id = uuid.uuid4()

    admitted = await service.admit(
        context,
        thread_id="thread-prepare",
        operation_id=operation_id,
    )
    latest = await service.read_latest(context, thread_id="thread-prepare")

    assert admitted.record == latest == record
    assert repository.calls[0][0] == "admit"
    assert repository.calls[0][1]["operation_id"] == operation_id
    assert repository.calls[1] == ("latest", {"thread_id": "thread-prepare"})
    assert revalidator.calls == [
        (
            (Capability.PRIVATE_WORK_CREATE, Capability.SHARED_ASSETS_EXECUTE),
            True,
        ),
        (
            (Capability.PRIVATE_WORK_CREATE, Capability.SHARED_ASSETS_EXECUTE),
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_prepare_service_cancel_propagates_into_terminal_parent_child() -> None:
    context = _context()
    child_job_id = uuid.uuid4()
    record = _record(
        dream_job_id=child_job_id,
        status="succeeded",
        phase="succeeded",
    )
    repository = _Repository(record)
    jobs = _Jobs()
    memory = _Memory()
    audit = _Audit()

    service = MemoryDreamPrepareService(
        lambda: _Session(),  # type: ignore[arg-type]
        repository_builder=lambda _session, *, jobs: repository,
        job_repository_builder=lambda _session: jobs,
        dream_store_builder=lambda _session, *, jobs: memory,
        revalidator=_Revalidator(),  # type: ignore[arg-type]
        audit=audit,
    )
    result = await service.cancel(
        context,
        record.job_id,
    )

    assert result == record
    assert memory.cancelled[0][1] == child_job_id
    assert memory.cancelled[0][2]["reason"] == "dream_prepare_cancelled"
    assert jobs.requested == []
    assert jobs.settled == []
    assert audit.settled == [
        {
            "project_id": context.project_id,
            "job_id": child_job_id,
            "request_id": context.request_id,
            "disposition": "cancelled",
        }
    ]
    assert [call[0] for call in repository.calls] == ["cancel", "read"]


@pytest.mark.asyncio
async def test_prepare_service_cooperative_child_cancel_does_not_audit_terminal() -> None:
    context = _context()
    child_job_id = uuid.uuid4()
    record = _record(
        dream_job_id=child_job_id,
        status="succeeded",
        phase="succeeded",
    )
    repository = _Repository(record)
    jobs = _Jobs()
    memory = _Memory(settled=False)
    audit = _Audit()
    service = MemoryDreamPrepareService(
        lambda: _Session(),  # type: ignore[arg-type]
        repository_builder=lambda _session, *, jobs: repository,
        job_repository_builder=lambda _session: jobs,
        dream_store_builder=lambda _session, *, jobs: memory,
        revalidator=_Revalidator(),  # type: ignore[arg-type]
        audit=audit,
    )

    assert await service.cancel(context, record.job_id) == record
    assert memory.cancelled[0][1] == child_job_id
    assert audit.settled == []
    assert jobs.requested == []
    assert jobs.settled == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repository_error", "expected"),
    [
        (MemoryDreamPrepareConflict(), PrivateWorkConflict),
        (MemoryDreamPrepareNotFound(), PrivateWorkNotFound),
    ],
)
async def test_prepare_service_maps_repository_authority_errors(
    repository_error: Exception,
    expected: type[Exception],
) -> None:
    repository = _Repository(_record())
    repository.error = repository_error

    with pytest.raises(expected):
        await _service(repository).admit(
            _context(),
            thread_id="thread-prepare",
            operation_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_prepare_service_fails_before_repository_when_authority_is_stale() -> None:
    context = _context()
    repository = _Repository(_record())
    revalidator = _Revalidator(PrivateWorkNotFound(context.request_id))

    with pytest.raises(PrivateWorkNotFound):
        await _service(repository, revalidator=revalidator).read_latest(
            context,
            thread_id="thread-prepare",
        )

    assert repository.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "invoke"),
    (
        (
            "prepare_admit",
            lambda service, context, job_id: service.admit(
                context,
                thread_id="thread-prepare",
                operation_id=job_id,
            ),
        ),
        (
            "prepare_read",
            lambda service, context, job_id: service.read(context, job_id),
        ),
        (
            "prepare_read_latest",
            lambda service, context, _job_id: service.read_latest(
                context,
                thread_id="thread-prepare",
            ),
        ),
        (
            "prepare_cancel",
            lambda service, context, job_id: service.cancel(context, job_id),
        ),
    ),
)
async def test_prepare_service_logs_content_free_failure_observation(
    operation,
    invoke,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "postgresql://private-user:secret@db/private?scope=private-id"
    repository = _Repository(_record())
    repository.error = RuntimeError(f"sql=/private/path params={secret}")
    service = _service(repository)

    with caplog.at_level(logging.ERROR, logger="app.private_work.memory"):
        with pytest.raises(PrivateWorkUnavailable):
            await invoke(service, _context(), uuid.uuid4())

    records = [record for record in caplog.records if record.name == "app.private_work.memory"]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert f"operation={operation}" in records[0].getMessage()
    assert "failure_category=internal" in records[0].getMessage()
    assert "failure_type=RuntimeError" in records[0].getMessage()
    assert secret not in caplog.text
    assert "/private/path" not in caplog.text
    assert "private-id" not in caplog.text
