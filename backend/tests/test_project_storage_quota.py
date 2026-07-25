from __future__ import annotations

import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.quotas.service as service_module
from app.quotas.integration import ProjectQuotaEnforcer
from app.quotas.models import (
    ProjectStorageQuotaAuthority,
    QuotaConflict,
    QuotaExceeded,
    QuotaForbidden,
    QuotaMutation,
    QuotaSourceRef,
    _is_issued_project_storage_quota_authority,
    _issue_project_storage_quota_authority,
)
from app.quotas.service import QuotaService
from deerflow.config.quota_config import QuotaConfig


class _ProjectResult:
    def __init__(self, session: _ProjectSession) -> None:
        self._session = session

    def scalar_one_or_none(self):
        return self._session.project


class _ProjectSession:
    def __init__(self, project) -> None:
        self.project = project
        self.statement = None
        self.flush = AsyncMock()

    async def execute(self, statement):
        self.statement = statement
        return _ProjectResult(self)


class _QuotaRepository:
    def __init__(self, session: _ProjectSession) -> None:
        self._session = session
        if not hasattr(session, "counter"):
            session.counter = SimpleNamespace(
                project_id=session.project.id,
                dimension="storage_bytes",
                bucket="lifetime",
                used=0,
                reserved=0,
                version=1,
                updated_at=None,
            )
            session.ledger = {}
            session.appended = []

    async def lock_counter(self, project_id, dimension, bucket):
        assert project_id == self._session.project.id
        assert dimension == "storage_bytes"
        assert bucket == "lifetime"
        return self._session.counter

    async def ledger_entry(self, project_id, dimension, idempotency_key):
        assert project_id == self._session.project.id
        assert dimension == "storage_bytes"
        return self._session.ledger.get(idempotency_key)

    async def threshold_recorded(self, _project_id, _dimension, _bucket):
        return False

    async def append_ledger(self, **values):
        row = SimpleNamespace(**values)
        self._session.ledger[values["idempotency_key"]] = row
        self._session.appended.append(row)
        return row


def _source_ref(payload: bytes) -> QuotaSourceRef:
    return QuotaSourceRef(
        key_id="test-project-storage",
        hmac_hex=hashlib.sha256(payload).hexdigest(),
    )


def _project(project_id: uuid.UUID, *, status: str, suspended: bool = False):
    return SimpleNamespace(
        id=project_id,
        status=status,
        is_suspended=suspended,
    )


def _service() -> QuotaService:
    return QuotaService(
        lambda: None,  # type: ignore[arg-type,return-value]
        QuotaConfig(),
        source_ref_hasher=_source_ref,
    )


def test_project_storage_authority_is_registry_issued_and_operation_bound() -> None:
    project_id = uuid.uuid4()

    reserve = _issue_project_storage_quota_authority(
        project_id,
        operation="reserve",
    )
    release = _issue_project_storage_quota_authority(
        project_id,
        operation="release",
    )

    assert reserve.project_id == project_id
    assert reserve.operation == "reserve"
    assert release.project_id == project_id
    assert release.operation == "release"
    assert _is_issued_project_storage_quota_authority(reserve)
    assert _is_issued_project_storage_quota_authority(release)

    forged = object.__new__(ProjectStorageQuotaAuthority)
    object.__setattr__(forged, "project_id", project_id)
    object.__setattr__(forged, "operation", "reserve")
    assert not _is_issued_project_storage_quota_authority(forged)

    with pytest.raises(QuotaForbidden):
        _issue_project_storage_quota_authority(project_id, operation="consume")
    with pytest.raises(QuotaForbidden):
        _issue_project_storage_quota_authority("not-a-project", operation="reserve")


@pytest.mark.anyio
async def test_project_storage_reserve_requires_active_non_suspended_project() -> None:
    project_id = uuid.uuid4()
    service = _service()
    authority = _issue_project_storage_quota_authority(
        project_id,
        operation="reserve",
    )

    active_session = _ProjectSession(_project(project_id, status="active"))
    assert (
        await service._lock_project_storage_authority(
            active_session,
            authority,
        )
        == project_id
    )
    assert active_session.statement._for_update_arg.read is True

    for project in (
        _project(project_id, status="active", suspended=True),
        _project(project_id, status="pending_deletion"),
        None,
    ):
        with pytest.raises(QuotaForbidden):
            await service._lock_project_storage_authority(
                _ProjectSession(project),
                authority,
            )


