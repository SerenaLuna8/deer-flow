from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.routers import project_assets, project_skill_frontmatter
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.shared_assets.errors import SkillSecretDeclarationInvalid
from deerflow.skills.types import SecretRequirement

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_REQUEST_ID = "skill-frontmatter-route"


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("33333333-3333-4333-8333-333333333333"),
        role="editor",
        capabilities=frozenset(
            {
                Capability.SHARED_ASSETS_READ,
                Capability.SHARED_ASSETS_EDIT,
            }
        ),
        membership_version=1,
        request_id=_REQUEST_ID,
    )


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class _RecordingService:
    calls: list[tuple[object, ...]]
    fail_patch: bool = False

    async def parse(self, actor, content, *, expected_source_sha256):
        self.calls.append(("parse", actor, content, expected_source_sha256))
        return SimpleNamespace(
            source_sha256=expected_source_sha256,
            valid=True,
            patchable=True,
            projection=SimpleNamespace(
                required_secrets=(SecretRequirement("API_KEY", optional=False),),
                secrets_autonomous=False,
                secrets_autonomous_explicit=True,
                shorthand_count=0,
            ),
            diagnostics=(),
        )

    async def patch(
        self,
        actor,
        content,
        *,
        expected_source_sha256,
        required_secrets,
        secrets_autonomous,
    ):
        self.calls.append(
            (
                "patch",
                actor,
                content,
                expected_source_sha256,
                required_secrets,
                secrets_autonomous,
            )
        )
        if self.fail_patch:
            raise SkillSecretDeclarationInvalid(
                actor.request_id,
                (
                    SimpleNamespace(
                        code="managed_comments_require_source_edit",
                        severity="warning",
                        field_path=("required-secrets",),
                        line=4,
                        column=1,
                        public_message=("Managed comments require source editing"),
                    ),
                ),
            )
        next_content = content.replace("---\n", "---\nsecrets-autonomous: false\n", 1)
        return SimpleNamespace(
            source_sha256=expected_source_sha256,
            result_sha256=_sha(next_content),
            content=next_content,
            changed=True,
            changed_fields=("required-secrets", "secrets-autonomous"),
            projection=SimpleNamespace(
                required_secrets=tuple(required_secrets),
                secrets_autonomous=secrets_autonomous,
                secrets_autonomous_explicit=True,
                shorthand_count=0,
            ),
            diagnostics=(),
        )


def _app(service: _RecordingService) -> FastAPI:
    application = FastAPI()
    application.include_router(project_skill_frontmatter.router)
    application.dependency_overrides[project_assets.project_asset_context] = _context
    application.dependency_overrides[project_skill_frontmatter.get_skill_frontmatter_service] = lambda: service
    return application


@pytest.mark.asyncio
async def test_parse_route_returns_strict_projection_and_no_store_headers() -> None:
    service = _RecordingService([])
    content = "---\nname: route\ndescription: Route\n---\n"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/frontmatter/parse",
            json={"content": content, "source_sha256": _sha(content)},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {
        "source_sha256": _sha(content),
        "valid": True,
        "patchable": True,
        "projection": {
            "required_secrets": [{"name": "API_KEY", "optional": False}],
            "secrets_autonomous": False,
            "secrets_autonomous_explicit": True,
            "shorthand_count": 0,
        },
        "diagnostics": [],
        "request_id": _REQUEST_ID,
    }
    assert service.calls == [("parse", _context(), content, _sha(content))]


@pytest.mark.asyncio
async def test_patch_route_forwards_only_secret_names_and_returns_new_content() -> None:
    service = _RecordingService([])
    content = "---\nname: route\ndescription: Route\n---\n"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/frontmatter/patch",
            json={
                "content": content,
                "source_sha256": _sha(content),
                "required_secrets": [{"name": "API_KEY", "optional": False}],
                "secrets_autonomous": False,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_sha256"] == _sha(content)
    assert payload["result_sha256"] == _sha(payload["content"])
    assert payload["projection"]["required_secrets"] == [{"name": "API_KEY", "optional": False}]
    assert "secret_value" not in response.text


@pytest.mark.asyncio
async def test_patch_rejection_returns_safe_structured_diagnostics() -> None:
    service = _RecordingService([], fail_patch=True)
    content = "---\nname: route\ndescription: Route\n---\n"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/frontmatter/patch",
            json={
                "content": content,
                "source_sha256": _sha(content),
                "required_secrets": [],
                "secrets_autonomous": True,
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "SKILL_SECRET_DECLARATION_INVALID",
        "message": "Skill secret declaration is invalid",
        "request_id": _REQUEST_ID,
        "diagnostics": [
            {
                "code": "managed_comments_require_source_edit",
                "severity": "warning",
                "field_path": ["required-secrets"],
                "line": 4,
                "column": 1,
                "public_message": "Managed comments require source editing",
            }
        ],
    }
    assert "required-secrets:" not in response.text


@pytest.mark.asyncio
async def test_route_contract_rejects_unknown_or_secret_value_fields() -> None:
    service = _RecordingService([])
    content = "---\nname: route\ndescription: Route\n---\n"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(service)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            f"/api/projects/{_PROJECT_ID}/skills/frontmatter/patch",
            json={
                "content": content,
                "source_sha256": _sha(content),
                "required_secrets": [
                    {
                        "name": "API_KEY",
                        "optional": False,
                        "value": "must-not-be-accepted",
                    }
                ],
                "secrets_autonomous": True,
            },
        )

    assert response.status_code == 422
    assert service.calls == []
    assert "must-not-be-accepted" not in response.text
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
