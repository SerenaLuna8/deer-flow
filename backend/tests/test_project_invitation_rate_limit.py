from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.gateway.deps import project_session
from app.gateway.routers import project_invitations
from app.projects.invitation_models import ProjectInvitationInvalid

pytestmark = [pytest.mark.asyncio, pytest.mark.postgres]


async def test_concurrent_invalid_claims_only_validate_first_five_attempts(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(project_invitations.router)

    async def request_session():
        async with factory() as session:
            yield session

    app.dependency_overrides[project_session] = request_session
    claim = AsyncMock(side_effect=ProjectInvitationInvalid())
    monkeypatch.setattr(project_invitations.InvitationService, "claim", claim)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("192.0.2.20", 1234)),
            base_url="http://test",
        ) as client:
            responses = await asyncio.gather(*(client.post("/api/project-invitations/claim", json={"token": "invalid-token"}) for _ in range(12)))

        assert all(response.status_code == 200 for response in responses)
        assert {response.json()["message"] for response in responses} == {"Invitation claim processed"}
        assert claim.await_count == 5
    finally:
        await engine.dispose()
