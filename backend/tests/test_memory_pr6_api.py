from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import project_memory as memory_router
from app.gateway.routers.project_memory import (
    ProjectMemoryV2CandidateDecisionRequest,
    ProjectMemoryV2Fact,
    ProjectMemoryV2FactStateRequest,
    ProjectMemoryV2FactUpdateRequest,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import (
    PrivateWorkConflict,
    PrivateWorkInvalid,
    PrivateWorkNotFound,
    PrivateWorkUnavailable,
)
from app.private_work.memory_service import PrivateMemoryV2Service
from app.projects.capabilities import Capability, capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.persistence.private_work.memory_v2_management import (
    MemoryV2CandidateView,
    MemoryV2FactDetail,
    MemoryV2FactView,
    MemoryV2HardForgetResult,
    MemoryV2ManagementConflict,
    MemoryV2ManagementInvalid,
    MemoryV2ManagementNotFound,
    MemoryV2RevisionView,
)


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="memory-pr6-request",
        )
    )


def _revision(*, fact_id: uuid.UUID | None = None) -> MemoryV2RevisionView:
    now = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)
    selected_fact_id = fact_id or uuid.uuid4()
    return MemoryV2RevisionView(
        id=uuid.uuid4(),
        fact_id=selected_fact_id,
        revision_number=1,
        revision_sequence=1,
        content="Prefer concise Chinese answers",
        content_digest="a" * 64,
        category="preference",
        confidence=0.9,
        valid_from=None,
        valid_to=None,
        last_confirmed_at=now,
        changed_by="user",
        source_candidate_id=None,
        supersedes_revision_id=None,
        change_reason="manual",
        content_erased_at=None,
        created_at=now,
    )


def _fact() -> MemoryV2FactView:
    now = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)
    fact_id = uuid.uuid4()
    return MemoryV2FactView(
        id=fact_id,
        fact_kind="preference",
        status="active",
        version=1,
        disabled_at=None,
        superseded_at=None,
        deleted_at=None,
        created_at=now,
        updated_at=now,
        current_revision=_revision(fact_id=fact_id),
    )


def _candidate() -> MemoryV2CandidateView:
    now = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)
    return MemoryV2CandidateView(
        id=uuid.uuid4(),
        candidate_type="preference",
        content="Prefer concise Chinese answers",
        confidence=0.9,
        retention_class="durable",
        sensitivity="normal",
        status="pending",
        decision_reason=None,
        decided_at=None,
        content_erased_at=None,
        created_at=now,
        updated_at=now,
    )


class _ApiService:
    def __init__(self) -> None:
        self.fact = _fact()
        self.candidate = _candidate()
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []
        self.errors: dict[str, Exception] = {}

    def _record(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object],
    ) -> None:
        self.calls.append((name, args, kwargs))
        if error := self.errors.get(name):
            raise error

    async def list_facts(self, *args: object, **kwargs: object) -> tuple[MemoryV2FactView, ...]:
        self._record("list_facts", args, kwargs)
        return (self.fact,)

    async def list_candidates(
        self,
        *args: object,
        **kwargs: object,
    ) -> tuple[MemoryV2CandidateView, ...]:
        self._record("list_candidates", args, kwargs)
        return (self.candidate,)

    async def get_fact(self, *args: object, **kwargs: object) -> MemoryV2FactDetail:
        self._record("get_fact", args, kwargs)
        return MemoryV2FactDetail(
            fact=self.fact,
            revisions=(self.fact.current_revision,),
            evidence=(),
        )

    async def accept_candidate(self, *args: object, **kwargs: object) -> MemoryV2FactView:
        self._record("accept_candidate", args, kwargs)
        return self.fact

    async def reject_candidate(
        self,
        *args: object,
        **kwargs: object,
    ) -> MemoryV2CandidateView:
        self._record("reject_candidate", args, kwargs)
        return self.candidate

    async def revise_fact(self, *args: object, **kwargs: object) -> MemoryV2FactView:
        self._record("revise_fact", args, kwargs)
        return self.fact

    async def set_fact_enabled(self, *args: object, **kwargs: object) -> MemoryV2FactView:
        self._record("set_fact_enabled", args, kwargs)
        return self.fact

    async def hard_forget_fact(
        self,
        *args: object,
        **kwargs: object,
    ) -> MemoryV2HardForgetResult:
        self._record("hard_forget_fact", args, kwargs)
        return MemoryV2HardForgetResult(
            fact_id=self.fact.id,
            version=2,
            status="deleted",
            erased_candidates=1,
            erased_revisions=1,
            erased_evidence=0,
            erased_source_items=1,
        )

    async def open_export(self, *args: object, **kwargs: object) -> AsyncIterator[bytes]:
        self._record("open_export", args, kwargs)

        async def stream() -> AsyncIterator[bytes]:
            yield b'{"record_type":"manifest"}\n'

        return stream()


