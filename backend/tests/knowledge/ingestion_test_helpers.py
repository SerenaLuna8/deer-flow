"""Production-path Knowledge ingestion and preview integration harness."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from actweave_knowledge import (
    KnowledgeChunkPreviewRequest,
    KnowledgeDocumentUpload,
    KnowledgeModule,
    KnowledgeReparseRequest,
    KnowledgeSettings,
)
from actweave_knowledge.contracts import KnowledgeMinioSettings
from actweave_knowledge.extraction.contracts import ProcessingProfile
from actweave_knowledge.extraction.registry import default_registry
from actweave_knowledge.ingestion import KnowledgeIngestionHandler, KnowledgeReembedHandler
from actweave_knowledge.ingestion.profiles import ProcessingParameters, build_file_capabilities
from actweave_knowledge.persistence.models import KnowledgeSegmentRow, KnowledgeTaskRow
from actweave_knowledge.tasks import KnowledgeTaskWorker
from extraction_test_helpers import ExtractionHarness, extraction_harness
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import select, text

from app.knowledge.authority import ProjectKnowledgeAuthority
from app.knowledge.composition import (
    is_knowledge_project_active,
    is_knowledge_project_pending_deletion,
)
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context


class FakeModelClient:
    """Deterministic external-model port; records inputs and performs no I/O."""

    def __init__(self, dimension: int = 8) -> None:
        self.dimension = dimension
        self.calls: list[list[str]] = []
        self.started = asyncio.Event()
        self.blocker: asyncio.Event | None = None
        self.fail = False

    async def embed(self, material, texts: list[str], *, batch_guard=None, on_batch_verified=None) -> list[list[float]]:  # noqa: ANN001
        del material
        if batch_guard is not None:
            await batch_guard()
        self.started.set()
        if self.blocker is not None:
            await self.blocker.wait()
        if self.fail:
            from actweave_knowledge import KNOWLEDGE_EMBEDDING_FAILED, KnowledgeError

            raise KnowledgeError(KNOWLEDGE_EMBEDDING_FAILED, "Embedding 调用失败")
        self.calls.append(list(texts))
        vectors = [[float((len(value) % 7) + 1)] * self.dimension for value in texts]
        if batch_guard is not None:
            await batch_guard()
        if on_batch_verified is not None:
            await on_batch_verified(len(texts))
        return vectors

    async def aclose(self) -> None:
        return None


def _parameters(profile: ProcessingProfile) -> ProcessingParameters:
    chunk = profile.chunk
    return ProcessingParameters(
        unit=chunk.unit,
        mode=chunk.mode,
        size=chunk.size,
        overlap=chunk.overlap,
        separator=chunk.separator,
        child_size=chunk.child_size,
        child_separator=chunk.child_separator,
        remove_extra_spaces=chunk.remove_extra_spaces,
        remove_urls_emails=chunk.remove_urls_emails,
        header_rules=profile.parse.header_rules,
    )


@dataclass(slots=True)
class IngestionHarness:
    resources: ExtractionHarness
    module: KnowledgeModule
    fake_model: FakeModelClient
    authority: ProjectKnowledgeAuthority

    async def preview(self, path: Path, profile: ProcessingProfile):
        return await self.module.preview_document_chunks(
            KnowledgeChunkPreviewRequest(
                original_name=path.name,
                source_path=path,
                size_bytes=path.stat().st_size,
                processing_profile=_parameters(profile),
            ),
            authority=self.authority,
        )

    async def upload(self, path: Path, profile: ProcessingProfile):
        return await self.module.upload_document(
            self.resources.project_id,
            self.resources.base_id,
            KnowledgeDocumentUpload(
                name=path.name,
                original_name=path.name,
                source_path=path,
                size_bytes=path.stat().st_size,
                processing_profile=_parameters(profile),
            ),
            authority=self.authority,
        )

    async def segments(self, document_id: uuid.UUID) -> list[KnowledgeSegmentRow]:
        async with self.resources.session_factory() as session:
            rows = await session.scalars(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_document_id == document_id).order_by(KnowledgeSegmentRow.position))
            return list(rows.all())

    async def run_next_task(
        self,
        *,
        expected_status: str | None = "succeeded",
    ) -> KnowledgeTaskRow:
        """Claim and execute one task through the production Worker loop."""

        worker = KnowledgeTaskWorker(
            session_factory=self.resources.session_factory,
            handlers={
                "ingest_document": KnowledgeIngestionHandler(
                    session_factory=self.resources.session_factory,
                    settings=self.module.settings,
                    object_store=self.resources.object_store,  # type: ignore[arg-type]
                    quota=self.resources.quota,
                    model_client=self.fake_model,  # type: ignore[arg-type]
                    model_port=registry_model_port(),
                    project_active_check=is_knowledge_project_active,
                ),
                "reembed_document": KnowledgeReembedHandler(
                    session_factory=self.resources.session_factory,
                    model_client=self.fake_model,  # type: ignore[arg-type]
                    model_port=registry_model_port(),
                    project_active_check=is_knowledge_project_active,
                ),
            },
            project_active_check=is_knowledge_project_active,
            concurrency=1,
            task_timeout_seconds=self.module.settings.task_timeout_seconds,
            retry_delay_seconds=0,
        )
        assert await worker._run_once(), "expected one claimable production task"
        async with self.resources.session_factory() as session:
            task = await session.scalar(select(KnowledgeTaskRow).where(KnowledgeTaskRow.kind == "ingest_document").order_by(KnowledgeTaskRow.created_at.desc()).limit(1))
            assert task is not None
            if expected_status is not None:
                assert task.status == expected_status, task.error_message
            return task

    async def reparse(self, document_id: uuid.UUID, profile: ProcessingProfile):
        from actweave_knowledge.persistence.models import KnowledgeDocumentRow

        async with self.resources.session_factory() as session:
            row = await session.get(KnowledgeDocumentRow, document_id)
            assert row is not None
            version = row.version
        return await self.module.reparse_document(
            self.resources.project_id,
            document_id,
            KnowledgeReparseRequest(
                expected_version=version,
                processing_profile=_parameters(profile),
            ),
            authority=self.authority,
        )

    async def reembed(self, base_id: uuid.UUID):
        model_id = await seed_embedding_model(
            self.resources.session_factory,
            await seed_provider(self.resources.session_factory),
        )
        return await self.module.rebuild_knowledge_base(
            self.resources.project_id,
            base_id,
            embedding_model_id=model_id,
            authority=self.authority,
        )


async def _authority(resources: ExtractionHarness) -> ProjectKnowledgeAuthority:
    membership_id = uuid.uuid4()
    async with resources.session_factory() as session, session.begin():
        user_id = await session.scalar(
            text("SELECT created_by_user_id FROM projects WHERE id=:project_id"),
            {"project_id": resources.project_id},
        )
        assert user_id is not None
        await session.execute(
            text(
                """INSERT INTO project_memberships
                (id, project_id, user_id, role, status, version)
                VALUES (:id, :project_id, :user_id, 'editor', 'active', 1)"""
            ),
            {
                "id": membership_id,
                "project_id": resources.project_id,
                "user_id": user_id,
            },
        )
    async with resources.session_factory() as session:
        context = await resolve_project_context(
            session,
            uuid.UUID(str(user_id)),
            resources.project_id,
            "req-p3-t4-preview",
        )
    return ProjectKnowledgeAuthority(context, Capability.SHARED_ASSETS_EDIT)


@asynccontextmanager
async def ingestion_harness(
    postgres_database_url: str,
    *,
    etl_type: str = "dify",
    cache_enabled: bool = True,
) -> AsyncIterator[IngestionHarness]:
    async with extraction_harness(postgres_database_url) as resources:
        settings = KnowledgeSettings(
            enabled=True,
            etl_type=etl_type,
            extraction_cache_enabled=cache_enabled,
            minio=KnowledgeMinioSettings(
                endpoint="minio.invalid:9000",
                bucket="p3-t4-preview",
                access_key="test-access",
                secret_key="test-secret",
            ),
        )
        fake_model = FakeModelClient()
        module = KnowledgeModule(
            settings=settings,
            session_factory=resources.session_factory,
            model_port=registry_model_port(),
            quota=resources.quota,
            project_active_check=is_knowledge_project_active,
            project_cleanup_check=is_knowledge_project_pending_deletion,
            model_client=fake_model,  # type: ignore[arg-type]
        )
        capabilities = build_file_capabilities(settings, default_registry())
        module.install_file_capabilities(capabilities)
        # The module still calls production services; only the external MinIO
        # adapter is replaced by the deterministic P2 byte-store test port.
        module._object_store = resources.object_store  # type: ignore[assignment]
        assert module._document_service is not None
        module._document_service._object_store = resources.object_store  # type: ignore[assignment]
        authority = await _authority(resources)
        try:
            yield IngestionHarness(resources, module, fake_model, authority)
        finally:
            await module.aclose()
