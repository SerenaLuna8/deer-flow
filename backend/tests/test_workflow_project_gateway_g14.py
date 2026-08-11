from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.gateway import deps
from app.gateway.deps import (
    get_current_user_from_request,
    get_workflow_project_control_service,
    project_session,
    workflow_project_context,
)
from app.gateway.routers import project_workflows
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.errors import ProjectDatabaseUnavailable, ProjectNotFound
from app.projects.models import ProjectRole
from app.workflows.catalog_contracts import (
    NodeAvailability,
    NodeCatalogResponseV1,
    WorkflowCatalogCapabilityProjectionV1,
    first_batch_node_registry_manifest_v1,
)
from app.workflows.contracts import WorkflowControlPlaneReadyV1
from app.workflows.errors import WorkflowUnavailable

PROJECT_ID = uuid.UUID("00000000-0000-4000-8000-000000000101")
USER_ID = uuid.UUID("00000000-0000-4000-8000-000000000102")
MEMBERSHIP_ID = uuid.UUID("00000000-0000-4000-8000-000000000103")
WORKFLOW_NODE_DISABLED_REASON_CODES = (
    "WORKFLOW_DISABLED",
    "WORKFLOW_NODE_CAPABILITY_REQUIRED",
    "WORKFLOW_NODE_NOT_ALLOWED",
    "WORKFLOW_CODE_DISABLED",
    "WORKFLOW_CODE_PROFILE_UNAVAILABLE",
    "WORKFLOW_HTTP_DISABLED",
    "WORKFLOW_HTTP_PROFILE_UNAVAILABLE",
)


def _context(*capabilities: Capability, request_id: str = "req-workflow") -> ProjectContext:
    return ProjectContext(
        user_id=USER_ID,
        project_id=PROJECT_ID,
        membership_id=MEMBERSHIP_ID,
        role=ProjectRole.EDITOR,
        capabilities=frozenset(capabilities),
        membership_version=7,
        request_id=request_id,
    )


def _catalog() -> NodeCatalogResponseV1:
    return NodeCatalogResponseV1.model_validate(
        {
            "schema_version": 1,
            "catalog_generation": "a" * 64,
            "availability_generation": "b" * 64,
            "entries": [
                {
                    "definition": definition,
                    "availability": {"state": "enabled"},
                    **(
                        {
                            "http_authoring": {
                                "endpoints": [
                                    {
                                        "id": "public-api",
                                        "origin": "https://api.example.com",
                                        "allowed_methods": ["GET", "POST"],
                                        "write_idempotency": "server_derived_key",
                                        "injection_profiles": [
                                            {
                                                "id": "api-key-v1",
                                                "scheme": "api_key",
                                                "target_header": "x-api-key",
                                                "credential_payload_contract": "api_key_v1",
                                            }
                                        ],
                                    }
                                ]
                            }
                        }
                        if definition["type"] == "http_request"
                        else {}
                    ),
                }
                for definition in first_batch_node_registry_manifest_v1()
            ],
        }
    )


class _ProjectControlService:
    def __init__(self) -> None:
        self.readiness_request_id: str | None = None
        self.catalog_request_id: str | None = None
        self.catalog_capabilities: WorkflowCatalogCapabilityProjectionV1 | None = None
        self.error: WorkflowUnavailable | None = None

    async def read_readiness(self, session, *, request_id: str):
        assert session is SESSION
        if self.error is not None:
            raise self.error
        self.readiness_request_id = request_id
        return WorkflowControlPlaneReadyV1(
            status="ready",
            code="WORKFLOW_CONTROL_PLANE_READY",
            workflow_enabled=True,
            schema_ready=True,
            admission_ready=False,
            request_id=request_id,
        )

    async def read_node_catalog(
        self,
        session,
        *,
        request_id: str,
        capabilities: WorkflowCatalogCapabilityProjectionV1,
    ):
        assert session is SESSION
        if self.error is not None:
            raise self.error
        self.catalog_request_id = request_id
        self.catalog_capabilities = capabilities
        return _catalog()


SESSION = object()