@pytest.fixture()
def api_service() -> _ApiService:
    return _ApiService()


@pytest.fixture()
def app(
    api_service: _ApiService,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    value = FastAPI()
    value.include_router(memory_router.router)
    value.dependency_overrides[private_work_context] = _context
    value.dependency_overrides[require_project_private_open] = lambda: None
    monkeypatch.setattr(memory_router, "_v2_service", lambda _request: api_service)
    return value


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    **kwargs: object,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_memory_router_keeps_v1_read_surface_and_exposes_v2_management() -> None:
    routes = {(route.path, method) for route in memory_router.router.routes for method in route.methods or ()}

    assert {
        ("/api/projects/{project_id}/memory", "GET"),
        ("/api/projects/{project_id}/memory/status", "GET"),
        ("/api/projects/{project_id}/memory/export", "GET"),
        ("/api/projects/{project_id}/memory/reload", "POST"),
        ("/api/projects/{project_id}/memory/v2/facts", "GET"),
        ("/api/projects/{project_id}/memory/v2/status", "GET"),
        ("/api/projects/{project_id}/memory/v2/candidates", "GET"),
        ("/api/projects/{project_id}/memory/v2/facts/{fact_id}", "GET"),
        ("/api/projects/{project_id}/memory/v2/candidates/{candidate_id}/accept", "POST"),
        ("/api/projects/{project_id}/memory/v2/candidates/{candidate_id}/reject", "POST"),
        ("/api/projects/{project_id}/memory/v2/facts/{fact_id}", "PATCH"),
        ("/api/projects/{project_id}/memory/v2/facts/{fact_id}/disable", "POST"),
        ("/api/projects/{project_id}/memory/v2/facts/{fact_id}/restore", "POST"),
        ("/api/projects/{project_id}/memory/v2/facts/{fact_id}/hard-forget", "POST"),
        ("/api/projects/{project_id}/memory/v2/export", "GET"),
    } <= routes

    assert {
        ("/api/projects/{project_id}/memory/import", "POST"),
        ("/api/projects/{project_id}/memory/facts", "POST"),
        ("/api/projects/{project_id}/memory/facts/{fact_id}", "PATCH"),
        ("/api/projects/{project_id}/memory/facts/{fact_id}", "DELETE"),
    }.isdisjoint(routes)


def test_v2_request_contracts_require_alias_cas_and_reject_unknown_fields() -> None:
    decision = ProjectMemoryV2CandidateDecisionRequest.model_validate({"expectedUpdatedAt": "2026-08-05T01:02:03Z"})
    state = ProjectMemoryV2FactStateRequest.model_validate({"expectedVersion": 3})
    update = ProjectMemoryV2FactUpdateRequest.model_validate(
        {
            "expectedVersion": 3,
            "content": "Updated fact",
            "reason": "user correction",
        }
    )

    assert decision.parsed_expected_updated_at() == datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)
    assert state.expected_version == 3
    assert update.expected_version == 3

    invalid_payloads = (
        (
            ProjectMemoryV2CandidateDecisionRequest,
            {"expected_updated_at": "2026-08-05T01:02:03Z"},
        ),
        (
            ProjectMemoryV2CandidateDecisionRequest,
            {"expectedUpdatedAt": "2026-08-05T01:02:03"},
        ),
        (
            ProjectMemoryV2FactStateRequest,
            {"expectedVersion": 3, "unexpected": True},
        ),
        (ProjectMemoryV2FactUpdateRequest, {"expectedVersion": 3}),
        (
            ProjectMemoryV2FactUpdateRequest,
            {"expected_version": 3, "content": "Updated fact"},
        ),
    )
    for model, payload in invalid_payloads:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_v2_response_contract_uses_aliases_and_rejects_invalid_nested_data() -> None:
    row = _fact()
    response = memory_router._fact_response(row)
    payload = response.model_dump(mode="json", by_alias=True)

    assert set(payload) == {
        "id",
        "factKind",
        "status",
        "version",
        "disabledAt",
        "supersededAt",
        "deletedAt",
        "createdAt",
        "updatedAt",
        "currentRevision",
    }
    assert "fact_id" not in payload["currentRevision"]
    assert payload["currentRevision"]["factId"] == str(row.id)

    invalid = payload | {"unexpected": True}
    with pytest.raises(ValidationError):
        ProjectMemoryV2Fact.model_validate(invalid)
    invalid_revision = dict(payload)
    invalid_revision["currentRevision"] = payload["currentRevision"] | {
        "changedBy": "client",
    }
    with pytest.raises(ValidationError):
        ProjectMemoryV2Fact.model_validate(invalid_revision)


