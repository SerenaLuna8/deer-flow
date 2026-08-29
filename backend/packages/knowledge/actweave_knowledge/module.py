"""Module entry point wiring Knowledge services onto host-provided resources."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .bases import KnowledgeBaseService
from .contracts import (
    KNOWLEDGE_DISABLED,
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseView,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewRequest,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeHealth,
    KnowledgeMetadataFieldType,
    KnowledgeMetadataFieldView,
    KnowledgeModelConfigurationCreate,
    KnowledgeModelConfigurationUpdate,
    KnowledgeModelConfigurationView,
    KnowledgeModelConnectionResult,
    KnowledgeModelOption,
    KnowledgeQueryView,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeSecretPort,
    KnowledgeSegmentCreate,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from .documents import KnowledgeDocumentService
from .ingestion import KnowledgeIngestionHandler, preview_document_chunks
from .metadata import KnowledgeMetadataService
from .models import KnowledgeModelClient, KnowledgeModelConfigurationService
from .persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeQueryRow,
    KnowledgeTaskRow,
)
from .retrieval import KnowledgeSearchService
from .segments import KnowledgeSegmentService
from .storage import MinioObjectStore
from .tasks import (
    KnowledgeBaseDeletionHandler,
    KnowledgeDocumentDeletionHandler,
    KnowledgeTaskWorker,
    TaskHandler,
    purge_project_knowledge,
)

logger = logging.getLogger(__name__)


def create_knowledge_module(
    *,
    settings: KnowledgeSettings,
    session_factory: async_sessionmaker[AsyncSession],
    secret_port: KnowledgeSecretPort,
) -> KnowledgeModule:
    """Build a :class:`KnowledgeModule` on the host session factory and secret port."""

    return KnowledgeModule(
        settings=settings,
        session_factory=session_factory,
        secret_port=secret_port,
    )


class KnowledgeModule:
    """Facade exposing every Knowledge operation to host adapters.

    This is the host's single entry point: model configuration and connection
    tests, base/document lifecycle, upload/download, segment preview,
    two-stage retrieval, the task worker loop, project purge, and health.
    Methods delegate to the owning internal service; hosts never construct or
    call those services directly.
    """

    def __init__(
        self,
        *,
        settings: KnowledgeSettings,
        session_factory: async_sessionmaker[AsyncSession],
        secret_port: KnowledgeSecretPort,
        model_client: KnowledgeModelClient | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._secret_port = secret_port
        # Injectable for integration tests (e.g. an httpx.MockTransport-backed
        # client); production hosts leave it None and the module owns one.
        self._model_client = model_client or KnowledgeModelClient()
        self._model_service = KnowledgeModelConfigurationService(
            session_factory=session_factory,
            secret_port=secret_port,
            client=self._model_client,
        )
        self._base_service = KnowledgeBaseService(session_factory=session_factory, settings=settings)
        # Constructing the store performs no I/O; it only exists when MinIO is
        # configured (always true for an enabled module).
        self._object_store = MinioObjectStore(settings.minio) if settings.minio is not None else None
        self._document_service = (
            KnowledgeDocumentService(
                session_factory=session_factory,
                settings=settings,
                object_store=self._object_store,
            )
            if self._object_store is not None
            else None
        )
        self._search_service = KnowledgeSearchService(
            session_factory=session_factory,
            client=self._model_client,
            secret_port=secret_port,
        )
        self._segment_service = KnowledgeSegmentService(
            session_factory=session_factory,
            settings=settings,
            client=self._model_client,
            secret_port=secret_port,
        )
        self._metadata_service = KnowledgeMetadataService(session_factory=session_factory)

    @property
    def settings(self) -> KnowledgeSettings:
        return self._settings

    # -- model configurations -------------------------------------------------

    async def create_model_configuration(self, create: KnowledgeModelConfigurationCreate) -> KnowledgeModelConfigurationView:
        return await self._model_service.create_model_configuration(create)

    async def list_model_configurations(self, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeModelConfigurationView], int]:
        return await self._model_service.list_model_configurations(page=page, page_size=page_size)

    async def update_model_configuration(self, configuration_id: UUID, update: KnowledgeModelConfigurationUpdate) -> KnowledgeModelConfigurationView:
        return await self._model_service.update_model_configuration(configuration_id, update)

    async def delete_model_configuration(self, configuration_id: UUID) -> None:
        await self._model_service.delete_model_configuration(configuration_id)

    async def test_model_configuration(self, configuration_id: UUID) -> KnowledgeModelConnectionResult:
        return await self._model_service.test_model_configuration(configuration_id)

    async def list_active_model_options(self) -> list[KnowledgeModelOption]:
        return await self._model_service.list_active_model_options()

    # -- knowledge bases -------------------------------------------------------

    async def create_knowledge_base(self, project_id: UUID, create: KnowledgeBaseCreate) -> KnowledgeBaseView:
        return await self._base_service.create_knowledge_base(project_id, create)

    async def list_knowledge_bases(self, project_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeBaseView], int]:
        return await self._base_service.list_knowledge_bases(project_id, page=page, page_size=page_size)

    async def get_knowledge_base(self, project_id: UUID, base_id: UUID) -> KnowledgeBaseView:
        return await self._base_service.get_knowledge_base(project_id, base_id)

    async def update_knowledge_base(self, project_id: UUID, base_id: UUID, update: KnowledgeBaseUpdate) -> KnowledgeBaseView:
        return await self._base_service.update_knowledge_base(project_id, base_id, update)

    async def delete_knowledge_base(self, project_id: UUID, base_id: UUID) -> KnowledgeBaseView:
        return await self._base_service.delete_knowledge_base(project_id, base_id)

    async def rebuild_knowledge_base(self, project_id: UUID, base_id: UUID, *, model_configuration_id: UUID) -> KnowledgeBaseView:
        return await self._base_service.rebuild_knowledge_base(project_id, base_id, model_configuration_id=model_configuration_id)

    # -- metadata fields ---------------------------------------------------------

    async def list_metadata_fields(self, project_id: UUID, base_id: UUID) -> list[KnowledgeMetadataFieldView]:
        return await self._metadata_service.list_metadata_fields(project_id, base_id)

    async def create_metadata_field(self, project_id: UUID, base_id: UUID, *, name: str, field_type: KnowledgeMetadataFieldType) -> KnowledgeMetadataFieldView:
        return await self._metadata_service.create_metadata_field(project_id, base_id, name=name, field_type=field_type)

    async def rename_metadata_field(self, project_id: UUID, field_id: UUID, *, name: str) -> KnowledgeMetadataFieldView:
        return await self._metadata_service.rename_metadata_field(project_id, field_id, name=name)

    async def delete_metadata_field(self, project_id: UUID, field_id: UUID) -> None:
        await self._metadata_service.delete_metadata_field(project_id, field_id)

    async def set_document_metadata(self, project_id: UUID, document_id: UUID, values: dict[str, Any]) -> KnowledgeDocumentView:
        return await self._metadata_service.set_document_metadata(project_id, document_id, values)

    # -- documents ---------------------------------------------------------------

    async def upload_document(self, project_id: UUID, base_id: UUID, upload: KnowledgeDocumentUpload) -> KnowledgeDocumentView:
        return await self._documents().upload_document(project_id, base_id, upload)

    async def list_documents(self, project_id: UUID, base_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeDocumentView], int]:
        return await self._documents().list_documents(project_id, base_id, page=page, page_size=page_size)

    async def get_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        return await self._documents().get_document(project_id, document_id)

    async def list_document_segments(self, project_id: UUID, document_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeSegmentView], int]:
        return await self._documents().list_document_segments(project_id, document_id, page=page, page_size=page_size)

    async def retry_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        return await self._documents().retry_document(project_id, document_id)

    async def rename_document(self, project_id: UUID, document_id: UUID, name: str) -> KnowledgeDocumentView:
        return await self._documents().rename_document(project_id, document_id, name)

    async def set_documents_enabled(self, project_id: UUID, document_ids: list[UUID], enabled: bool) -> list[KnowledgeDocumentView]:
        return await self._documents().set_documents_enabled(project_id, document_ids, enabled)

    async def delete_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        return await self._documents().delete_document(project_id, document_id)

    async def delete_documents(self, project_id: UUID, document_ids: list[UUID]) -> list[KnowledgeDocumentView]:
        return await self._documents().delete_documents(project_id, document_ids)

    async def download_document(self, project_id: UUID, document_id: UUID, target_path: Path) -> KnowledgeDocumentView:
        return await self._documents().download_document(project_id, document_id, target_path)

    async def preview_document_chunks(self, request: KnowledgeChunkPreviewRequest) -> KnowledgeChunkPreview:
        """Stateless extract → clean → split preview; no rows, objects, or tasks."""

        return await preview_document_chunks(request, self._settings)

    # -- segments ----------------------------------------------------------------

    async def create_segment(self, project_id: UUID, document_id: UUID, create: KnowledgeSegmentCreate) -> KnowledgeSegmentView:
        return await self._segments().create_segment(project_id, document_id, create)

    async def update_segment(self, project_id: UUID, segment_id: UUID, update: KnowledgeSegmentUpdate) -> KnowledgeSegmentView:
        return await self._segments().update_segment(project_id, segment_id, update)

    async def delete_segment(self, project_id: UUID, segment_id: UUID) -> KnowledgeDocumentView:
        return await self._segments().delete_segment(project_id, segment_id)

    # -- search ----------------------------------------------------------------

    async def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResult:
        return await self._search_service.search(request)

    async def list_recent_queries(self, project_id: UUID, base_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeQueryView], int]:
        return await self._search_service.list_recent_queries(project_id, base_id, page=page, page_size=page_size)

    # -- lifecycle ---------------------------------------------------------------

    async def purge_project(self, project_id: UUID) -> bool:
        """Delete every Knowledge resource of the project; True when complete.

        Idempotent: a project without Knowledge resources is already complete.
        Storage trouble returns False so the caller's retry (the retention
        job) tries again instead of purging the project with objects left.
        """

        try:
            if self._object_store is not None:
                await purge_project_knowledge(self._session_factory, self._object_store, project_id=project_id)
                return True
            # Without an object store, document objects cannot be removed;
            # only a project with no document rows can be purged completely.
            async with self._session_factory() as session, session.begin():
                remaining = await session.scalar(select(func.count()).select_from(KnowledgeDocumentRow).where(KnowledgeDocumentRow.project_id == project_id))
                if int(remaining or 0) > 0:
                    return False
                await session.execute(delete(KnowledgeBaseRow).where(KnowledgeBaseRow.project_id == project_id))
                await session.execute(delete(KnowledgeTaskRow).where(KnowledgeTaskRow.project_id == project_id))
                await session.execute(delete(KnowledgeQueryRow).where(KnowledgeQueryRow.project_id == project_id))
                return True
        except (KnowledgeError, SQLAlchemyError):
            logger.warning("knowledge purge for project %s did not complete", project_id)
            return False

    async def run_worker(self, stop_event: asyncio.Event) -> None:
        """Run the Knowledge task worker until ``stop_event`` is set."""

        object_store = self._object_store
        if object_store is None:
            raise KnowledgeError(KNOWLEDGE_DISABLED, "Knowledge 功能未启用")
        handlers: dict[str, TaskHandler] = {
            "ingest_document": KnowledgeIngestionHandler(
                session_factory=self._session_factory,
                settings=self._settings,
                object_store=object_store,
                model_client=self._model_client,
                secret_port=self._secret_port,
            ),
            "delete_document": KnowledgeDocumentDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
            ),
            "delete_knowledge_base": KnowledgeBaseDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
            ),
        }
        worker = KnowledgeTaskWorker(
            session_factory=self._session_factory,
            handlers=handlers,
            concurrency=self._settings.worker_concurrency,
            task_timeout_seconds=self._settings.task_timeout_seconds,
        )
        await worker.run(stop_event)

    async def health(self) -> KnowledgeHealth:
        """Probe the database and the configured MinIO bucket with real credentials."""

        problems: list[str] = []
        database_ok = False
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            problems.append("数据库不可用")
        storage_ok = False
        if self._object_store is None:
            problems.append("对象存储未配置")
        else:
            storage_ok = await self._object_store.check_bucket()
            if not storage_ok:
                problems.append("对象存储 bucket 不可访问")
        return KnowledgeHealth(
            enabled=self._settings.enabled,
            database_ok=database_ok,
            storage_ok=storage_ok,
            message="；".join(problems),
        )

    async def aclose(self) -> None:
        """Release module-owned clients; safe to call more than once."""
        await self._model_client.aclose()

    def _documents(self) -> KnowledgeDocumentService:
        if self._document_service is None:
            raise KnowledgeError(KNOWLEDGE_DISABLED, "Knowledge 功能未启用")
        return self._document_service

    def _segments(self) -> KnowledgeSegmentService:
        # Segment governance ships with the document feature: without the
        # object store the whole document surface is off.
        if self._document_service is None:
            raise KnowledgeError(KNOWLEDGE_DISABLED, "Knowledge 功能未启用")
        return self._segment_service
