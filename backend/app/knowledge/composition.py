"""Composition root assembling the Knowledge Package onto host resources.

The Gateway composes the optional feature module. The Worker composes that
module plus an independent Project-retention capability, because disabling
the product surface must not make historical rows or objects undeletable.
Both entry points require an initialized persistence engine.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from actweave_knowledge import (
    KnowledgeError,
    KnowledgeModule,
    KnowledgeSettings,
    create_knowledge_module,
    create_knowledge_project_purger,
)
from actweave_knowledge.ingestion.profiles import probe_file_capabilities, required_file_formats_ready
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.config import load_knowledge_settings_from_db
from app.knowledge.model_port import RegistryKnowledgeModelPort
from app.knowledge.quota_port import HostKnowledgeStorageQuotaPort
from app.knowledge.summary_runtime import DatabaseKnowledgeSummaryRuntime
from app.knowledge_settings.service import KnowledgeSettingsError, probe_knowledge_storage, read_active_summary_model
from app.quotas.service import QuotaService
from deerflow.persistence.projects.model import ProjectRow
from deerflow.secrets import SecretKey, SecretKeyInvalid

KnowledgeProjectPurge = Callable[[UUID], Awaitable[bool]]
KnowledgeStartupState = Literal["ready", "disabled", "storage_failed", "parser_failed"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class KnowledgeWorkerResources:
    """Independent runtime and retention capabilities for one Worker."""

    feature_module: KnowledgeModule | None
    project_purge: KnowledgeProjectPurge
    startup_state: KnowledgeStartupState


async def is_knowledge_project_active(
    session: AsyncSession,
    project_id: UUID,
) -> bool:
    """Check Project execution eligibility under the claim transaction."""

    status = await session.scalar(select(ProjectRow.status).where(ProjectRow.id == project_id).with_for_update(read=True, of=ProjectRow))
    return status == "active"


async def is_knowledge_project_pending_deletion(
    session: AsyncSession,
    project_id: UUID,
) -> bool:
    """Fence retention-owned cleanup to the exact pending-deletion Project."""

    status = await session.scalar(select(ProjectRow.status).where(ProjectRow.id == project_id).with_for_update(read=True, of=ProjectRow))
    return status == "pending_deletion"


async def _startup_settings() -> KnowledgeSettings | None:
    from deerflow.persistence.engine import get_session_factory

    try:
        return await load_knowledge_settings_from_db(get_session_factory(), secret_key=SecretKey.from_environment())
    except (KnowledgeSettingsError, SecretKeyInvalid):
        logger.warning("knowledge startup configuration unavailable")
        return None


async def _compose_feature(settings: KnowledgeSettings | None, app_config: Any, quota: HostKnowledgeStorageQuotaPort) -> tuple[KnowledgeModule | None, KnowledgeStartupState]:
    if settings is None:
        return None, "storage_failed"
    if not settings.enabled:
        return None, "disabled"
    from deerflow.persistence.engine import get_session_factory

    session_factory = get_session_factory()
    try:
        module = create_knowledge_module(
            settings=settings,
            quota=quota,
            session_factory=session_factory,
            model_port=RegistryKnowledgeModelPort.from_environment(
                model_runtime=DatabaseKnowledgeSummaryRuntime(app_config=app_config, session_factory=session_factory),
                summary_model_reader=read_active_summary_model,
            ),
            project_active_check=is_knowledge_project_active,
            project_cleanup_check=is_knowledge_project_pending_deletion,
        )
    except (ValueError, SecretKeyInvalid):
        logger.warning("knowledge storage configuration invalid; feature disabled for this process")
        return None, "storage_failed"
    try:
        await require_knowledge_storage_ready(module)
    except KnowledgeStorageNotReady:
        logger.warning("knowledge storage unavailable; feature disabled for this process")
        await module.aclose()
        return None, "storage_failed"
    capabilities = await probe_file_capabilities(settings)
    module.install_file_capabilities(capabilities)
    if not required_file_formats_ready(capabilities):
        logger.warning("knowledge parser unavailable; parsing disabled for this process")
        return module, "parser_failed"
    return module, "ready"


async def create_knowledge_module_from_database(*, app_config: Any, quota_service: QuotaService) -> tuple[KnowledgeModule | None, KnowledgeStartupState]:
    """Storage failure disables Knowledge, without stopping the rest of Gateway."""

    return await _compose_feature(await _startup_settings(), app_config, HostKnowledgeStorageQuotaPort(quota_service))


async def create_knowledge_worker_resources_from_database(*, app_config: Any, quota_service: QuotaService) -> KnowledgeWorkerResources:
    """Build Worker capabilities without equating feature disable with cleanup."""

    quota = HostKnowledgeStorageQuotaPort(quota_service)
    settings = await _startup_settings()
    feature_module, startup_state = await _compose_feature(settings, app_config, quota)
    from deerflow.persistence.engine import get_session_factory

    try:
        project_purger = create_knowledge_project_purger(
            # Unreadable storage credentials cannot authorize object deletion.
            settings=settings or KnowledgeSettings(),
            quota=quota,
            session_factory=get_session_factory(),
            project_cleanup_check=is_knowledge_project_pending_deletion,
        )
    except ValueError:
        # A disabled draft or migrated endpoint may fail the SDK's stricter
        # validation. Keep the Worker and metadata-only retention available;
        # the absent-storage purger refuses historical document/object cleanup.
        logger.warning("knowledge retention storage configuration invalid")
        if feature_module is not None:
            await feature_module.aclose()
        feature_module, startup_state = None, "storage_failed"
        project_purger = create_knowledge_project_purger(
            settings=KnowledgeSettings(),
            session_factory=get_session_factory(),
            quota=quota,
            project_cleanup_check=is_knowledge_project_pending_deletion,
        )

    return KnowledgeWorkerResources(
        feature_module=feature_module,
        project_purge=project_purger.purge_project,
        startup_state=startup_state,
    )


class KnowledgeStorageNotReady(RuntimeError):
    """Locator-free storage failure, also used by fail-closed retention."""


async def require_knowledge_storage_ready(module: KnowledgeModule) -> None:
    """Require the administrator-provisioned, reachable, unversioned bucket."""

    storage = module.settings.minio
    if storage is None:
        raise KnowledgeStorageNotReady("Knowledge 对象存储启动检查失败")
    try:
        await probe_knowledge_storage(storage)
    except (KnowledgeError, TimeoutError, ValueError):
        raise KnowledgeStorageNotReady("Knowledge 对象存储启动检查失败") from None