@pytest.mark.asyncio
async def test_v2_list_routes_forward_filters_and_return_alias_shaped_data(
    app: FastAPI,
    api_service: _ApiService,
) -> None:
    project_id = uuid.uuid4()
    facts = await _request(
        app,
        "GET",
        f"/api/projects/{project_id}/memory/v2/facts",
        params={
            "namespace": "agent:lead",
            "status": "all",
            "limit": 12,
            "offset": 4,
        },
    )
    candidates = await _request(
        app,
        "GET",
        f"/api/projects/{project_id}/memory/v2/candidates",
        params={"status": "all"},
    )

    assert facts.status_code == 200
    assert facts.json()["namespace"] == "agent:lead"
    assert facts.json()["items"][0]["factKind"] == "preference"
    assert candidates.status_code == 200
    assert candidates.json()["items"][0]["candidateType"] == "preference"
    assert api_service.calls[0][0] == "list_facts"
    assert api_service.calls[0][2] == {
        "namespace": "agent:lead",
        "statuses": ("active", "disabled"),
        "limit": 12,
        "offset": 4,
    }
    assert api_service.calls[1][0] == "list_candidates"
    assert api_service.calls[1][2]["statuses"] == (
        "pending",
        "accepted",
        "rejected",
        "superseded",
    )


