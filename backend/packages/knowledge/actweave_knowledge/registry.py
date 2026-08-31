"""Module-level seams for the host retrieval model registry.

The host registry (``app/model_registry``) governs Providers and typed
models; the Knowledge package owns the provider client implementation and the
binding-reference query. These entry points let the registry operate without
a constructed :class:`~actweave_knowledge.module.KnowledgeModule`, so the
admin surface stays available while the Knowledge feature is disabled.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models.client import KnowledgeModelClient
from .persistence.models import KnowledgeBaseRow


def create_knowledge_model_client() -> KnowledgeModelClient:
    """Build one package-owned provider probe client.

    The class stays internal; hosts hold the instance opaquely, share it
    across requests, and must release it with ``aclose()`` exactly like a
    module-owned client. It performs no I/O at construction time.
    """

    return KnowledgeModelClient()


async def retrieval_model_in_use(session: AsyncSession, model_id: UUID) -> bool:
    """Whether any Knowledge Base binds ``model_id`` (either binding column).

    Runs inside the caller's transaction — the registry calls this while
    holding FOR UPDATE on the model row — and includes bases that are pending
    deletion, whose ingest/search paths may still resolve the model until the
    Worker finishes. Feature switches do not change the answer: existing
    database references keep protecting the model while Knowledge is disabled.
    """

    found = await session.scalar(
        select(KnowledgeBaseRow.id)
        .where(
            or_(
                KnowledgeBaseRow.embedding_model_id == model_id,
                KnowledgeBaseRow.reranker_model_id == model_id,
            )
        )
        .limit(1)
    )
    return found is not None


__all__ = ["create_knowledge_model_client", "retrieval_model_in_use"]
