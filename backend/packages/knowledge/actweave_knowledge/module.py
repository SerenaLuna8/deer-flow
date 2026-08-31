"""Module entry point wiring Knowledge services onto host-provided resources."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .authority import KnowledgeProjectAuthority, revalidate_project_authority
from .bases import KnowledgeBaseService
from .contracts import (
    KNOWLEDGE_DISABLED,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeBaseCreate,
    KnowledgeBaseFilterFields,
    KnowledgeBaseUpdate,
    KnowledgeBaseView,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewRequest,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeHealth,
    KnowledgeMetadataBatchPatch,
    KnowledgeMetadataFieldType,
    KnowledgeMetadataFieldView,
    KnowledgeModelPort,
    KnowledgeQueryView,
    KnowledgeRebuildResult,
    KnowledgeReparsePreview,
    KnowledgeReparseRequest,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeSegmentCreate,
    KnowledgeSegmentDetail,
    KnowledgeSegmentUpdate,
    KnowledgeSegmentView,
    KnowledgeSettings,
)
from .documents import KnowledgeDocumentService
from .ingestion import (
    KnowledgeIngestionHandler,
    KnowledgeReembedHandler,
    preview_document_chunks,
)
from .metadata import KnowledgeMetadataService
from .models import KnowledgeModelClient
from .persistence.models import KnowledgeBaseRow
from .project_retention import create_knowledge_project_purger
from .retrieval import KnowledgeSearchService
from .segments import KnowledgeSegmentService
from .storage import MinioObjectStore
from .tasks import (
    KnowledgeBaseDeletionHandler,
    KnowledgeDocumentDeletionHandler,
    KnowledgeDocumentObjectDeletionHandler,
    KnowledgeTaskWorker,
    ProjectActiveCheck,
    TaskHandler,
)

logger = logging.getLogger(__name__)


def create_knowledge_module(
    *,
    settings: KnowledgeSettings,
    session_factory: async_sessionmaker[AsyncSession],
    model_port: KnowledgeModelPort,
    project_active_check: ProjectActiveCheck,
) -> KnowledgeModule:
    """Build a module on host persistence, model-registry, and Project ports."""

    return KnowledgeModule(
        settings=settings,
        session_factory=session_factory,
        model_port=model_port,
        project_active_check=project_active_check,
    )


class KnowledgeModule:
    """Facade exposing every Knowledge operation to host adapters.

    This is the host's single entry point: base/document lifecycle,
    upload/download, segment preview, retrieval, the task worker loop, project
    purge, in-use checks for the host model registry, and health. Methods
    delegate to the owning internal service; hosts never construct or call
    those services directly. Model governance itself (providers, typed models,
    keys) lives in the host registry behind ``model_port``.
    """

    def __init__(
        self,
        *,
        settings: KnowledgeSettings,
        session_factory: async_sessionmaker[AsyncSession],
        model_port: KnowledgeModelPort,
        project_active_check: ProjectActiveCheck | None = None,
        model_client: KnowledgeModelClient | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._model_port = model_port
        self._project_active_check = project_active_check
        # Injectable for integration tests (e.g. an httpx.MockTransport-backed
        # client); production hosts leave it None and the module owns one.
        self._model_client = model_client or KnowledgeModelClient()
        self._base_service = KnowledgeBaseService(
            session_factory=session_factory,
            settings=settings,
            model_port=model_port,
        )
        # Constructing the store performs no I/O; it only exists when MinIO is
        # configured (always true for an enabled module).
        self._object_store = MinioObjectStore(settings.minio) if settings.minio is not None else None
        self._project_purger = create_knowledge_project_purger(
            settings=settings,
            session_factory=session_factory,
        )
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
            model_port=model_port,
        )
        self._segment_service = KnowledgeSegmentService(
            session_factory=session_factory,
            settings=settings,
            client=self._model_client,
            model_port=model_port,
        )
        self._metadata_service = KnowledgeMetadataService(session_factory=session_factory)

    @property
    def settings(self) -> KnowledgeSettings:
        return self._settings

    @property
    def model_client(self) -> KnowledgeModelClient:
        """The provider client, shared with the host registry's probe flows."""

        return self._model_client

    # -- model registry support -------------------------------------------------

    async def model_in_use(self, session: AsyncSession, model_id: UUID) -> bool:
        """Whether any Knowledge Base binds ``model_id`` (either column).

        Runs inside the caller's transaction — the registry calls this while
        holding FOR UPDATE on the model row — and includes bases that are
        pending deletion, whose ingest/search paths may still resolve the
        model until the Worker finishes.
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

    # -- knowledge bases -------------------------------------------------------

    async def create_knowledge_base(
        self,
        project_id: UUID,
        create: KnowledgeBaseCreate,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeBaseView:
        return await self._base_service.create_knowledge_base(
            project_id,
            create,
            authority=authority,
        )

    async def list_knowledge_bases(
        self,
        project_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority,
    ) -> tuple[list[KnowledgeBaseView], int]:
        return await self._base_service.list_knowledge_bases(
            project_id,
            page=page,
            page_size=page_size,
            authority=authority,
        )

    async def get_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeBaseView:
        return await self._base_service.get_knowledge_base(
            project_id,
            base_id,
            authority=authority,
        )

    async def update_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        update: KnowledgeBaseUpdate,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeBaseView:
        return await self._base_service.update_knowledge_base(
            project_id,
            base_id,
            update,
            authority=authority,
        )

    async def delete_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeBaseView:
        return await self._base_service.delete_knowledge_base(
            project_id,
            base_id,
            authority=authority,
        )

    async def rebuild_knowledge_base(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        embedding_model_id: UUID,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeRebuildResult:
        return await self._base_service.rebuild_knowledge_base(
            project_id,
            base_id,
            embedding_model_id=embedding_model_id,
            authority=authority,
        )

    # -- metadata fields ---------------------------------------------------------

    async def list_metadata_fields(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> list[KnowledgeMetadataFieldView]:
        return await self._metadata_service.list_metadata_fields(
            project_id,
            base_id,
            authority=authority,
        )

    async def list_filter_fields(
        self,
        project_id: UUID,
        base_ids: list[UUID] | tuple[UUID, ...] | None = None,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> list[KnowledgeBaseFilterFields]:
        return await self._metadata_service.list_filter_fields(
            project_id,
            base_ids,
            authority=authority,
        )

    async def create_metadata_field(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        name: str,
        field_type: KnowledgeMetadataFieldType,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeMetadataFieldView:
        return await self._metadata_service.create_metadata_field(
            project_id,
            base_id,
            name=name,
            field_type=field_type,
            authority=authority,
        )

    async def rename_metadata_field(
        self,
        project_id: UUID,
        field_id: UUID,
        *,
        name: str,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeMetadataFieldView:
        return await self._metadata_service.rename_metadata_field(
            project_id,
            field_id,
            name=name,
            authority=authority,
        )

    async def delete_metadata_field(
        self,
        project_id: UUID,
        field_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> None:
        await self._metadata_service.delete_metadata_field(
            project_id,
            field_id,
            authority=authority,
        )

    async def set_document_metadata(
        self,
        project_id: UUID,
        document_id: UUID,
        values: dict[str, Any],
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._metadata_service.set_document_metadata(
            project_id,
            document_id,
            values,
            authority=authority,
        )

    async def set_documents_metadata(
        self,
        project_id: UUID,
        base_id: UUID,
        patch: KnowledgeMetadataBatchPatch,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> list[KnowledgeDocumentView]:
        return await self._metadata_service.set_documents_metadata(
            project_id,
            base_id,
            patch,
            authority=authority,
        )

    # -- documents ---------------------------------------------------------------

    async def upload_document(
        self,
        project_id: UUID,
        base_id: UUID,
        upload: KnowledgeDocumentUpload,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().upload_document(
            project_id,
            base_id,
            upload,
            authority=authority,
        )

    async def list_documents(
        self,
        project_id: UUID,
        base_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority,
    ) -> tuple[list[KnowledgeDocumentView], int]:
        return await self._documents().list_documents(
            project_id,
            base_id,
            page=page,
            page_size=page_size,
            authority=authority,
        )

    async def get_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().get_document(
            project_id,
            document_id,
            authority=authority,
        )

    async def list_document_segments(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority,
    ) -> tuple[list[KnowledgeSegmentView], int]:
        return await self._documents().list_document_segments(
            project_id,
            document_id,
            page=page,
            page_size=page_size,
            authority=authority,
        )

    async def retry_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().retry_document(
            project_id,
            document_id,
            authority=authority,
        )

    async def preview_document_reparse(
        self,
        project_id: UUID,
        document_id: UUID,
        request: KnowledgeReparseRequest,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeReparsePreview:
        return await self._documents().preview_reparse(
            project_id,
            document_id,
            request,
            authority=authority,
        )

    async def reparse_document(
        self,
        project_id: UUID,
        document_id: UUID,
        request: KnowledgeReparseRequest,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().reparse_document(
            project_id,
            document_id,
            request,
            authority=authority,
        )

    async def rename_document(
        self,
        project_id: UUID,
        document_id: UUID,
        name: str,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().rename_document(
            project_id,
            document_id,
            name,
            authority=authority,
        )

    async def set_documents_enabled(
        self,
        project_id: UUID,
        document_ids: list[UUID],
        enabled: bool,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> list[KnowledgeDocumentView]:
        return await self._documents().set_documents_enabled(
            project_id,
            document_ids,
            enabled,
            authority=authority,
        )

    async def delete_document(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().delete_document(
            project_id,
            document_id,
            authority=authority,
        )

    async def delete_documents(
        self,
        project_id: UUID,
        document_ids: list[UUID],
        *,
        authority: KnowledgeProjectAuthority,
    ) -> list[KnowledgeDocumentView]:
        return await self._documents().delete_documents(
            project_id,
            document_ids,
            authority=authority,
        )

    async def download_document(
        self,
        project_id: UUID,
        document_id: UUID,
        target_path: Path,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._documents().download_document(
            project_id,
            document_id,
            target_path,
            authority=authority,
        )

    async def preview_document_chunks(
        self,
        request: KnowledgeChunkPreviewRequest,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeChunkPreview:
        """Stateless extract → clean → split preview; no rows, objects, or tasks."""

        async with self._session_factory() as session, session.begin():
            await revalidate_project_authority(
                authority,
                session,
                project_id=authority.project_id,
            )
        preview = await preview_document_chunks(request, self._settings)
        # Parsing runs outside PostgreSQL and can be expensive for PDF/DOCX
        # inputs. Revalidate in a fresh short transaction after it settles so a
        # membership or capability revoked during parser work cannot receive
        # the already-computed preview.
        async with self._session_factory() as session, session.begin():
            await revalidate_project_authority(
                authority,
                session,
                project_id=authority.project_id,
            )
        return preview

    # -- segments ----------------------------------------------------------------

    async def create_segment(
        self,
        project_id: UUID,
        document_id: UUID,
        create: KnowledgeSegmentCreate,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeSegmentView:
        return await self._segments().create_segment(
            project_id,
            document_id,
            create,
            authority=authority,
        )

    async def update_segment(
        self,
        project_id: UUID,
        segment_id: UUID,
        update: KnowledgeSegmentUpdate,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeSegmentView:
        return await self._segments().update_segment(
            project_id,
            segment_id,
            update,
            authority=authority,
        )

    async def delete_segment(
        self,
        project_id: UUID,
        segment_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeDocumentView:
        return await self._segments().delete_segment(
            project_id,
            segment_id,
            authority=authority,
        )

    async def get_segment_detail(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        segment_id: UUID,
        *,
        expected_document_version: int | None = None,
        expected_content_digest: str | None = None,
        child_page: int = 1,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeSegmentDetail:
        return await self._segments().get_segment_detail(
            project_id,
            base_id,
            document_id,
            segment_id,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            child_page=child_page,
            authority=authority,
        )

    # -- search ----------------------------------------------------------------

    async def search(
        self,
        request: KnowledgeSearchRequest,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeSearchResult:
        return await self._search_service.search(
            request,
            authority=authority,
        )

    async def list_recent_queries(
        self,
        project_id: UUID,
        owner_user_id: UUID,
        base_id: UUID,
        *,
        page: int = 1,
        page_size: int = 20,
        authority: KnowledgeProjectAuthority,
    ) -> tuple[list[KnowledgeQueryView], int]:
        return await self._search_service.list_recent_queries(
            project_id,
            owner_user_id,
            base_id,
            page=page,
            page_size=page_size,
            authority=authority,
        )

    # -- lifecycle ---------------------------------------------------------------

    async def purge_project(self, project_id: UUID) -> bool:
        """Delete every Knowledge resource of the project; True when complete.

        Idempotent: a project without Knowledge resources is already complete.
        Storage trouble returns False so the caller's retry (the retention
        job) tries again instead of purging the project with objects left.
        """

        return await self._project_purger.purge_project(project_id)

    async def run_worker(self, stop_event: asyncio.Event) -> None:
        """Run the Knowledge task worker until ``stop_event`` is set."""

        object_store = self._object_store
        if object_store is None:
            raise KnowledgeError(KNOWLEDGE_DISABLED, "Knowledge 功能未启用")
        project_active_check = self._project_active_check
        if project_active_check is None:
            raise RuntimeError("Knowledge task worker requires a host Project-active check")
        handlers: dict[str, TaskHandler] = {
            "ingest_document": KnowledgeIngestionHandler(
                session_factory=self._session_factory,
                settings=self._settings,
                object_store=object_store,
                model_client=self._model_client,
                model_port=self._model_port,
            ),
            # Re-embedding reads current rows only: no object store on purpose,
            # so it structurally cannot re-parse the original file.
            "reembed_document": KnowledgeReembedHandler(
                session_factory=self._session_factory,
                model_client=self._model_client,
                model_port=self._model_port,
            ),
            "delete_document": KnowledgeDocumentDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
            ),
            "delete_document_object": KnowledgeDocumentObjectDeletionHandler(
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
            project_active_check=project_active_check,
            concurrency=self._settings.worker_concurrency,
            task_timeout_seconds=self._settings.task_timeout_seconds,
        )
        await worker.run(stop_event)

    async def health(
        self,
        *,
        authority: KnowledgeProjectAuthority | None = None,
    ) -> KnowledgeHealth:
        """Probe the database and the configured MinIO bucket with real credentials."""

        problems: list[str] = []
        database_ok = False
        try:
            async with self._session_factory() as session, session.begin():
                if authority is not None:
                    await revalidate_project_authority(
                        authority,
                        session,
                        project_id=authority.project_id,
                    )
                await session.execute(text("SELECT 1"))
            database_ok = True
        except KnowledgeError:
            raise
        except SQLAlchemyError:
            if authority is not None:
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "Knowledge 存储暂时不可用",
                ) from None
            problems.append("数据库不可用")
        except Exception:
            problems.append("数据库不可用")
        storage_ok = False
        if self._object_store is None:
            problems.append("对象存储未配置")
        else:
            storage_ok = await self._object_store.check_bucket()
            if not storage_ok:
                problems.append("对象存储 bucket 不可访问")
        if authority is not None:
            try:
                async with self._session_factory() as session, session.begin():
                    await revalidate_project_authority(
                        authority,
                        session,
                        project_id=authority.project_id,
                    )
            except KnowledgeError:
                raise
            except SQLAlchemyError:
                raise KnowledgeError(
                    KNOWLEDGE_STORAGE_UNAVAILABLE,
                    "Knowledge 存储暂时不可用",
                ) from None
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