@pytest.mark.anyio
async def test_project_storage_release_allows_pending_deletion() -> None:
    project_id = uuid.uuid4()
    service = _service()
    authority = _issue_project_storage_quota_authority(
        project_id,
        operation="release",
    )

    assert (
        await service._lock_project_storage_authority(
            _ProjectSession(_project(project_id, status="pending_deletion")),
            authority,
        )
        == project_id
    )


@pytest.mark.anyio
async def test_project_storage_uses_fixed_subject_and_exact_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    session = _ProjectSession(_project(project_id, status="active"))
    service = _service()
    service.effective_limit = AsyncMock(return_value=1024)  # type: ignore[method-assign]
    monkeypatch.setattr(service_module, "QuotaRepository", _QuotaRepository)
    key = f"skill-version:{uuid.uuid4()}"

    reserved = await service.mutate_project_storage(
        session,
        _issue_project_storage_quota_authority(
            project_id,
            operation="reserve",
        ),
        17,
        key,
    )
    session.project.status = "pending_deletion"
    released = await service.mutate_project_storage(
        session,
        _issue_project_storage_quota_authority(
            project_id,
            operation="release",
        ),
        17,
        key,
    )

    assert reserved.reserved == 17
    assert released.reserved == 0
    assert [row.delta for row in session.appended] == [17, -17]
    assert all(row.dimension == "storage_bytes" for row in session.appended)
    source_payloads = [
        service._source_payload(
            project_id=project_id,
            owner_user_id="trusted:project_storage",
            dimension="storage_bytes",
            bucket="lifetime",
            operation=operation,
            key=key,
        )
        for operation in ("reserve", "release")
    ]
    assert [row.source_ref_hmac for row in session.appended] == [hashlib.sha256(payload).hexdigest() for payload in source_payloads]


@pytest.mark.anyio
async def test_legacy_skill_release_without_exact_reservation_preserves_aggregate_for_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    session = _ProjectSession(_project(project_id, status="pending_deletion"))
    service = _service()
    service.effective_limit = AsyncMock(return_value=1024)  # type: ignore[method-assign]
    monkeypatch.setattr(service_module, "QuotaRepository", _QuotaRepository)
    _QuotaRepository(session)
    session.counter.reserved = 17

    released = await service.release_project_storage_if_reserved(
        session,
        _issue_project_storage_quota_authority(
            project_id,
            operation="release",
        ),
        17,
        f"skill-version:{uuid.uuid4()}",
    )

    assert released is False
    assert session.counter.reserved == 17
    assert session.appended == []


@pytest.mark.anyio
async def test_skill_version_enforcer_issues_authority_and_uses_stable_key() -> None:
    project_id = uuid.uuid4()
    version_id = uuid.uuid4()
    quotas = object.__new__(QuotaService)
    quotas.mutate_project_storage = AsyncMock(  # type: ignore[method-assign]
        return_value=QuotaMutation(
            dimension="storage_bytes",
            bucket="lifetime",
            used=0,
            reserved=23,
            limit=1024,
            threshold_crossed=False,
            created=True,
        )
    )
    enforcer = ProjectQuotaEnforcer(quotas)
    session = object()

    await enforcer.reserve_skill_version(
        session,  # type: ignore[arg-type]
        project_id,
        version_id=version_id,
        size=23,
    )
    authority = quotas.mutate_project_storage.await_args.args[1]
    assert _is_issued_project_storage_quota_authority(authority)
    assert authority.project_id == project_id
    assert authority.operation == "reserve"
    assert quotas.mutate_project_storage.await_args.args[2:] == (
        23,
        f"skill-version:{version_id}",
    )

    quotas.mutate_project_storage.reset_mock()
    await enforcer.release_skill_version(
        session,  # type: ignore[arg-type]
        project_id,
        version_id=version_id,
        size=23,
    )
    authority = quotas.mutate_project_storage.await_args.args[1]
    assert _is_issued_project_storage_quota_authority(authority)
    assert authority.project_id == project_id
    assert authority.operation == "release"
    assert quotas.mutate_project_storage.await_args.args[2:] == (
        23,
        f"skill-version:{version_id}",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "error",
    (
        QuotaExceeded("storage_bytes", 1),
        QuotaConflict("project storage conflict"),
    ),
)
async def test_skill_version_enforcer_preserves_quota_errors(error: Exception) -> None:
    quotas = object.__new__(QuotaService)
    quotas.mutate_project_storage = AsyncMock(side_effect=error)  # type: ignore[method-assign]
    enforcer = ProjectQuotaEnforcer(quotas)

    with pytest.raises(type(error)) as raised:
        await enforcer.reserve_skill_version(
            object(),  # type: ignore[arg-type]
            uuid.uuid4(),
            version_id=uuid.uuid4(),
            size=1,
        )

    assert raised.value is error
