from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.shared_assets.errors import AssetNotFound
from app.shared_assets.mcp_secret_service import McpSecretService
from app.shared_assets.skill_secret_service import SkillSecretService


@pytest.mark.asyncio
async def test_system_skill_secrets_require_enabled_project_binding() -> None:
    project_id = uuid.uuid4()
    actor = SimpleNamespace(project_id=project_id, request_id="request-skill")
    asset = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    with pytest.raises(AssetNotFound):
        await SkillSecretService._require_system_binding(  # noqa: SLF001
            session,
            actor,
            asset,
            read=False,
        )

    session.scalar.return_value = project_id
    await SkillSecretService._require_system_binding(  # noqa: SLF001
        session,
        actor,
        asset,
        read=True,
    )


@pytest.mark.asyncio
async def test_system_mcp_secrets_require_exact_enabled_version_binding() -> None:
    project_id = uuid.uuid4()
    actor = SimpleNamespace(project_id=project_id, request_id="request-mcp")
    asset = SimpleNamespace(id=uuid.uuid4())
    version = SimpleNamespace(id=uuid.uuid4())
    session = SimpleNamespace(scalar=AsyncMock(return_value=None))

    with pytest.raises(AssetNotFound):
        await McpSecretService._require_system_binding(  # noqa: SLF001
            session,
            actor,
            asset,
            version,
            read=False,
        )

    session.scalar.return_value = project_id
    await McpSecretService._require_system_binding(  # noqa: SLF001
        session,
        actor,
        asset,
        version,
        read=True,
    )
