"""Host-owned project authority seam for Knowledge operations.

The package never imports the host's membership or capability models.  A host
adapter carries trusted request identity and revalidates it inside the exact
Knowledge transaction that reads or mutates Project data.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .contracts import KNOWLEDGE_NOT_FOUND, KnowledgeError


class KnowledgeProjectAuthority(Protocol):
    """Trusted actor bound to one Project and a host-selected capability."""

    project_id: UUID
    actor_user_id: UUID

    async def revalidate(self, session: AsyncSession) -> None:
        """Lock and revalidate live authority in ``session``."""


async def revalidate_project_authority(
    authority: KnowledgeProjectAuthority | None,
    session: AsyncSession,
    *,
    project_id: UUID,
) -> None:
    """Apply a host authority guard without weakening package-only callers.

    Internal package tests and maintenance jobs may call services directly
    without a host authority. The public :class:`KnowledgeModule` requires an
    authority for host-facing Project reads and mutations and passes it through
    here.
    """

    if authority is None:
        return
    if authority.project_id != project_id:
        raise KnowledgeError(KNOWLEDGE_NOT_FOUND, "资源不存在")
    await authority.revalidate(session)