@pytest.mark.asyncio
async def test_v2_mutation_routes_forward_exact_cas_values(
    app: FastAPI,
    api_service: _ApiService,
) -> None:
    project_id = uuid.uuid4()
    candidate_id = api_service.candidate.id
    fact_id = api_service.fact.id
    expected_at = "2026-08-05T01:02:03Z"

    requests = (
        (
            "POST",
            f"/api/projects/{project_id}/memory/v2/candidates/{candidate_id}/accept",
            {"expectedUpdatedAt": expected_at},
        ),
        (
            "POST",
            f"/api/projects/{project_id}/memory/v2/candidates/{candidate_id}/reject",
            {"expectedUpdatedAt": expected_at},
        ),
        (
            "PATCH",
            f"/api/projects/{project_id}/memory/v2/facts/{fact_id}",
            {
                "expectedVersion": 7,
                "content": "Corrected",
                "reason": "user correction",
            },
        ),
        (
            "POST",
            f"/api/projects/{project_id}/memory/v2/facts/{fact_id}/disable",
            {"expectedVersion": 8},
        ),
        (
            "POST",
            f"/api/projects/{project_id}/memory/v2/facts/{fact_id}/restore",
            {"expectedVersion": 9},
        ),
        (
            "POST",
            f"/api/projects/{project_id}/memory/v2/facts/{fact_id}/hard-forget",
            {"expectedVersion": 10},
        ),
    )
    for method, path, body in requests:
        response = await _request(app, method, path, json=body)
        assert response.status_code == 200, response.text

    assert [call[0] for call in api_service.calls] == [
        "accept_candidate",
        "reject_candidate",
        "revise_fact",
        "set_fact_enabled",
        "set_fact_enabled",
        "hard_forget_fact",
    ]
    assert api_service.calls[0][2]["expected_updated_at"] == datetime(
        2026,
        8,
        5,
        1,
        2,
        3,
        tzinfo=UTC,
    )
    assert api_service.calls[1][2]["expected_updated_at"] == datetime(
        2026,
        8,
        5,
        1,
        2,
        3,
        tzinfo=UTC,
    )
    assert api_service.calls[2][2] == {
        "namespace": "default",
        "expected_version": 7,
        "content": "Corrected",
        "category": None,
        "confidence": None,
        "reason": "user correction",
    }
    assert api_service.calls[3][2]["expected_version"] == 8
    assert api_service.calls[3][2]["enabled"] is False
    assert api_service.calls[4][2]["expected_version"] == 9
    assert api_service.calls[4][2]["enabled"] is True
    assert api_service.calls[5][2]["expected_version"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "method", "body"),
    [
        ("/memory/v2/facts/not-a-uuid", "GET", None),
        (
            f"/memory/v2/candidates/{uuid.uuid4()}/accept",
            "POST",
            {"expectedUpdatedAt": "2026-08-05T01:02:03"},
        ),
        (
            f"/memory/v2/facts/{uuid.uuid4()}/disable",
            "POST",
            {"expected_version": 1},
        ),
        ("/memory/v2/facts?limit=101", "GET", None),
    ],
)
async def test_v2_invalid_inputs_use_stable_private_422(
    app: FastAPI,
    path: str,
    method: str,
    body: dict[str, object] | None,
) -> None:
    response = await _request(
        app,
        method,
        f"/api/projects/{uuid.uuid4()}{path}",
        **({"json": body} if body is not None else {}),
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code", "retry_after"),
    [
        (PrivateWorkNotFound("request-not-found"), 404, "PRIVATE_WORK_NOT_FOUND", None),
        (PrivateWorkConflict("request-conflict"), 409, "PRIVATE_WORK_CONFLICT", None),
        (
            PrivateWorkUnavailable("request-unavailable"),
            503,
            "PRIVATE_WORK_UNAVAILABLE",
            "1",
        ),
    ],
)
async def test_v2_routes_map_private_errors_without_internal_details(
    app: FastAPI,
    api_service: _ApiService,
    error: Exception,
    status: int,
    code: str,
    retry_after: str | None,
) -> None:
    api_service.errors["accept_candidate"] = error
    response = await _request(
        app,
        "POST",
        f"/api/projects/{uuid.uuid4()}/memory/v2/candidates/{uuid.uuid4()}/accept",
        json={"expectedUpdatedAt": "2026-08-05T01:02:03Z"},
    )

    assert response.status_code == status
    assert response.json()["detail"] == {
        "code": code,
        "message": str(error),
        "request_id": error.request_id,
    }
    assert response.headers.get("retry-after") == retry_after


class _Transaction:
    def __init__(self) -> None:
        self.is_active = True

    def __await__(self):
        async def ready() -> _Transaction:
            return self

        return ready().__await__()

    async def __aenter__(self) -> _Transaction:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self.is_active = False
        return False

    async def rollback(self) -> None:
        self.is_active = False


class _Session:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        self.closed = True
        return False

    def begin(self) -> _Transaction:
        return _Transaction()

    async def execute(self, statement: object) -> None:
        del statement

    async def close(self) -> None:
        self.closed = True


class _SessionFactory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


class _Revalidator:
    def __init__(self) -> None:
        self.calls: list[tuple[Capability, bool]] = []

    async def require(
        self,
        session: object,
        context: PrivateWorkContext,
        capability: Capability,
        *,
        lock: bool = False,
    ) -> None:
        del session, context
        self.calls.append((capability, lock))


