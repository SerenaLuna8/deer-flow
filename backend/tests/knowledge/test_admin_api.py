"""M2 gates: knowledge admin/project HTTP contracts and error mapping.

Routes are exercised over ASGI with the authorization dependencies overridden,
mirroring the other admin HTTP contract suites; project-context authorization
is unit-tested against the real dependency with a stubbed resolver.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_CONFLICT,
    KNOWLEDGE_DISABLED,
    KNOWLEDGE_EMBEDDING_FAILED,
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_MODEL_UNAVAILABLE,
    KNOWLEDGE_NAME_CONFLICT,
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_PARSE_FAILED,
    KNOWLEDGE_QUOTA_EXCEEDED,
    KNOWLEDGE_RERANK_FAILED,
    KNOWLEDGE_SEARCH_FAILED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KNOWLEDGE_TASK_FAILED,
    KnowledgeError,
    KnowledgeModelConfigurationView,
    KnowledgeModelConnectionResult,
    KnowledgeModelOption,
)
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.audit.models import AuditAuthorityRejected
from app.final_schema import FinalSchemaUnavailable
from app.knowledge import gateway
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectNotFound,
)
from app.projects.models import ProjectRole
from app.reliability.error_mapping import ReliabilityHTTPException

_REQUEST_ID = "knowledge-admin-contract"
_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_CONFIGURATION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_NOW = datetime(2026, 8, 29, 3, 0, tzinfo=UTC)


def _view(**overrides: object) -> KnowledgeModelConfigurationView:
    values: dict[str, object] = {
        "id": _CONFIGURATION_ID,
        "display_name": "Retrieval",
        "status": "active",
        "base_url": "https://provider.invalid/v1",
        "embedding_model": "embed-model",
        "embedding_dimension": 1024,
        "embedding_max_batch": 64,
        "reranker_model": "rerank-model",
        "reranker_max_batch": 32,
        "request_timeout_seconds": 30,
        "in_use": False,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return KnowledgeModelConfigurationView(**values)  # type: ignore[arg-type]


class _FakeKnowledgeModule:
    def __init__(self, *, error: KnowledgeError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_model_configurations(self, *, page: int = 1, page_size: int = 20):
        self.calls.append(("list", (page, page_size)))
        self._maybe_fail()
        return [_view()], 1

    async def create_model_configuration(self, create):  # noqa: ANN001
        self.calls.append(("create", create))
        self._maybe_fail()
        return _view()

    async def update_model_configuration(self, configuration_id, update):  # noqa: ANN001
        self.calls.append(("update", (configuration_id, update)))
        self._maybe_fail()
        return _view(display_name="Renamed")

    async def delete_model_configuration(self, configuration_id) -> None:  # noqa: ANN001
        self.calls.append(("delete", configuration_id))
        self._maybe_fail()

    async def test_model_configuration(self, configuration_id):  # noqa: ANN001
        self.calls.append(("test", configuration_id))
        self._maybe_fail()
        return KnowledgeModelConnectionResult(ok=True, message="通过")

    async def list_active_model_options(self):
        self.calls.append(("options", None))
        self._maybe_fail()
        return [
            KnowledgeModelOption(
                id=_CONFIGURATION_ID,
                display_name="Retrieval",
                embedding_model="embed-model",
                embedding_dimension=1024,
                reranker_model="rerank-model",
            )
        ]


def _app(module: _FakeKnowledgeModule | None) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.admin_router)
    app.include_router(gateway.project_router)
    app.dependency_overrides[gateway.require_knowledge_admin_context] = lambda: SimpleNamespace(request_id=_REQUEST_ID)
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: SimpleNamespace(request_id=_REQUEST_ID)
    app.state.knowledge_module = module
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_admin_list_returns_items_and_forwards_pagination() -> None:
    module = _FakeKnowledgeModule()
    async with _client(_app(module)) as client:
        response = await client.get("/api/admin/knowledge/models", params={"page": 2, "page_size": 5})

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == _REQUEST_ID
    assert payload["total"] == 1
    assert payload["page"] == 2
    assert payload["page_size"] == 5
    item = payload["items"][0]
    assert item["id"] == str(_CONFIGURATION_ID)
    assert item["in_use"] is False
    assert "api_key" not in item
    assert module.calls == [("list", (2, 5))]


@pytest.mark.asyncio
async def test_admin_create_passes_secret_api_key_to_the_module() -> None:
    module = _FakeKnowledgeModule()
    async with _client(_app(module)) as client:
        response = await client.post(
            "/api/admin/knowledge/models",
            json={
                "display_name": "Retrieval",
                "base_url": "https://provider.invalid/v1",
                "embedding_model": "embed-model",
                "embedding_dimension": 1024,
                "reranker_model": "rerank-model",
                "api_key": "top-secret-key",
            },
        )

    assert response.status_code == 200
    assert response.json()["item"]["display_name"] == "Retrieval"
    verb, create = module.calls[0]
    assert verb == "create"
    assert create.api_key == "top-secret-key"
    assert create.embedding_max_batch == 64
    assert create.reranker_max_batch == 32
    assert create.request_timeout_seconds == 30


@pytest.mark.asyncio
async def test_admin_update_sends_only_provided_fields() -> None:
    module = _FakeKnowledgeModule()
    async with _client(_app(module)) as client:
        response = await client.patch(
            f"/api/admin/knowledge/models/{_CONFIGURATION_ID}",
            json={"display_name": "Renamed", "api_key": "rotated-key"},
        )

    assert response.status_code == 200
    verb, (configuration_id, update) = module.calls[0]
    assert verb == "update"
    assert configuration_id == _CONFIGURATION_ID
    assert update.display_name == "Renamed"
    assert update.api_key == "rotated-key"
    assert update.status is None
    assert update.base_url is None
    assert update.embedding_dimension is None


@pytest.mark.asyncio
async def test_admin_delete_and_test_round_trip() -> None:
    module = _FakeKnowledgeModule()
    async with _client(_app(module)) as client:
        deleted = await client.delete(f"/api/admin/knowledge/models/{_CONFIGURATION_ID}")
        tested = await client.post(f"/api/admin/knowledge/models/{_CONFIGURATION_ID}/test")

    assert deleted.status_code == 200
    assert deleted.json() == {"request_id": _REQUEST_ID}
    assert tested.status_code == 200
    assert tested.json() == {"ok": True, "message": "通过", "request_id": _REQUEST_ID}
    assert module.calls == [("delete", _CONFIGURATION_ID), ("test", _CONFIGURATION_ID)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        (KNOWLEDGE_NOT_FOUND, 404),
        (KNOWLEDGE_NAME_CONFLICT, 409),
        (KNOWLEDGE_CONFLICT, 409),
        (KNOWLEDGE_INVALID_REQUEST, 422),
        (KNOWLEDGE_QUOTA_EXCEEDED, 429),
        (KNOWLEDGE_PARSE_FAILED, 422),
        (KNOWLEDGE_MODEL_UNAVAILABLE, 503),
        (KNOWLEDGE_STORAGE_UNAVAILABLE, 503),
        (KNOWLEDGE_EMBEDDING_FAILED, 502),
        (KNOWLEDGE_RERANK_FAILED, 502),
        (KNOWLEDGE_SEARCH_FAILED, 502),
        (KNOWLEDGE_TASK_FAILED, 502),
    ],
)
async def test_admin_routes_map_knowledge_errors_to_stable_codes(code: str, status: int) -> None:
    module = _FakeKnowledgeModule(error=KnowledgeError(code, "显示给管理员的消息"))
    async with _client(_app(module)) as client:
        response = await client.get("/api/admin/knowledge/models")

    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail == {"code": code, "message": "显示给管理员的消息", "request_id": _REQUEST_ID}


@pytest.mark.asyncio
async def test_disabled_feature_answers_knowledge_disabled_everywhere() -> None:
    async with _client(_app(None)) as client:
        admin = await client.get("/api/admin/knowledge/models")
        options = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")

    for response in (admin, options):
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == KNOWLEDGE_DISABLED


@pytest.mark.asyncio
async def test_project_model_options_return_active_options() -> None:
    module = _FakeKnowledgeModule()
    async with _client(_app(module)) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == _REQUEST_ID
    assert payload["items"] == [
        {
            "id": str(_CONFIGURATION_ID),
            "display_name": "Retrieval",
            "embedding_model": "embed-model",
            "embedding_dimension": 1024,
            "reranker_model": "rerank-model",
        }
    ]


def test_create_request_dto_is_strict_and_requires_api_key() -> None:
    with pytest.raises(ValidationError):
        gateway.KnowledgeModelCreateRequest.model_validate(
            {
                "display_name": "Retrieval",
                "base_url": "https://provider.invalid/v1",
                "embedding_model": "embed-model",
                "embedding_dimension": 1024,
                "reranker_model": "rerank-model",
                "api_key": "key",
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        gateway.KnowledgeModelCreateRequest.model_validate(
            {
                "display_name": "Retrieval",
                "base_url": "https://provider.invalid/v1",
                "embedding_model": "embed-model",
                "embedding_dimension": 1024,
                "reranker_model": "rerank-model",
                "api_key": "",
            }
        )
    # The update DTO mirrors the same rule: absent is fine, empty is not.
    with pytest.raises(ValidationError):
        gateway.KnowledgeModelUpdateRequest.model_validate({"api_key": ""})
    assert gateway.KnowledgeModelUpdateRequest.model_validate({}).api_key is None


def test_request_dtos_never_reveal_the_api_key() -> None:
    create = gateway.KnowledgeModelCreateRequest.model_validate(
        {
            "display_name": "Retrieval",
            "base_url": "https://provider.invalid/v1",
            "embedding_model": "embed-model",
            "embedding_dimension": 1024,
            "reranker_model": "rerank-model",
            "api_key": "top-secret-key",
        }
    )
    update = gateway.KnowledgeModelUpdateRequest.model_validate({"api_key": "rotated-key"})

    rendered = repr(create) + str(create.model_dump()) + repr(update) + str(update.model_dump())
    assert "top-secret-key" not in rendered
    assert "rotated-key" not in rendered


# ---------------------------------------------------------------------------
# Admin audit-context dependency
# ---------------------------------------------------------------------------


class _FakeSessionBegin:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> bool:
        return False


class _FakeSession:
    def begin(self) -> _FakeSessionBegin:
        return _FakeSessionBegin()


class _ReadyProbe:
    async def require_ready(self, session: object) -> None:
        return None


@pytest.mark.asyncio
async def test_admin_context_returns_the_resolved_audit_context(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = SimpleNamespace(request_id=_REQUEST_ID)

    async def _resolve(session, user_id, request_id):  # noqa: ANN001
        return expected

    monkeypatch.setattr(gateway, "resolve_current_system_audit_context", _resolve)
    monkeypatch.setattr(gateway, "FinalSchemaProbe", _ReadyProbe)

    resolved = await gateway.require_knowledge_admin_context(
        (uuid.uuid4(), _REQUEST_ID),
        session=_FakeSession(),  # type: ignore[arg-type]
    )
    assert resolved is expected


@pytest.mark.asyncio
async def test_admin_context_hides_non_admin_identities_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(session, user_id, request_id):  # noqa: ANN001
        raise AuditAuthorityRejected

    monkeypatch.setattr(gateway, "resolve_current_system_audit_context", _resolve)
    monkeypatch.setattr(gateway, "FinalSchemaProbe", _ReadyProbe)

    with pytest.raises(ReliabilityHTTPException) as error:
        await gateway.require_knowledge_admin_context(
            (uuid.uuid4(), _REQUEST_ID),
            session=_FakeSession(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == 404
    assert error.value.body["code"] == "RELIABILITY_NOT_FOUND"
    assert error.value.body["request_id"] == _REQUEST_ID


@pytest.mark.asyncio
async def test_admin_context_maps_schema_and_database_failures_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolve(session, user_id, request_id):  # noqa: ANN001
        return SimpleNamespace(request_id=_REQUEST_ID)

    class _BrokenProbe:
        async def require_ready(self, session: object) -> None:
            raise FinalSchemaUnavailable

    monkeypatch.setattr(gateway, "resolve_current_system_audit_context", _resolve)
    monkeypatch.setattr(gateway, "FinalSchemaProbe", _BrokenProbe)

    with pytest.raises(ReliabilityHTTPException) as error:
        await gateway.require_knowledge_admin_context(
            (uuid.uuid4(), _REQUEST_ID),
            session=_FakeSession(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == 503
    assert error.value.body["code"] == "DATABASE_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Project read-context dependency
# ---------------------------------------------------------------------------


def _project_context(capabilities: frozenset[Capability]) -> ProjectContext:
    return ProjectContext(
        user_id=uuid.uuid4(),
        project_id=_PROJECT_ID,
        membership_id=uuid.uuid4(),
        role=ProjectRole.VIEWER,
        capabilities=capabilities,
        membership_version=1,
        request_id=_REQUEST_ID,
    )


@pytest.mark.asyncio
async def test_project_read_context_passes_with_shared_assets_read(monkeypatch: pytest.MonkeyPatch) -> None:
    context = _project_context(frozenset({Capability.SHARED_ASSETS_READ}))

    async def _resolve(session, user_id, project_id, request_id):  # noqa: ANN001
        return context

    monkeypatch.setattr(gateway, "resolve_project_context", _resolve)

    resolved = await gateway.require_project_knowledge_read(
        _PROJECT_ID,
        (context.user_id, _REQUEST_ID),
        session=object(),  # type: ignore[arg-type]
    )
    assert resolved is context


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (ProjectNotFound(), 404, KNOWLEDGE_NOT_FOUND),
        (ProjectDatabaseUnavailable(), 503, KNOWLEDGE_STORAGE_UNAVAILABLE),
    ],
)
async def test_project_read_context_maps_resolution_failures(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
    status: int,
    code: str,
) -> None:
    async def _resolve(session, user_id, project_id, request_id):  # noqa: ANN001
        raise failure

    monkeypatch.setattr(gateway, "resolve_project_context", _resolve)

    with pytest.raises(HTTPException) as error:
        await gateway.require_project_knowledge_read(
            _PROJECT_ID,
            (uuid.uuid4(), _REQUEST_ID),
            session=object(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == status
    assert error.value.detail["code"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dependency", "capabilities"),
    [
        (gateway.require_project_knowledge_read, frozenset()),
        (
            gateway.require_project_knowledge_edit,
            frozenset({Capability.SHARED_ASSETS_READ}),
        ),
    ],
)
async def test_project_context_rejects_member_capability_gaps_as_forbidden(
    monkeypatch: pytest.MonkeyPatch,
    dependency,  # noqa: ANN001
    capabilities: frozenset[Capability],
) -> None:
    """A current member lacking the capability gets 403, not a hidden 404."""

    context = _project_context(capabilities)

    async def _resolve(session, user_id, project_id, request_id):  # noqa: ANN001
        return context

    monkeypatch.setattr(gateway, "resolve_project_context", _resolve)

    with pytest.raises(HTTPException) as error:
        await dependency(
            _PROJECT_ID,
            (context.user_id, _REQUEST_ID),
            session=object(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == 403
    assert error.value.detail["code"] == gateway.KNOWLEDGE_FORBIDDEN
