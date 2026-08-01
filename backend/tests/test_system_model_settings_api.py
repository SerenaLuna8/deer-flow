from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit.models import resolve_system_audit_context
from app.gateway.deps import (
    get_current_agent_runtime_config,
    get_current_user_from_request,
    get_system_model_catalog,
)
from app.gateway.routers import admin_model_settings, models
from app.system_settings.models import (
    PublicSystemModelView,
    SystemModelCatalogView,
    SystemModelVersionView,
    SystemModelView,
)

USER_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
MODEL_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
VERSION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
CREDENTIAL_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
CREDENTIAL_VERSION_ID = uuid.UUID(
    "50000000-0000-0000-0000-000000000001",
)
NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _admin_context():
    return resolve_system_audit_context(
        SimpleNamespace(id=USER_ID, system_role="system_admin"),
        request_id="request-model-settings",
    )


def _model_view() -> SystemModelView:
    return SystemModelView(
        id=MODEL_ID,
        logical_name="primary",
        display_name="Primary",
        description="Primary model",
        status="active",
        current_version_id=VERSION_ID,
        revision=2,
        sort_order=10,
        current_version=SystemModelVersionView(
            id=VERSION_ID,
            model_config_id=MODEL_ID,
            version_number=3,
            provider_adapter="patched_deepseek",
            provider_model="deepseek-v4-pro",
            settings={"base_url": "https://example.invalid/v1"},
            supports_thinking=True,
            supports_reasoning_effort=False,
            supports_vision=True,
            credential_id=CREDENTIAL_ID,
            credential_version_id=CREDENTIAL_VERSION_ID,
            credential_env_key="DEEPSEEK_API_KEY",
            payload_checksum="a" * 64,
            supersedes_version_id=None,
            created_by_user_id=str(USER_ID),
            created_at=NOW,
        ),
        created_by_user_id=str(USER_ID),
        updated_by_user_id=str(USER_ID),
        created_at=NOW,
        updated_at=NOW,
    )


class _Catalog:
    async def list_models(self, _context):
        return SystemModelCatalogView(
            catalog_revision=4,
            default_model_config_id=MODEL_ID,
            items=(_model_view(),),
        )

    async def list_available_models(self):
        return (
            PublicSystemModelView(
                logical_name="primary",
                display_name="Primary",
                description="Primary model",
                supports_thinking=True,
                supports_reasoning_effort=False,
                supports_vision=True,
                is_default=True,
            ),
        )


class _MutatingCatalog(_Catalog):
    def __init__(self) -> None:
        self.created = None

    async def create_model(self, _context, command):
        self.created = command
        return _model_view()


def test_admin_model_catalog_exposes_configuration_only_on_admin_route() -> None:
    app = FastAPI()
    app.include_router(admin_model_settings.router)
    app.dependency_overrides[admin_model_settings.current_model_admin_context] = _admin_context
    app.dependency_overrides[get_system_model_catalog] = _Catalog

    response = TestClient(app).get("/api/admin/settings/models")

    assert response.status_code == 200
    body = response.json()
    assert body["catalog_revision"] == 4
    assert body["items"] == [
        {
            "id": str(MODEL_ID),
            "logical_name": "primary",
            "display_name": "Primary",
            "description": "Primary model",
            "provider_adapter": "patched_deepseek",
            "provider_model": "deepseek-v4-pro",
            "settings": {
                "base_url": "https://example.invalid/v1",
            },
            "supports_thinking": True,
            "supports_reasoning_effort": False,
            "supports_vision": True,
            "status": "active",
            "is_default": True,
            "revision": 2,
            "version_number": 3,
            "credential_id": str(CREDENTIAL_ID),
            "credential_version_id": str(CREDENTIAL_VERSION_ID),
            "credential_env_key": "DEEPSEEK_API_KEY",
            "sort_order": 10,
            "updated_at": "2026-07-31T00:00:00Z",
        },
    ]


def test_public_models_projection_never_exposes_provider_or_credential() -> None:
    app = FastAPI()
    app.include_router(models.router)
    app.dependency_overrides[get_system_model_catalog] = _Catalog
    app.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(id=str(USER_ID))
    app.dependency_overrides[get_current_agent_runtime_config] = lambda: SimpleNamespace(
        token_usage=SimpleNamespace(enabled=True),
    )

    response = TestClient(app).get("/api/models")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "models": [
            {
                "name": "primary",
                "model": "primary",
                "display_name": "Primary",
                "description": "Primary model",
                "supports_thinking": True,
                "supports_reasoning_effort": False,
                "supports_vision": True,
                "is_default": True,
            },
        ],
        "token_usage": {"enabled": True},
    }
    encoded = response.text.lower()
    for forbidden in (
        "provider_adapter",
        "provider_model",
        "settings",
        "credential",
        "deepseek-v4-pro",
        "base_url",
    ):
        assert forbidden not in encoded


def test_admin_model_create_accepts_json_uuid_credential_references() -> None:
    app = FastAPI()
    app.include_router(admin_model_settings.router)
    catalog = _MutatingCatalog()
    app.dependency_overrides[admin_model_settings.current_model_admin_context] = _admin_context
    app.dependency_overrides[get_system_model_catalog] = lambda: catalog

    response = TestClient(app).post(
        "/api/admin/settings/models",
        json={
            "logical_name": "primary",
            "display_name": "Primary",
            "description": "Primary model",
            "status": "active",
            "provider_adapter": "patched_deepseek",
            "provider_model": "deepseek-v4-pro",
            "settings": {
                "base_url": "https://example.invalid/v1",
            },
            "supports_thinking": True,
            "supports_reasoning_effort": False,
            "supports_vision": True,
            "credential_id": str(CREDENTIAL_ID),
            "credential_version_id": str(CREDENTIAL_VERSION_ID),
            "credential_env_key": "DEEPSEEK_API_KEY",
            "sort_order": 10,
        },
    )

    assert response.status_code == 200
    assert catalog.created is not None
    assert catalog.created.credential_id == CREDENTIAL_ID
    assert catalog.created.credential_version_id == CREDENTIAL_VERSION_ID


def test_gateway_routes_include_admin_model_settings_contract() -> None:
    paths = admin_model_settings.router.routes
    operations = {(route.path, method) for route in paths for method in getattr(route, "methods", ())}
    assert {
        ("/api/admin/settings/models", "GET"),
        ("/api/admin/settings/models", "POST"),
        ("/api/admin/settings/models/{model_config_id}", "PUT"),
        (
            "/api/admin/settings/models/{model_config_id}/status",
            "POST",
        ),
        (
            "/api/admin/settings/models/{model_config_id}/default",
            "POST",
        ),
    }.issubset(operations)