def _app(
    service: _ProjectControlService | None,
    *,
    context: ProjectContext | None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(project_workflows.router)

    async def session_override():
        yield SESSION

    app.dependency_overrides[project_session] = session_override
    app.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(id=USER_ID)
    if context is not None:
        app.dependency_overrides[workflow_project_context] = lambda: context
    if service is not None:
        app.dependency_overrides[get_workflow_project_control_service] = lambda: service
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_readiness_is_static_strict_and_forwards_only_server_request_id() -> None:
    service = _ProjectControlService()
    context = _context(Capability.WORKFLOW_READ, request_id="req-readiness")

    response = await _get(
        _app(service, context=context),
        f"/api/projects/{PROJECT_ID}/workflows/readiness",
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "code": "WORKFLOW_CONTROL_PLANE_READY",
        "workflow_enabled": True,
        "schema_ready": True,
        "admission_ready": False,
        "request_id": "req-readiness",
    }
    assert service.readiness_request_id == "req-readiness"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capabilities", "expected_code_use", "expected_http_use"),
    [
        ((Capability.WORKFLOW_READ,), False, False),
        (
            (Capability.WORKFLOW_READ, Capability.WORKFLOW_CODE_USE),
            True,
            False,
        ),
        (
            (Capability.WORKFLOW_READ, Capability.WORKFLOW_HTTP_USE),
            False,
            True,
        ),
        (
            (
                Capability.WORKFLOW_READ,
                Capability.WORKFLOW_CODE_USE,
                Capability.WORKFLOW_HTTP_USE,
            ),
            True,
            True,
        ),
    ],
)
async def test_node_catalog_passes_only_frozen_code_and_http_capability_projection(
    capabilities: tuple[Capability, ...],
    expected_code_use: bool,
    expected_http_use: bool,
) -> None:
    service = _ProjectControlService()
    context = _context(*capabilities, request_id="req-catalog")

    response = await _get(
        _app(service, context=context),
        f"/api/projects/{PROJECT_ID}/workflows/node-catalog",
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "catalog_generation",
        "availability_generation",
        "entries",
    }
    assert len(payload["entries"]) == 9
    first_entry = payload["entries"][0]
    assert first_entry["availability"] == {"state": "enabled"}
    assert "public_limits" not in first_entry
    assert first_entry["definition"]["output_ports"][0]["value_type"] is None
    http_entry = next(entry for entry in payload["entries"] if entry["definition"]["type"] == "http_request")
    assert http_entry["http_authoring"] == {
        "endpoints": [
            {
                "id": "public-api",
                "origin": "https://api.example.com",
                "allowed_methods": ["GET", "POST"],
                "write_idempotency": "server_derived_key",
                "injection_profiles": [
                    {
                        "id": "api-key-v1",
                        "scheme": "api_key",
                        "target_header": "x-api-key",
                        "credential_payload_contract": "api_key_v1",
                    }
                ],
            }
        ]
    }
    assert service.catalog_request_id == "req-catalog"
    assert type(service.catalog_capabilities) is WorkflowCatalogCapabilityProjectionV1
    assert service.catalog_capabilities.code_use is expected_code_use
    assert service.catalog_capabilities.http_use is expected_http_use


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["readiness", "node-catalog"])
async def test_api_rejects_forged_raw_string_workflow_capabilities(route: str) -> None:
    service = _ProjectControlService()
    forged_context = ProjectContext(
        user_id=USER_ID,
        project_id=PROJECT_ID,
        membership_id=MEMBERSHIP_ID,
        role=ProjectRole.EDITOR,
        capabilities=frozenset(
            {
                Capability.WORKFLOW_READ.value,
                Capability.WORKFLOW_CODE_USE.value,
                Capability.WORKFLOW_HTTP_USE.value,
            }
        ),
        membership_version=7,
        request_id="req-forged-capabilities",
    )

    response = await _get(
        _app(service, context=forged_context),
        f"/api/projects/{PROJECT_ID}/workflows/{route}",
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": {
            "code": "WORKFLOW_FORBIDDEN",
            "message": "Workflow action is forbidden.",
            "request_id": "req-forged-capabilities",
        }
    }
    assert service.readiness_request_id is None
    assert service.catalog_request_id is None
    assert service.catalog_capabilities is None


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["readiness", "node-catalog"])
async def test_workflow_unavailable_has_stable_secret_free_503_mapping(route: str) -> None:
    service = _ProjectControlService()
    service.error = WorkflowUnavailable("req-unavailable")
    context = _context(Capability.WORKFLOW_READ, request_id="req-unavailable")

    response = await _get(
        _app(service, context=context),
        f"/api/projects/{PROJECT_ID}/workflows/{route}",
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json() == {
        "detail": {
            "code": "WORKFLOW_UNAVAILABLE",
            "message": "Workflow is temporarily unavailable.",
            "request_id": "req-unavailable",
        }
    }


@pytest.mark.asyncio
async def test_missing_workflow_control_service_uses_stable_503_dependency_mapping() -> None:
    response = await _get(
        _app(None, context=_context(Capability.WORKFLOW_READ)),
        f"/api/projects/{PROJECT_ID}/workflows/readiness",
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    detail = response.json()["detail"]
    assert detail["code"] == "WORKFLOW_UNAVAILABLE"
    assert detail["message"] == "Workflow is temporarily unavailable."
    assert isinstance(detail["request_id"], str) and detail["request_id"]
    assert set(detail) == {"code", "message", "request_id"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resolved", "status_code", "code"),
    [
        (ProjectNotFound(), 404, "WORKFLOW_NOT_FOUND"),
        (
            _context(request_id="req-forbidden"),
            403,
            "WORKFLOW_FORBIDDEN",
        ),
        (
            ProjectDatabaseUnavailable(),
            503,
            "WORKFLOW_UNAVAILABLE",
        ),
    ],
)
async def test_project_context_maps_absence_forbidden_and_storage_failure_without_leak(
    monkeypatch: pytest.MonkeyPatch,
    resolved: object,
    status_code: int,
    code: str,
) -> None:
    async def resolve(*args, **kwargs):
        del args, kwargs
        if isinstance(resolved, Exception):
            raise resolved
        return resolved

    monkeypatch.setattr(deps, "resolve_project_context", resolve)

    with pytest.raises(HTTPException) as raised:
        await workflow_project_context(
            PROJECT_ID,
            user=SimpleNamespace(id=USER_ID),
            session=SESSION,
        )

    assert raised.value.status_code == status_code
    assert raised.value.detail["code"] == code
    assert set(raised.value.detail) == {"code", "message", "request_id"}


@pytest.mark.asyncio
async def test_invalid_project_id_uses_stable_validation_error() -> None:
    service = _ProjectControlService()
    response = await _get(
        _app(service, context=None),
        "/api/projects/not-a-uuid/workflows/readiness",
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "WORKFLOW_INPUT_INVALID"
    assert set(response.json()["detail"]) == {"code", "message", "request_id"}


def test_static_project_workflow_routes_are_registered_before_future_dynamic_routes() -> None:
    paths = [route.path for route in project_workflows.router.routes]

    assert paths[:2] == [
        "/api/projects/{project_id}/workflows/readiness",
        "/api/projects/{project_id}/workflows/node-catalog",
    ]
    assert all("{workflow_id}" not in path for path in paths[:2])


def test_frontend_disabled_reason_enum_is_closed_and_matches_backend() -> None:
    frontend_catalog = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "core" / "project-workflows" / "catalog.ts").read_text(encoding="utf-8")
    declaration = re.search(
        r"export const workflowNodeDisabledReasonCodes = \[(?P<values>.*?)\] as const;",
        frontend_catalog,
        flags=re.DOTALL,
    )

    assert declaration is not None
    frontend_values = tuple(re.findall(r'"([A-Z][A-Z0-9_]*)"', declaration.group("values")))
    assert frontend_values == WORKFLOW_NODE_DISABLED_REASON_CODES
    for reason_code in WORKFLOW_NODE_DISABLED_REASON_CODES:
        assert NodeAvailability.model_validate({"state": "disabled", "reason_code": reason_code}).reason_code == reason_code
