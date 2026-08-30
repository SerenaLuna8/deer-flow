"""M2/M9 gates: registry admin and project knowledge HTTP contracts.

The retrieval model registry admin surface (``app/model_registry/gateway.py``)
is exercised over ASGI with the authorization dependency overridden and the
service faked; the project ``model-options`` route and the project-context
dependencies are tested against the knowledge gateway the same way.
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
)
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.audit.models import AuditAuthorityRejected
from app.final_schema import FinalSchemaUnavailable
from app.gateway.deps import project_session
from app.knowledge import gateway
from app.model_registry import gateway as registry_gateway
from app.model_registry.service import (
    ModelProviderView,
    ProviderModelTestResult,
    ProviderModelView,
    RetrievalModelOption,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import (
    ProjectDatabaseUnavailable,
    ProjectNotFound,
)
from app.projects.models import ProjectRole
from app.reliability.error_mapping import ReliabilityHTTPException

_REQUEST_ID = "model-registry-contract"
_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_PROVIDER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_MODEL_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
_NOW = datetime(2026, 8, 30, 3, 0, tzinfo=UTC)


def _provider_view(**overrides: object) -> ModelProviderView:
    values: dict[str, object] = {
        "id": _PROVIDER_ID,
        "name": "SiliconFlow",
        "base_url": "https://provider.invalid/v1",
        "request_timeout_seconds": 30,
        "api_key_configured": True,
        "model_count": 2,
        "active_model_count": 1,
        "endpoint_frozen": True,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return ModelProviderView(**values)  # type: ignore[arg-type]


def _model_view(**overrides: object) -> ProviderModelView:
    values: dict[str, object] = {
        "id": _MODEL_ID,
        "provider_id": _PROVIDER_ID,
        "model_type": "embedding",
        "model_name": "embed-model",
        "embedding_dimension": 1024,
        "max_batch": 64,
        "status": "active",
        "in_use": False,
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    values.update(overrides)
    return ProviderModelView(**values)  # type: ignore[arg-type]


class _FakeRegistryService:
    def __init__(self, *, error: KnowledgeError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, object]] = []

    def _maybe_fail(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_providers(self, context):  # noqa: ANN001
        self.calls.append(("list_providers", None))
        self._maybe_fail()
        return [_provider_view()]

    async def create_provider(self, context, *, name, base_url, request_timeout_seconds, api_key):  # noqa: ANN001
        self.calls.append(("create_provider", (name, base_url, request_timeout_seconds, api_key)))
        self._maybe_fail()
        return _provider_view(name=name, model_count=0)

    async def update_provider(self, context, provider_id, *, name, base_url, request_timeout_seconds, api_key):  # noqa: ANN001
        self.calls.append(("update_provider", (provider_id, name, base_url, request_timeout_seconds, api_key)))
        self._maybe_fail()
        return _provider_view(name="Renamed")

    async def delete_provider(self, context, provider_id) -> None:  # noqa: ANN001
        self.calls.append(("delete_provider", provider_id))
        self._maybe_fail()

    async def list_models(self, context, provider_id):  # noqa: ANN001
        self.calls.append(("list_models", provider_id))
        self._maybe_fail()
        return [_model_view()]

    async def create_model(self, context, provider_id, *, model_type, model_name, embedding_dimension, max_batch):  # noqa: ANN001
        self.calls.append(("create_model", (provider_id, model_type, model_name, embedding_dimension, max_batch)))
        self._maybe_fail()
        return _model_view(model_type=model_type, model_name=model_name)

    async def set_model_status(self, context, model_id, status):  # noqa: ANN001
        self.calls.append(("set_model_status", (model_id, status)))
        self._maybe_fail()
        return _model_view(status=status)

    async def delete_model(self, context, model_id) -> None:  # noqa: ANN001
        self.calls.append(("delete_model", model_id))
        self._maybe_fail()

    async def test_model(self, context, model_id):  # noqa: ANN001
        self.calls.append(("test_model", model_id))
        self._maybe_fail()
        return ProviderModelTestResult(ok=True, message="通过")


def _registry_app(service: _FakeRegistryService | None) -> FastAPI:
    """Admin registry app; ``service=None`` leaves the Knowledge switch off."""

    app = FastAPI()
    app.include_router(registry_gateway.router)
    app.dependency_overrides[registry_gateway.require_model_registry_admin_context] = lambda: SimpleNamespace(request_id=_REQUEST_ID)
    if service is None:
        app.state.knowledge_module = None
    else:
        app.dependency_overrides[registry_gateway.get_model_registry_service] = lambda: service
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Admin registry: provider routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_provider_list_returns_items_without_key_material() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        response = await client.get("/api/admin/settings/model-providers")

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == _REQUEST_ID
    item = payload["items"][0]
    assert item["id"] == str(_PROVIDER_ID)
    assert item["api_key_configured"] is True
    assert item["model_count"] == 2
    assert item["active_model_count"] == 1
    assert item["endpoint_frozen"] is True
    assert "api_key" not in item
    assert service.calls == [("list_providers", None)]


@pytest.mark.asyncio
async def test_admin_provider_create_passes_the_secret_key_and_defaults() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        response = await client.post(
            "/api/admin/settings/model-providers",
            json={
                "name": "SiliconFlow",
                "base_url": "https://provider.invalid/v1",
                "api_key": "top-secret-key",
            },
        )

    assert response.status_code == 200
    assert response.json()["item"]["name"] == "SiliconFlow"
    verb, (name, base_url, timeout, api_key) = service.calls[0]
    assert verb == "create_provider"
    assert name == "SiliconFlow"
    assert base_url == "https://provider.invalid/v1"
    assert timeout == 30
    assert api_key == "top-secret-key"


@pytest.mark.asyncio
async def test_admin_provider_update_sends_only_provided_fields() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        response = await client.patch(
            f"/api/admin/settings/model-providers/{_PROVIDER_ID}",
            json={"name": "Renamed", "api_key": "rotated-key"},
        )

    assert response.status_code == 200
    assert response.json()["item"]["name"] == "Renamed"
    verb, (provider_id, name, base_url, timeout, api_key) = service.calls[0]
    assert verb == "update_provider"
    assert provider_id == _PROVIDER_ID
    assert name == "Renamed"
    assert base_url is None
    assert timeout is None
    assert api_key == "rotated-key"


@pytest.mark.asyncio
async def test_admin_provider_delete_round_trips() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        response = await client.delete(f"/api/admin/settings/model-providers/{_PROVIDER_ID}")

    assert response.status_code == 200
    assert response.json() == {"request_id": _REQUEST_ID}
    assert service.calls == [("delete_provider", _PROVIDER_ID)]


# ---------------------------------------------------------------------------
# Admin registry: model routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_model_list_is_scoped_to_the_provider() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        response = await client.get(f"/api/admin/settings/model-providers/{_PROVIDER_ID}/models")

    assert response.status_code == 200
    payload = response.json()
    item = payload["items"][0]
    assert item["provider_id"] == str(_PROVIDER_ID)
    assert item["model_type"] == "embedding"
    assert item["in_use"] is False
    assert service.calls == [("list_models", _PROVIDER_ID)]


@pytest.mark.asyncio
async def test_admin_model_create_defaults_max_batch_by_type() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        embedding = await client.post(
            f"/api/admin/settings/model-providers/{_PROVIDER_ID}/models",
            json={"model_type": "embedding", "model_name": "embed-model", "embedding_dimension": 1024},
        )
        rerank = await client.post(
            f"/api/admin/settings/model-providers/{_PROVIDER_ID}/models",
            json={"model_type": "rerank", "model_name": "rerank-model"},
        )
        explicit = await client.post(
            f"/api/admin/settings/model-providers/{_PROVIDER_ID}/models",
            json={"model_type": "rerank", "model_name": "rerank-model", "max_batch": 8},
        )

    assert embedding.status_code == 200
    assert rerank.status_code == 200
    assert explicit.status_code == 200
    assert service.calls == [
        ("create_model", (_PROVIDER_ID, "embedding", "embed-model", 1024, 64)),
        ("create_model", (_PROVIDER_ID, "rerank", "rerank-model", None, 32)),
        ("create_model", (_PROVIDER_ID, "rerank", "rerank-model", None, 8)),
    ]


@pytest.mark.asyncio
async def test_admin_model_status_delete_and_test_round_trip() -> None:
    service = _FakeRegistryService()
    async with _client(_registry_app(service)) as client:
        disabled = await client.patch(
            f"/api/admin/settings/provider-models/{_MODEL_ID}",
            json={"status": "disabled"},
        )
        deleted = await client.delete(f"/api/admin/settings/provider-models/{_MODEL_ID}")
        tested = await client.post(f"/api/admin/settings/provider-models/{_MODEL_ID}/test")

    assert disabled.status_code == 200
    assert disabled.json()["item"]["status"] == "disabled"
    assert deleted.status_code == 200
    assert deleted.json() == {"request_id": _REQUEST_ID}
    assert tested.status_code == 200
    assert tested.json() == {"ok": True, "message": "通过", "request_id": _REQUEST_ID}
    assert service.calls == [
        ("set_model_status", (_MODEL_ID, "disabled")),
        ("delete_model", _MODEL_ID),
        ("test_model", _MODEL_ID),
    ]


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
    service = _FakeRegistryService(error=KnowledgeError(code, "显示给管理员的消息"))
    async with _client(_registry_app(service)) as client:
        response = await client.get("/api/admin/settings/model-providers")

    assert response.status_code == status
    detail = response.json()["detail"]
    assert detail == {"code": code, "message": "显示给管理员的消息", "request_id": _REQUEST_ID}


@pytest.mark.asyncio
async def test_disabled_feature_answers_knowledge_disabled_on_the_admin_surface() -> None:
    async with _client(_registry_app(None)) as client:
        response = await client.get("/api/admin/settings/model-providers")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == KNOWLEDGE_DISABLED


# ---------------------------------------------------------------------------
# Admin registry: request DTO strictness and secret hygiene
# ---------------------------------------------------------------------------


def test_provider_create_request_is_strict_and_requires_api_key() -> None:
    with pytest.raises(ValidationError):
        registry_gateway.ModelProviderCreateRequest.model_validate(
            {
                "name": "SiliconFlow",
                "base_url": "https://provider.invalid/v1",
                "api_key": "key",
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        registry_gateway.ModelProviderCreateRequest.model_validate(
            {
                "name": "SiliconFlow",
                "base_url": "https://provider.invalid/v1",
                "api_key": "",
            }
        )
    # The update DTO mirrors the same rule: absent is fine, empty is not.
    with pytest.raises(ValidationError):
        registry_gateway.ModelProviderUpdateRequest.model_validate({"api_key": ""})
    assert registry_gateway.ModelProviderUpdateRequest.model_validate({}).api_key is None


def test_provider_request_dtos_never_reveal_the_api_key() -> None:
    create = registry_gateway.ModelProviderCreateRequest.model_validate(
        {
            "name": "SiliconFlow",
            "base_url": "https://provider.invalid/v1",
            "api_key": "top-secret-key",
        }
    )
    update = registry_gateway.ModelProviderUpdateRequest.model_validate({"api_key": "rotated-key"})

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

    monkeypatch.setattr(registry_gateway, "resolve_current_system_audit_context", _resolve)
    monkeypatch.setattr(registry_gateway, "FinalSchemaProbe", _ReadyProbe)

    resolved = await registry_gateway.require_model_registry_admin_context(
        (uuid.uuid4(), _REQUEST_ID),
        session=_FakeSession(),  # type: ignore[arg-type]
    )
    assert resolved is expected


@pytest.mark.asyncio
async def test_admin_context_hides_non_admin_identities_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _resolve(session, user_id, request_id):  # noqa: ANN001
        raise AuditAuthorityRejected

    monkeypatch.setattr(registry_gateway, "resolve_current_system_audit_context", _resolve)
    monkeypatch.setattr(registry_gateway, "FinalSchemaProbe", _ReadyProbe)

    with pytest.raises(ReliabilityHTTPException) as error:
        await registry_gateway.require_model_registry_admin_context(
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

    monkeypatch.setattr(registry_gateway, "resolve_current_system_audit_context", _resolve)
    monkeypatch.setattr(registry_gateway, "FinalSchemaProbe", _BrokenProbe)

    with pytest.raises(ReliabilityHTTPException) as error:
        await registry_gateway.require_model_registry_admin_context(
            (uuid.uuid4(), _REQUEST_ID),
            session=_FakeSession(),  # type: ignore[arg-type]
        )
    assert error.value.status_code == 503
    assert error.value.body["code"] == "DATABASE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_registry_routes_fail_closed_without_the_audit_service() -> None:
    """Registry writes are governed asset changes: a missing audit sink must
    answer 503 instead of assembling a service that drops the trail."""

    app = FastAPI()
    app.include_router(registry_gateway.router)
    app.dependency_overrides[registry_gateway.require_model_registry_admin_context] = lambda: SimpleNamespace(request_id=_REQUEST_ID)
    app.state.knowledge_module = SimpleNamespace(model_client=object(), model_in_use=object())
    # ``project_audit_service`` is deliberately absent from app.state.

    async with _client(app) as client:
        response = await client.get("/api/admin/settings/model-providers")

    assert response.status_code == 503
    assert response.json()["detail"] == "Project audit service not available"


# ---------------------------------------------------------------------------
# Project model-options route
# ---------------------------------------------------------------------------


class _FakeAuthority:
    """Stands in for ProjectKnowledgeAuthority inside the route module."""

    def __init__(self, *, error: KnowledgeError | None = None, calls: list[str] | None = None) -> None:
        self.error = error
        self.calls = calls if calls is not None else []
        self.capability: object | None = None

    def __call__(self, context, capability):  # noqa: ANN001 - mirrors the class constructor
        self.capability = capability
        return self

    async def revalidate(self, session) -> None:  # noqa: ANN001
        self.calls.append("revalidate")
        if self.error is not None:
            raise self.error


def _project_app(*, module: object | None) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("44444444-4444-4444-8444-444444444444"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id=_REQUEST_ID,
    )
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[project_session] = lambda: _FakeSession()
    app.state.knowledge_module = module
    return app


@pytest.mark.asyncio
async def test_project_model_options_return_active_models_split_by_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    embedding_option = RetrievalModelOption(
        id=_MODEL_ID,
        provider_name="SiliconFlow",
        model_name="embed-model",
        embedding_dimension=1024,
    )
    rerank_option = RetrievalModelOption(
        id=uuid.UUID("66666666-6666-4666-8666-666666666666"),
        provider_name="SiliconFlow",
        model_name="rerank-model",
        embedding_dimension=None,
    )

    async def _options(session):  # noqa: ANN001
        return [embedding_option], [rerank_option]

    monkeypatch.setattr(gateway, "ProjectKnowledgeAuthority", _FakeAuthority())
    monkeypatch.setattr(gateway, "list_active_retrieval_model_options", _options)

    async with _client(_project_app(module=object())) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == _REQUEST_ID
    assert payload["embedding_models"] == [
        {
            "id": str(_MODEL_ID),
            "provider_name": "SiliconFlow",
            "model_name": "embed-model",
            "embedding_dimension": 1024,
        }
    ]
    assert payload["reranker_models"] == [
        {
            "id": "66666666-6666-4666-8666-666666666666",
            "provider_name": "SiliconFlow",
            "model_name": "rerank-model",
            "embedding_dimension": None,
        }
    ]


@pytest.mark.asyncio
async def test_project_model_options_revalidate_membership_before_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The read transaction re-checks membership: a member revoked between
    context issuance and use gets 404 and never reads the registry list."""

    calls: list[str] = []
    passing = _FakeAuthority(calls=calls)
    monkeypatch.setattr(gateway, "ProjectKnowledgeAuthority", passing)

    async def _options(session):  # noqa: ANN001
        calls.append("options")
        return [], []

    monkeypatch.setattr(gateway, "list_active_retrieval_model_options", _options)

    async with _client(_project_app(module=object())) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")
    assert response.status_code == 200
    assert calls == ["revalidate", "options"]
    assert passing.capability is Capability.SHARED_ASSETS_READ

    revoked = _FakeAuthority(error=KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在"))
    monkeypatch.setattr(gateway, "ProjectKnowledgeAuthority", revoked)

    async with _client(_project_app(module=object())) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == KNOWLEDGE_NOT_FOUND


@pytest.mark.asyncio
async def test_project_model_options_map_storage_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _options(session):  # noqa: ANN001
        raise SQLAlchemyError("boom")

    monkeypatch.setattr(gateway, "ProjectKnowledgeAuthority", _FakeAuthority())
    monkeypatch.setattr(gateway, "list_active_retrieval_model_options", _options)

    async with _client(_project_app(module=object())) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == KNOWLEDGE_STORAGE_UNAVAILABLE


@pytest.mark.asyncio
async def test_disabled_feature_answers_knowledge_disabled_on_the_project_surface() -> None:
    async with _client(_project_app(module=None)) as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/model-options")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == KNOWLEDGE_DISABLED


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
