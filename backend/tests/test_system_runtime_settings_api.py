from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.audit.models import resolve_system_audit_context
from app.gateway.deps import get_system_runtime_policy_service
from app.gateway.routers import admin_operations, admin_system_settings
from app.system_runtime_settings.errors import (
    SystemRuntimePolicyConflict,
    SystemRuntimePolicyInvalid,
)
from app.system_runtime_settings.models import (
    RuntimePolicyCatalogView,
    RuntimePolicySection,
    RuntimePolicyUpdateResult,
    RuntimePolicyView,
    default_policy_value,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService

USER_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _admin_context():
    return resolve_system_audit_context(
        SimpleNamespace(id=USER_ID, system_role="system_admin"),
        request_id="request-runtime-settings",
    )


def _view(section: RuntimePolicySection, revision: int = 1) -> RuntimePolicyView:
    scopes = {
        RuntimePolicySection.AGENT_RUNTIME: "new_requests_and_runs",
        RuntimePolicySection.AUTH: "new_requests",
        RuntimePolicySection.QUOTAS: "next_authoritative_check",
    }
    return RuntimePolicyView(
        section=section,
        revision=revision,
        schema_version=1,
        value=default_policy_value(section),
        effect_scope=scopes[section],
        effective_revision=revision,
        updated_at=NOW,
    )


class _Catalog:
    async def list_policies(self, _context):
        return RuntimePolicyCatalogView.create(
            1,
            {section: _view(section) for section in RuntimePolicySection},
        )

    async def update_policy(
        self,
        _context,
        section,
        *,
        expected_revision,
        value,
    ):
        assert section is RuntimePolicySection.AUTH
        assert expected_revision == 1
        assert value == {"allow_registration": False}
        return RuntimePolicyUpdateResult(
            catalog_revision=2,
            policy=RuntimePolicyView(
                section=RuntimePolicySection.AUTH,
                revision=2,
                schema_version=1,
                value=default_policy_value(RuntimePolicySection.AUTH).model_copy(
                    update={"allow_registration": False},
                ),
                effect_scope="new_requests",
                effective_revision=2,
                updated_at=NOW,
            ),
            effective_at=NOW,
        )


def _app(service: object) -> FastAPI:
    app = FastAPI()
    app.include_router(admin_system_settings.router)
    app.dependency_overrides[admin_system_settings.current_model_admin_context] = _admin_context
    app.dependency_overrides[get_system_runtime_policy_service] = lambda: service
    return app


def test_admin_system_settings_get_contract_is_exact_and_secret_free() -> None:
    response = TestClient(_app(_Catalog())).get("/api/admin/settings/system")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"catalog_revision", "sections"}
    assert body["catalog_revision"] == 1
    assert set(body["sections"]) == {"agent_runtime", "auth", "quotas"}
    assert body["sections"]["agent_runtime"]["effect_scope"] == ("new_requests_and_runs")
    assert body["sections"]["auth"] == {
        "section": "auth",
        "revision": 1,
        "schema_version": 1,
        "value": {"allow_registration": True},
        "effect_scope": "new_requests",
        "effective_revision": 1,
        "updated_at": "2026-07-31T00:00:00Z",
    }
    encoded = response.text.lower()
    for forbidden in ("password", "api_key", "storage_subdir", "summary_prompt"):
        assert forbidden not in encoded


def test_admin_system_settings_put_contract_is_exact() -> None:
    response = TestClient(_app(_Catalog())).put(
        "/api/admin/settings/system/auth",
        json={
            "expected_revision": 1,
            "value": {"allow_registration": False},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "catalog_revision": 2,
        "section": "auth",
        "stored_revision": 2,
        "effective_revision": 2,
        "effect_scope": "new_requests",
        "effective_at": "2026-07-31T00:00:00Z",
        "pending_roles": [],
        "policy": {
            "revision": 2,
            "schema_version": 1,
            "value": {"allow_registration": False},
        },
    }


def test_admin_system_settings_rejects_extra_body_and_hides_non_admin(
    monkeypatch,
) -> None:
    app = _app(_Catalog())

    async def authenticated_validation_admin(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        admin_operations,
        "_require_validation_system_admin",
        authenticated_validation_admin,
    )
    malformed = TestClient(app).put(
        "/api/admin/settings/system/auth",
        json={
            "expected_revision": 1,
            "value": {"allow_registration": False},
            "unknown": True,
        },
    )
    assert malformed.status_code == 422

    async def hidden():
        raise HTTPException(status_code=404, detail="Not found")

    app.dependency_overrides[admin_system_settings.current_model_admin_context] = hidden
    assert TestClient(app).get("/api/admin/settings/system").status_code == 404


def test_admin_system_settings_maps_revision_conflict_to_stable_409() -> None:
    class ConflictCatalog(_Catalog):
        async def update_policy(self, *_args, **_kwargs):
            raise SystemRuntimePolicyConflict("request-runtime-settings")

    response = TestClient(_app(ConflictCatalog())).put(
        "/api/admin/settings/system/auth",
        json={
            "expected_revision": 1,
            "value": {"allow_registration": False},
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "system_runtime_policy_conflict",
            "message": "System runtime policy state conflict",
            "request_id": "request-runtime-settings",
        }
    }


@pytest.mark.asyncio
async def test_service_rejects_malformed_model_ref_before_opening_transaction() -> None:
    opened = False

    def sessions():
        nonlocal opened
        opened = True
        raise AssertionError("validation must precede storage")

    service = SystemRuntimePolicyService(sessions, SimpleNamespace())
    value = default_policy_value(RuntimePolicySection.AGENT_RUNTIME).model_dump(
        mode="python",
    )
    value["title"]["model_name"] = "INVALID MODEL REF"

    with pytest.raises(SystemRuntimePolicyInvalid) as error:
        await service.update_policy(
            _admin_context(),
            RuntimePolicySection.AGENT_RUNTIME,
            expected_revision=1,
            value=value,
        )
    assert error.value.status_code == 422
    assert opened is False
