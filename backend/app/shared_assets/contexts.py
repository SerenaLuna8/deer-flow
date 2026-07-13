from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

from app.shared_assets.errors import AssetForbidden


class _AuthenticatedUser(Protocol):
    id: uuid.UUID
    system_role: str


@dataclass(frozen=True)
class SystemAssetGovernanceContext:
    user_id: uuid.UUID
    request_id: str
    project_id: uuid.UUID | None = None


def resolve_asset_actor(
    user: _AuthenticatedUser,
    *,
    request_id: str,
    project_id: uuid.UUID | None = None,
) -> SystemAssetGovernanceContext:
    user_id = user.id
    if user.system_role != "system_admin" or not isinstance(user_id, uuid.UUID):
        raise AssetForbidden(request_id)
    return SystemAssetGovernanceContext(user_id=user_id, request_id=request_id, project_id=project_id)
