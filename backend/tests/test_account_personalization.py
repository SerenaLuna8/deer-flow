"""Account-scoped personalization API contracts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.gateway.deps import get_current_user_from_request
from app.gateway.routers import account_personalization
from app.personalization.service import (
    AccountMemoryResetResult,
    AccountPersonalizationConflict,
    AccountPersonalizationView,
)


class _PersonalizationService:
    def __init__(self) -> None:
        self.view = AccountPersonalizationView(
            memory_enabled=True,
            effective_memory_enabled=True,
            platform_memory_available=True,
            version=1,
        )
        self.calls: list[tuple[str, uuid.UUID, dict[str, object]]] = []
        self.conflict = False

    async def get(self, user_id: uuid.UUID) -> AccountPersonalizationView:
        self.calls.append(("get", user_id, {}))
        return self.view

    async def update_memory(
        self,
        user_id: uuid.UUID,
        *,
        memory_enabled: bool,
        expected_version: int,
    ) -> AccountPersonalizationView:
        self.calls.append(
            (
                "update_memory",
                user_id,
                {
                    "memory_enabled": memory_enabled,
                    "expected_version": expected_version,
                },
            )
        )
        if self.conflict:
            raise AccountPersonalizationConflict
        return AccountPersonalizationView(
            memory_enabled=memory_enabled,
            effective_memory_enabled=memory_enabled,
            platform_memory_available=True,
            version=expected_version + 1,
        )

    async def reset_memory(
        self,
        user_id: uuid.UUID,
        *,
        expected_version: int,
    ) -> AccountMemoryResetResult:
        self.calls.append(
            (
                "reset_memory",
                user_id,
                {"expected_version": expected_version},
            )
        )
        return AccountMemoryResetResult(
            version=expected_version + 1,
            scopes_reset=2,
            history_entries=3,
            documents=1,
            versions=4,
            dream_runs=5,
            snapshots=6,
            jobs_cancelled=2,
        )


@pytest.fixture()
def app() -> FastAPI:
    application = FastAPI()
    service = _PersonalizationService()
    application.state.account_personalization_service = service
    application.dependency_overrides[get_current_user_from_request] = lambda: SimpleNamespace(id=uuid.UUID("11111111-1111-4111-8111-111111111111"))
    application.include_router(account_personalization.router)
    return application


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: object | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, json=json)


@pytest.mark.asyncio
async def test_account_personalization_routes_are_strict_and_account_scoped(
    app: FastAPI,
) -> None:
    get_response = await _request(app, "GET", "/api/v1/account/personalization")
    assert get_response.status_code == 200
    assert get_response.json() == {
        "memoryEnabled": True,
        "effectiveMemoryEnabled": True,
        "platformMemoryAvailable": True,
        "version": 1,
    }

    patch_response = await _request(
        app,
        "PATCH",
        "/api/v1/account/personalization",
        json={"memoryEnabled": False, "expectedVersion": 1},
    )
    assert patch_response.status_code == 200
    assert patch_response.json()["version"] == 2

    unknown_response = await _request(
        app,
        "PATCH",
        "/api/v1/account/personalization",
        json={
            "memoryEnabled": False,
            "expectedVersion": 1,
            "ownerUserId": str(uuid.uuid4()),
        },
    )
    assert unknown_response.status_code == 422

    reset_without_confirmation = await _request(
        app,
        "POST",
        "/api/v1/account/personalization/memory/reset",
        json={"confirm": False, "expectedVersion": 2},
    )
    assert reset_without_confirmation.status_code == 422

    reset_response = await _request(
        app,
        "POST",
        "/api/v1/account/personalization/memory/reset",
        json={"confirm": True, "expectedVersion": 2},
    )
    assert reset_response.status_code == 200
    assert reset_response.json() == {
        "version": 3,
        "scopesReset": 2,
        "historyEntries": 3,
        "documents": 1,
        "versions": 4,
        "dreamRuns": 5,
        "snapshots": 6,
        "jobsCancelled": 2,
    }

    service = app.state.account_personalization_service
    assert all(call[1] == uuid.UUID("11111111-1111-4111-8111-111111111111") for call in service.calls)


@pytest.mark.asyncio
async def test_account_personalization_conflict_is_stable_409(app: FastAPI) -> None:
    app.state.account_personalization_service.conflict = True

    response = await _request(
        app,
        "PATCH",
        "/api/v1/account/personalization",
        json={"memoryEnabled": False, "expectedVersion": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PERSONALIZATION_CONFLICT"