class _Repository:
    def __init__(self) -> None:
        self.error: Exception | None = None

    async def _result(self) -> object:
        if self.error is not None:
            raise self.error
        return object()

    async def list_facts(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def list_candidates(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def get_fact_detail(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def accept_candidate(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def reject_candidate(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def revise_fact(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def set_fact_enabled(self, *args: object, **kwargs: object) -> object:
        return await self._result()

    async def hard_forget_fact(self, *args: object, **kwargs: object) -> object:
        if self.error is not None:
            raise self.error
        return MemoryV2HardForgetResult(
            fact_id=uuid.uuid4(),
            version=2,
            status="deleted",
            erased_candidates=0,
            erased_revisions=1,
            erased_evidence=0,
            erased_source_items=0,
        )


def _service(
    *,
    repository: _Repository,
    revalidator: _Revalidator | None = None,
) -> tuple[PrivateMemoryV2Service, _Revalidator, _SessionFactory]:
    selected_revalidator = revalidator or _Revalidator()
    factory = _SessionFactory()
    service = PrivateMemoryV2Service(
        factory,
        source_hmac=lambda _payload: SimpleNamespace(
            hmac_hex="a" * 64,
            key_id="test-key",
        ),
        revalidator=selected_revalidator,
        repository_builder=lambda _session: repository,
    )
    return service, selected_revalidator, factory


@pytest.mark.asyncio
async def test_v2_service_maps_read_write_and_forget_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _Repository()
    service, revalidator, factory = _service(repository=repository)
    context = _context()
    fact_id = uuid.uuid4()
    candidate_id = uuid.uuid4()
    now = datetime(2026, 8, 5, 1, 2, 3, tzinfo=UTC)

    async def no_export_records(*args: object, **kwargs: object) -> AsyncIterator[tuple[str, dict]]:
        del args, kwargs
        if False:
            yield "unused", {}

    monkeypatch.setattr(
        "app.private_work.memory_service.iter_memory_v2_export_records",
        no_export_records,
    )

    await service.list_facts(
        context,
        namespace="default",
        statuses=("active",),
        limit=10,
        offset=0,
    )
    await service.list_candidates(
        context,
        namespace="default",
        statuses=("pending",),
        limit=10,
        offset=0,
    )
    await service.get_fact(context, fact_id, namespace="default")
    export = await service.open_export(context, namespace="default")
    assert [line async for line in export]
    await service.accept_candidate(
        context,
        candidate_id,
        namespace="default",
        expected_updated_at=now,
    )
    await service.reject_candidate(
        context,
        candidate_id,
        namespace="default",
        expected_updated_at=now,
    )
    await service.revise_fact(
        context,
        fact_id,
        namespace="default",
        expected_version=1,
        content="corrected",
        category=None,
        confidence=None,
        reason=None,
    )
    await service.set_fact_enabled(
        context,
        fact_id,
        namespace="default",
        expected_version=2,
        enabled=False,
    )
    await service.hard_forget_fact(
        context,
        fact_id,
        namespace="default",
        expected_version=3,
    )

    assert revalidator.calls == [
        (Capability.PRIVATE_WORK_READ_OWN, False),
        (Capability.PRIVATE_WORK_READ_OWN, False),
        (Capability.PRIVATE_WORK_READ_OWN, False),
        (Capability.PRIVATE_WORK_READ_OWN, False),
        (Capability.PRIVATE_WORK_CREATE, True),
        (Capability.PRIVATE_WORK_CREATE, True),
        (Capability.PRIVATE_WORK_CREATE, True),
        (Capability.PRIVATE_WORK_CREATE, True),
        (Capability.PRIVATE_WORK_READ_OWN, True),
    ]
    assert all(session.closed for session in factory.sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("repository_error", "expected_error"),
    [
        (MemoryV2ManagementNotFound(), PrivateWorkNotFound),
        (MemoryV2ManagementConflict(), PrivateWorkConflict),
        (MemoryV2ManagementInvalid(), PrivateWorkInvalid),
        (RuntimeError("database detail"), PrivateWorkUnavailable),
    ],
)
async def test_v2_service_maps_repository_errors_to_stable_private_errors(
    repository_error: Exception,
    expected_error: type[Exception],
) -> None:
    repository = _Repository()
    repository.error = repository_error
    service, _, _ = _service(repository=repository)

    with pytest.raises(expected_error) as raised:
        await service.get_fact(_context(), uuid.uuid4(), namespace="default")

    assert raised.value.request_id == "memory-pr6-request"
    assert "database detail" not in str(raised.value)
