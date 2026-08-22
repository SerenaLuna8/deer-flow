from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.shared_assets.mcp_secret_store import (
    McpSecretMaterial,
    McpSecretStore,
    mcp_secret_closure_digest,
)
from deerflow.persistence.shared_assets import McpSecretSlotRow


@dataclass(frozen=True, slots=True)
class McpSecretClosure:
    slots: tuple[McpSecretSlotRow, ...]
    materials: tuple[McpSecretMaterial, ...]
    digest: str


async def lock_mcp_secret_closure(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    mcp_server_id: uuid.UUID,
    mcp_server_version_id: uuid.UUID,
    slots: tuple[McpSecretSlotRow, ...],
    request_id: str,
) -> McpSecretClosure:
    materials = await McpSecretStore(session).load_materials(
        project_id=project_id,
        mcp_server_id=mcp_server_id,
        mcp_server_version_id=mcp_server_version_id,
        slots=slots,
        require_required=True,
        for_update=True,
        request_id=request_id,
    )
    return McpSecretClosure(
        slots=slots,
        materials=materials,
        digest=mcp_secret_closure_digest(materials),
    )


__all__ = ["McpSecretClosure", "lock_mcp_secret_closure"]
