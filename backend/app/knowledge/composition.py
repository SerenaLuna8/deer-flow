"""Composition root assembling the Knowledge Package onto host resources.

The Gateway composes the optional feature module. The Worker composes that
module plus an independent Project-retention capability, because disabling
the product surface must not make historical rows or objects undeletable.
Both entry points require an initialized persistence engine.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from actweave_knowledge import (
    KnowledgeModule,
    create_knowledge_module,
    create_knowledge_project_purger,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.config import load_knowledge_settings
from app.knowledge.model_port import RegistryKnowledgeModelPort
from deerflow.persistence.projects.model import ProjectRow

KnowledgeProjectPurge = Callable[[UUID], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class KnowledgeWorkerResources:
    """Independent runtime and retention capabilities for one Worker."""

    feature_module: KnowledgeModule | None
    project_purge: KnowledgeProjectPurge


async def is_knowledge_project_active(
    session: AsyncSession,
    project_id: UUID,
) -> bool:
    """Check Project execution eligibility under the claim transaction."""

    status = await session.scalar(select(ProjectRow.status).where(ProjectRow.id == project_id).with_for_update(read=True, of=ProjectRow))
    return status == "active"


def create_knowledge_module_from_app_config(app_config: Any) -> KnowledgeModule | None:
    """Build the module when enabled; return ``None`` when the feature is off."""

    settings = load_knowledge_settings(app_config)
    if not settings.enabled:
        return None

    from deerflow.persistence.engine import get_session_factory

    return create_knowledge_module(
        settings=settings,
        session_factory=get_session_factory(),
        model_port=RegistryKnowledgeModelPort.from_environment(),
        project_active_check=is_knowledge_project_active,
    )


def create_knowledge_worker_resources_from_app_config(app_config: Any) -> KnowledgeWorkerResources:
    """Build Worker capabilities without equating feature disable with cleanup."""

    settings = load_knowledge_settings(app_config)
    feature_module = create_knowledge_module_from_app_config(app_config)
    from deerflow.persistence.engine import get_session_factory

    project_purger = create_knowledge_project_purger(
        settings=settings,
        session_factory=get_session_factory(),
    )

    return KnowledgeWorkerResources(
        feature_module=feature_module,
        project_purge=project_purger.purge_project,
    )


class KnowledgeStorageNotReady(RuntimeError):
    """Startup failure: the enabled module cannot reach its MinIO bucket."""


async def require_knowledge_storage_ready(module: KnowledgeModule) -> None:
    """Fail fast at process startup when object storage is unreachable.

    An enabled Knowledge module without a reachable bucket is an operator
    misconfiguration (the bucket is provisioned by the administrator, never
    auto-created), so Gateway and Worker refuse to start instead of failing
    lazily on the first upload. The health message is locator-free.
    """

    health = await module.health()
    if not health.storage_ok:
        raise KnowledgeStorageNotReady(f"Knowledge 对象存储启动检查失败：{health.message}")
