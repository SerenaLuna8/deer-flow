"""Module entry point wiring Knowledge services onto host-provided resources."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
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
    KnowledgeBaseUpdateResult,
    KnowledgeBaseView,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewRequest,
    KnowledgeDocumentAttachmentView,
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
from .extraction.contracts import ExtractionError
from .extraction.runtime import ParserSlots
from .ingestion import (
    KnowledgeIngestionHandler,
    KnowledgeReembedHandler,
    preview_document_chunks,
)
from .ingestion.profiles import FileCapabilities, required_file_formats_ready
from .ingestion.summarize import KnowledgeSummarizeHandler
from .metadata import KnowledgeMetadataService
from .models import KnowledgeModelClient
from .project_retention import ProjectCleanupCheck, create_knowledge_project_purger
from .registry import retrieval_model_in_use
from .retrieval import KnowledgeSearchService
from .retrieval.query_cache import KnowledgeQueryEmbeddingCache
from .segments import KnowledgeSegmentService
from .storage import MinioObjectStore
from .storage.attachment_reads import AttachmentReadMetadata, KnowledgeAttachmentReadService
from .storage.quota import KnowledgeStorageQuotaPort
from .tasks import (
    KnowledgeBaseDeletionHandler,
    KnowledgeDocumentDeletionHandler,
    KnowledgeDocumentObjectDeletionHandler,
    KnowledgeExtractionDeletionHandler,
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
    quota: KnowledgeStorageQuotaPort,
    project_active_check: ProjectActiveCheck,
    project_cleanup_check: ProjectCleanupCheck,
) -> KnowledgeModule:
    """Build a module on host persistence, model-registry, and Project ports."""

    return KnowledgeModule(
        settings=settings,
        session_factory=session_factory,
        model_port=model_port,
        quota=quota,
        project_active_check=project_active_check,
        project_cleanup_check=project_cleanup_check,
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
        quota: KnowledgeStorageQuotaPort,
        project_active_check: ProjectActiveCheck,
        project_cleanup_check: ProjectCleanupCheck,
        model_client: KnowledgeModelClient | None = None,
    ) -> None:
        self._quota = quota
        self._settings = settings
        self._file_capabilities_snapshot: FileCapabilities | None = None
        self._session_factory = session_factory
        self._model_port = model_port
        self._project_active_check = project_active_check
        self._project_cleanup_check = project_cleanup_check
        self._preview_parser_slots = ParserSlots(1)
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
        self._attachment_read_service = (
            KnowledgeAttachmentReadService(
                session_factory=session_factory,
                object_store=self._object_store,
            )
            if self._object_store is not None
            else None
        )
        self._project_purger = create_knowledge_project_purger(
            quota=quota,
            settings=settings,
            session_factory=session_factory,
            project_cleanup_check=project_cleanup_check,
        )
        self._document_service = (
            KnowledgeDocumentService(
                quota=self._quota,
                project_active_check=self._project_active_check,
                session_factory=session_factory,
                settings=settings,
                object_store=self._object_store,
                file_capabilities=self._require_file_capabilities,
                preview_parser_slots=self._preview_parser_slots,
            )
            if self._object_store is not None
            else None
        )
        self._search_service = KnowledgeSearchService(
            session_factory=session_factory,
            client=self._model_client,
            model_port=model_port,
            query_cache=KnowledgeQueryEmbeddingCache(
                enabled=settings.query_cache_enabled,
                max_entries=settings.query_cache_max_entries,
                ttl_seconds=settings.query_cache_ttl_seconds,
            ),
        )
        self._segment_service = KnowledgeSegmentService(
            session_factory=session_factory,
            settings=settings,
            client=self._model_client,
            model_port=model_port,
        )
        self._metadata_service = KnowledgeMetadataService(session_factory=session_factory)

    def install_file_capabilities(self, capabilities: FileCapabilities) -> None:
        if self._file_capabilities_snapshot is not None and self._file_capabilities_snapshot != capabilities:
            raise RuntimeError("Knowledge file capabilities already installed")
        self._file_capabilities_snapshot = capabilities

    def _require_file_capabilities(self) -> FileCapabilities:
        if self._file_capabilities_snapshot is None:
            raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE", "解析能力尚未完成启动检查")
        return self._file_capabilities_snapshot

    async def file_capabilities(self, *, authority: KnowledgeProjectAuthority) -> FileCapabilities:
        async with self._session_factory() as session, session.begin():
            await revalidate_project_authority(authority, session, project_id=authority.project_id)
            return self._require_file_capabilities()

    def _require_parsing_ready(self) -> None:
        if not required_file_formats_ready(self._require_file_capabilities()):
            raise ExtractionError("PARSER_SANDBOX_UNAVAILABLE", "文件解析暂不可用，请联系管理员")

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

        Delegates to the module-level :func:`retrieval_model_in_use` so the
        registry sees one binding-reference query whether or not a module is
        constructed.
        """

        return await retrieval_model_in_use(session, model_id)

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
    ) -> KnowledgeBaseUpdateResult:
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
        self._require_parsing_ready()
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

    async def list_document_attachments(
        self,
        project_id: UUID,
        document_id: UUID,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> tuple[list[KnowledgeDocumentAttachmentView], int]:
        return await self._documents().list_document_attachments(
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
        self._require_parsing_ready()
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
        self._require_parsing_ready()
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

    async def download_segment_attachment(
        self,
        project_id: UUID,
        document_id: UUID,
        segment_id: UUID,
        attachment_id: UUID,
        target_path: Path,
        *,
        expected_document_version: int,
        expected_content_digest: str,
        authority: KnowledgeProjectAuthority,
    ) -> AttachmentReadMetadata:
        return await self._attachment_reads().download_managed(
            project_id,
            document_id,
            segment_id,
            attachment_id,
            target_path,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            authority=authority,
        )

    async def download_citation_attachment(
        self,
        project_id: UUID,
        base_id: UUID,
        document_id: UUID,
        segment_id: UUID,
        attachment_id: UUID,
        target_path: Path,
        *,
        expected_document_version: int,
        expected_content_digest: str,
        authority: KnowledgeProjectAuthority,
    ) -> AttachmentReadMetadata:
        return await self._attachment_reads().download_citation(
            project_id,
            base_id,
            document_id,
            segment_id,
            attachment_id,
            target_path,
            expected_document_version=expected_document_version,
            expected_content_digest=expected_content_digest,
            authority=authority,
        )

    async def preview_document_chunks(
        self,
        request: KnowledgeChunkPreviewRequest,
        *,
        authority: KnowledgeProjectAuthority,
    ) -> KnowledgeChunkPreview:
        """Stateless extract → clean → split preview; no rows, objects, or tasks."""

        async def guard() -> None:
            async with self._session_factory() as session, session.begin():
                await revalidate_project_authority(
                    authority,
                    session,
                    project_id=authority.project_id,
                )

        await guard()
        capabilities = self._require_file_capabilities()
        self._require_parsing_ready()
        preview = await preview_document_chunks(
            request,
            self._settings,
            capability_revision=capabilities.capability_revision,
            parser_slots=self._preview_parser_slots,
            guard=guard,
        )
        # Parsing runs outside PostgreSQL and can be expensive for PDF/DOCX
        # inputs. Revalidate in a fresh short transaction after it settles so a
        # membership or capability revoked during parser work cannot receive
        # the already-computed preview.
        await guard()
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
                quota=self._quota,
                model_client=self._model_client,
                model_port=self._model_port,
                project_active_check=project_active_check,
            ),
            # Re-embedding reads current rows only: no object store on purpose,
            # so it structurally cannot re-parse the original file.
            "reembed_document": KnowledgeReembedHandler(
                session_factory=self._session_factory,
                model_client=self._model_client,
                model_port=self._model_port,
                project_active_check=project_active_check,
            ),
            "summarize_document": KnowledgeSummarizeHandler(
                session_factory=self._session_factory,
                model_client=self._model_client,
                model_port=self._model_port,
                project_active_check=project_active_check,
            ),
            "delete_document": KnowledgeDocumentDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
                quota=self._quota,
                project_active_check=project_active_check,
            ),
            "delete_document_object": KnowledgeDocumentObjectDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
                quota=self._quota,
                project_active_check=project_active_check,
            ),
            "delete_knowledge_base": KnowledgeBaseDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
                quota=self._quota,
                project_active_check=project_active_check,
            ),
            "delete_extraction": KnowledgeExtractionDeletionHandler(
                session_factory=self._session_factory,
                object_store=object_store,
                quota=self._quota,
                project_active_check=project_active_check,
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
        if self._file_capabilities_snapshot is not None and not required_file_formats_ready(self._file_capabilities_snapshot):
            problems.append("文件解析暂不可用")
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

    def _attachment_reads(self) -> KnowledgeAttachmentReadService:
        if self._attachment_read_service is None:
            raise KnowledgeError(KNOWLEDGE_DISABLED, "Knowledge 功能未启用")
        return self._attachment_read_service
