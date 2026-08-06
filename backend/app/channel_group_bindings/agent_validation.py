from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.channel_group_bindings.errors import (
    GroupBindingAgentUnavailable,
    GroupBindingNotFound,
    GroupBindingUnavailable,
)
from app.projects.context import ProjectContext
from app.shared_assets.errors import (
    AssetResolutionUnavailable,
    AssetStorageUnavailable,
    SharedAssetError,
)
from app.shared_assets.models import (
    AssetKind,
    AssetScope,
    AssetSelection,
    ResolvedAgentSnapshot,
)
from app.shared_assets.resolver import ProjectAssetResolver


class GroupBindingAgentValidator(Protocol):
    async def validate(
        self,
        session: AsyncSession,
        context: ProjectContext,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
    ) -> None: ...


class ProjectGroupBindingAgentValidator:
    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._resolver = ProjectAssetResolver(session_factory)

    async def validate(
        self,
        session: AsyncSession,
        context: ProjectContext,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
    ) -> None:
        try:
            resolved = await self._resolver.resolve_project_asset_snapshot_in_session(
                session,
                context,
                AssetSelection(AssetKind.AGENT, agent_asset_id),
            )
        except AssetStorageUnavailable:
            raise GroupBindingUnavailable(context.request_id) from None
        except AssetResolutionUnavailable:
            raise GroupBindingAgentUnavailable(context.request_id) from None
        except SharedAssetError:
            raise GroupBindingNotFound(context.request_id) from None
        expected_scope = AssetScope.PROJECT if agent_scope == "project" else AssetScope.SYSTEM
        if not isinstance(resolved, ResolvedAgentSnapshot) or resolved.scope is not expected_scope:
            raise GroupBindingAgentUnavailable(context.request_id)


__all__ = ["GroupBindingAgentValidator", "ProjectGroupBindingAgentValidator"]
