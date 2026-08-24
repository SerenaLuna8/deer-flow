from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import admin_assets
from app.shared_assets import AssetConflict, SkillVersionView, VersionRelation
from app.shared_assets.contexts import SystemAssetGovernanceContext
from deerflow.trace_context import request_trace_context

_ASSET_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_VERSION_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_ADMIN_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_REQUEST_ID = "system-skill-revocation-route"
_REVOKED_AT = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


def _revoked_version() -> SkillVersionView:
    return SkillVersionView(
        id=_VERSION_ID,
        skill_id=_ASSET_ID,
        version_number=1,
        relation=VersionRelation.CURRENT,
        description="Revoked packaged Skill",
        frontmatter={"name": "release-review"},
        compatibility=None,
        secret_requirements=(),
        file_views=(),
        supersedes_version_id=None,
        payload_checksum="a" * 64,
        revoked_at=_REVOKED_AT,
        revoked_by_user_id=str(_ADMIN_ID),
        revocation_reason_code="security",
        governance_status="revoked",
        binding_eligible=False,
        created_by_user_id=str(_ADMIN_ID),
        created_at=_REVOKED_AT,
    )


class _SkillService:
    def __init__(self, *, error: type[AssetConflict] | None = None) -> None:
        self.error = error
        self.calls: list[tuple[SystemAssetGovernanceContext, uuid.UUID, uuid.UUID, int, str]] = []

    async def revoke_version(
        self,
        actor: SystemAssetGovernanceContext,
        asset_id: uuid.UUID,
        version_id: uuid.UUID,
        *,
        expected_asset_version: int,
        reason_code: str,
    ) -> SkillVersionView:
        self.calls.append(
            (
                actor,
                asset_id,
                version_id,
                expected_asset_version,
                reason_code,
            )
        )
        if self.error is not None:
            raise self.error(actor.request_id)
        return _revoked_version()


def _app(
    service: _SkillService,
    *,
    system_role: str = "system_admin",
) -> FastAPI:
    application = FastAPI()
    user = SimpleNamespace(id=_ADMIN_ID, system_role=system_role)
    application.dependency_overrides[admin_assets.get_current_user_from_request] = lambda: user
    application.dependency_overrides[admin_assets.get_skill_service] = lambda: service
    application.include_router(admin_assets.admin_router)
    return application


async def _revoke(
    application: FastAPI,
    *,
    asset_id: str = str(_ASSET_ID),
    version_id: str = str(_VERSION_ID),
    body: object,
) -> httpx.Response:
    with request_trace_context(_REQUEST_ID):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            return await client.post(
                f"/api/admin/assets/skills/{asset_id}/versions/{version_id}/revoke",
                json=body,
            )


@pytest.mark.asyncio
async def test_system_admin_can_revoke_a_system_skill_version() -> None:
    service = _SkillService()

    response = await _revoke(
        _app(service),
        body={"expected_revision": 7, "reason_code": "security"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == _REQUEST_ID
    assert (
        payload["data"]
        | {
            "revoked_at": _REVOKED_AT.isoformat().replace("+00:00", "Z"),
            "revoked_by_user_id": str(_ADMIN_ID),
            "revocation_reason_code": "security",
            "governance_status": "revoked",
            "binding_eligible": False,
        }
        == payload["data"]
    )
    assert service.calls == [
        (
            SystemAssetGovernanceContext(
                user_id=_ADMIN_ID,
                request_id=_REQUEST_ID,
            ),
            _ASSET_ID,
            _VERSION_ID,
            7,
            "security",
        )
    ]


@pytest.mark.asyncio
async def test_non_admin_cannot_revoke_a_system_skill_version() -> None:
    service = _SkillService()

    response = await _revoke(
        _app(service, system_role="user"),
        body={"expected_revision": 7, "reason_code": "security"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "System administrator privileges required."}
    assert service.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"expected_revision": 7, "reason_code": "operator_request"},
        {
            "expected_revision": 7,
            "reason_code": "security",
            "operator_note": "must not enter the audit contract",
        },
        {"expected_revision": True, "reason_code": "security"},
        {"expected_revision": False, "reason_code": "security"},
    ],
)
async def test_revocation_request_rejects_non_contract_values(body: object) -> None:
    service = _SkillService()

    response = await _revoke(_app(service), body=body)

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "asset_validation_failed",
            "message": "Asset validation failed",
            "request_id": _REQUEST_ID,
        }
    }
    assert service.calls == []


@pytest.mark.asyncio
async def test_revocation_conflict_keeps_the_asset_error_contract() -> None:
    service = _SkillService(error=AssetConflict)

    response = await _revoke(
        _app(service),
        body={"expected_revision": 7, "reason_code": "integrity"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "asset_conflict",
            "message": "Asset state conflict",
            "request_id": _REQUEST_ID,
        }
    }
    assert len(service.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("asset_id", "version_id"),
    [
        ("not-an-asset-uuid", str(_VERSION_ID)),
        (str(_ASSET_ID), "not-a-version-uuid"),
    ],
)
async def test_revocation_rejects_malformed_path_ids_before_service_call(
    asset_id: str,
    version_id: str,
) -> None:
    service = _SkillService()

    response = await _revoke(
        _app(service),
        asset_id=asset_id,
        version_id=version_id,
        body={"expected_revision": 7, "reason_code": "policy"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "asset_validation_failed",
            "message": "Asset validation failed",
            "request_id": _REQUEST_ID,
        }
    }
    assert service.calls == []
