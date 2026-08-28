"""Module entry point wiring Knowledge services onto host-provided resources."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .contracts import (
    KnowledgeBaseCreate,
    KnowledgeBaseUpdate,
    KnowledgeBaseView,
    KnowledgeDocumentUpload,
    KnowledgeDocumentView,
    KnowledgeHealth,
    KnowledgeModelConfigurationCreate,
    KnowledgeModelConfigurationUpdate,
    KnowledgeModelConfigurationView,
    KnowledgeModelConnectionResult,
    KnowledgeModelOption,
    KnowledgeSearchRequest,
    KnowledgeSearchResult,
    KnowledgeSecretPort,
    KnowledgeSegmentView,
    KnowledgeSettings,
)


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

    Business behavior lands milestone by milestone (M1 persistence, M2 models,
    M3 storage, M4 ingestion, M5 retrieval); methods raise
    :class:`NotImplementedError` until their owning milestone is implemented.
    """

    def __init__(
        self,
        *,
        settings: KnowledgeSettings,
        session_factory: async_sessionmaker[AsyncSession],
        secret_port: KnowledgeSecretPort,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._secret_port = secret_port

    @property
    def settings(self) -> KnowledgeSettings:
        return self._settings

    # -- model configurations -------------------------------------------------

    async def create_model_configuration(self, create: KnowledgeModelConfigurationCreate) -> KnowledgeModelConfigurationView:
        raise NotImplementedError("M2")

    async def list_model_configurations(self, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeModelConfigurationView], int]:
        raise NotImplementedError("M2")

    async def update_model_configuration(self, configuration_id: UUID, update: KnowledgeModelConfigurationUpdate) -> KnowledgeModelConfigurationView:
        raise NotImplementedError("M2")

    async def delete_model_configuration(self, configuration_id: UUID) -> None:
        raise NotImplementedError("M2")

    async def test_model_configuration(self, configuration_id: UUID) -> KnowledgeModelConnectionResult:
        raise NotImplementedError("M2")

    async def list_active_model_options(self) -> list[KnowledgeModelOption]:
        raise NotImplementedError("M2")

    # -- knowledge bases -------------------------------------------------------

    async def create_knowledge_base(self, project_id: UUID, create: KnowledgeBaseCreate) -> KnowledgeBaseView:
        raise NotImplementedError("M3")

    async def list_knowledge_bases(self, project_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeBaseView], int]:
        raise NotImplementedError("M3")

    async def get_knowledge_base(self, project_id: UUID, base_id: UUID) -> KnowledgeBaseView:
        raise NotImplementedError("M3")

    async def update_knowledge_base(self, project_id: UUID, base_id: UUID, update: KnowledgeBaseUpdate) -> KnowledgeBaseView:
        raise NotImplementedError("M3")

    async def delete_knowledge_base(self, project_id: UUID, base_id: UUID) -> KnowledgeBaseView:
        raise NotImplementedError("M4")

    # -- documents ---------------------------------------------------------------

    async def upload_document(self, project_id: UUID, base_id: UUID, upload: KnowledgeDocumentUpload) -> KnowledgeDocumentView:
        raise NotImplementedError("M3")

    async def list_documents(self, project_id: UUID, base_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeDocumentView], int]:
        raise NotImplementedError("M3")

    async def get_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        raise NotImplementedError("M3")

    async def list_document_segments(self, project_id: UUID, document_id: UUID, *, page: int = 1, page_size: int = 20) -> tuple[list[KnowledgeSegmentView], int]:
        raise NotImplementedError("M4")

    async def retry_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        raise NotImplementedError("M4")

    async def delete_document(self, project_id: UUID, document_id: UUID) -> KnowledgeDocumentView:
        raise NotImplementedError("M4")

    async def download_document(self, project_id: UUID, document_id: UUID, target_path: Path) -> KnowledgeDocumentView:
        raise NotImplementedError("M3")

    # -- search ----------------------------------------------------------------

    async def search(self, request: KnowledgeSearchRequest) -> KnowledgeSearchResult:
        raise NotImplementedError("M5")

    # -- lifecycle ---------------------------------------------------------------

    async def purge_project(self, project_id: UUID) -> bool:
        """Delete every Knowledge resource of the project; True when complete."""
        raise NotImplementedError("M4")

    async def run_worker(self, stop_event: asyncio.Event) -> None:
        raise NotImplementedError("M4")

    async def health(self) -> KnowledgeHealth:
        raise NotImplementedError("M3")

    async def aclose(self) -> None:
        """Release module-owned clients; safe to call more than once."""
        return None
