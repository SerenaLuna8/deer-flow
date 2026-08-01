from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.routers import private_work as private_work_router

_POSTGRES_BIGINT_MAX = (1 << 63) - 1


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(private_work_router.router)
    app.dependency_overrides[require_project_private_open] = lambda: None
    app.dependency_overrides[private_work_context] = lambda: SimpleNamespace(
        request_id="api-bounds",
    )
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "json"),
    (
        (
            "POST",
            "/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/threads/search",
            {"limit": 20, "offset": _POSTGRES_BIGINT_MAX + 1},
        ),
        (
            "PATCH",
            (f"/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/threads/{uuid.uuid4()}"),
            {
                "expected_version": _POSTGRES_BIGINT_MAX + 1,
                "display_name": "too large",
            },
        ),
        (
            "DELETE",
            (f"/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/threads/{uuid.uuid4()}?expected_version={_POSTGRES_BIGINT_MAX + 1}"),
            None,
        ),
        (
            "GET",
            (f"/api/projects/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/private-work/threads/{uuid.uuid4()}/runs?offset={_POSTGRES_BIGINT_MAX + 1}"),
            None,
        ),
    ),
)
async def test_private_work_bigint_backed_inputs_reject_overflow_with_stable_422(
    method: str,
    path: str,
    json: dict[str, object] | None,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
    ) as client:
        response = await client.request(method, path, json=json)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "PRIVATE_WORK_INVALID"


def test_private_work_openapi_exposes_postgres_bigint_maximums() -> None:
    schema = _app().openapi()
    search = schema["components"]["schemas"]["PrivateThreadSearchRequest"]
    patch = schema["components"]["schemas"]["PrivateThreadPatchRequest"]
    bounded_schemas = [
        search["properties"]["offset"],
        patch["properties"]["expected_version"],
    ]

    paths = schema["paths"]
    delete_parameters = paths["/api/projects/{project_id}/private-work/threads/{thread_id}"]["delete"]["parameters"]
    run_parameters = paths["/api/projects/{project_id}/private-work/threads/{thread_id}/runs"]["get"]["parameters"]
    delete_version = next(item for item in delete_parameters if item["name"] == "expected_version")
    run_offset = next(item for item in run_parameters if item["name"] == "offset")
    bounded_schemas.extend([delete_version["schema"], run_offset["schema"]])

    for bounded in bounded_schemas:
        assert bounded["format"] == "int64"
        assert bounded["x-postgres-bigint-maximum"] == str(_POSTGRES_BIGINT_MAX)
