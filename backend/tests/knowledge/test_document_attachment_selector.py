"""P4-T4 backend contracts for document initialization and attachment selection."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_INVALID_REQUEST,
    KNOWLEDGE_NOT_FOUND,
    KnowledgeDocumentView,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.documents import KnowledgeDocumentService
from actweave_knowledge.persistence.models import KnowledgeAttachmentRow, KnowledgeDocumentRow
from extraction_test_helpers import (
    ToggleKnowledgeAuthority,
    extraction_harness,
    make_test_file_capability_provider,
)
from fastapi import FastAPI

from app.knowledge import gateway
from app.knowledge.composition import is_knowledge_project_active
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole

_PROJECT_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_BASE_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_DOCUMENT_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_REQUEST_ID = "req-p4-t4-document-attachments"
_NOW = datetime(2026, 8, 31, tzinfo=UTC)


def _document_view(*, initialized: bool) -> KnowledgeDocumentView:
    return KnowledgeDocumentView(
        id=_DOCUMENT_ID if initialized else uuid.UUID("44444444-4444-4444-8444-444444444444"),
        project_id=_PROJECT_ID,
        knowledge_base_id=_BASE_ID,
        name="已发布" if initialized else "从未发布",
        original_name="source.md",
        media_type="text/markdown",
        size_bytes=10,
        status="ready" if initialized else "failed",
        enabled=True,
        version=2 if initialized else 1,
        chunk_size=1000,
        chunk_overlap=100,
        chunk_separator="\\n\\n",
        remove_extra_spaces=False,
        remove_urls_emails=False,
        chunking_mode="general",
        child_chunk_size=500,
        child_chunk_separator="\\n",
        segment_count=0,
        word_count=0,
        hit_count=0,
        doc_metadata={},
        error_message=None,
        delete_error=None,
        created_at=_NOW,
        updated_at=_NOW,
        content_initialized=initialized,
    )


class _GatewayModule:
    def __init__(self) -> None:
        self.initialized = _document_view(initialized=True)
        self.never_published = _document_view(initialized=False)
        self.attachment_calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def list_documents(self, project_id, base_id, *, page, page_size, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        assert (base_id, page, page_size) == (_BASE_ID, 1, 20)
        return [self.initialized, self.never_published], 2

    async def get_document(self, project_id, document_id, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        assert document_id == _DOCUMENT_ID
        return self.initialized

    async def rename_document(self, project_id, document_id, name, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        assert (document_id, name) == (_DOCUMENT_ID, "新名字")
        return self.initialized

    async def list_document_attachments(self, project_id, document_id, *, authority):  # noqa: ANN001
        assert authority.project_id == project_id
        self.attachment_calls.append((project_id, document_id))
        return (
            [
                SimpleNamespace(
                    attachment_id=uuid.UUID("55555555-5555-4555-8555-555555555555"),
                    ref="a" * 64,
                    media_type="image/png",
                    width=320,
                    height=200,
                    extraction_id=uuid.uuid4(),
                    storage_key="must-not-leak",
                    sha256="must-not-leak",
                )
            ],
            2,
        )


def _app(module: _GatewayModule) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    context = ProjectContext(
        user_id=uuid.UUID("88888888-8888-4888-8888-888888888888"),
        project_id=_PROJECT_ID,
        membership_id=uuid.UUID("99999999-9999-4999-8999-999999999999"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset(Capability),
        membership_version=1,
        request_id=_REQUEST_ID,
    )
    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context
    app.state.knowledge_module = module
    return app


@pytest.mark.asyncio
async def test_document_list_get_and_mutation_expose_initialized_publication_fact() -> None:
    module = _GatewayModule()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(module)), base_url="http://test") as client:
        listed = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/bases/{_BASE_ID}/documents")
        fetched = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}")
        renamed = await client.patch(
            f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}",
            json={"name": "新名字"},
        )

    assert listed.status_code == fetched.status_code == renamed.status_code == 200
    assert [item["content_initialized"] for item in listed.json()["items"]] == [True, False]
    assert listed.json()["items"][0]["segment_count"] == 0
    assert fetched.json()["item"]["content_initialized"] is True
    assert renamed.json()["item"]["content_initialized"] is True


@pytest.mark.asyncio
async def test_document_attachment_selector_http_contract_is_locator_free() -> None:
    module = _GatewayModule()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app(module)), base_url="http://test") as client:
        response = await client.get(f"/api/projects/{_PROJECT_ID}/knowledge/documents/{_DOCUMENT_ID}/attachments")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "attachment_id": "55555555-5555-4555-8555-555555555555",
                "ref": "a" * 64,
                "media_type": "image/png",
                "width": 320,
                "height": 200,
            }
        ],
        "document_version": 2,
        "request_id": _REQUEST_ID,
    }
    serialized = json.dumps(response.json())
    for forbidden in ("extraction_id", "storage_key", "url", "quota", "sha256"):
        assert forbidden not in serialized
    assert module.attachment_calls == [(_PROJECT_ID, _DOCUMENT_ID)]


def _document_service(harness) -> KnowledgeDocumentService:  # noqa: ANN001
    return KnowledgeDocumentService(
        session_factory=harness.session_factory,
        settings=KnowledgeSettings.model_validate({"enabled": False}),
        object_store=harness.object_store,  # type: ignore[arg-type]
        quota=harness.quota,
        project_active_check=is_knowledge_project_active,
        file_capabilities=make_test_file_capability_provider(),
    )


@pytest.mark.asyncio
async def test_package_selector_lists_current_ready_attachments_in_stable_ref_order(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        _segment_id, bound_id, _digest, authority = await harness.seed_attachment_read(tmp_path)
        rows = await harness.read_rows()
        document = rows["documents"][0]
        extraction_id = document.published_extraction_id
        assert extraction_id is not None
        unbound_id = uuid.UUID("00000000-0000-4000-8000-000000000002")
        unbound_ref = "0" * 64
        deleting_id = uuid.UUID("00000000-0000-4000-8000-000000000003")
        async with harness.session_factory() as session, session.begin():
            session.add_all(
                [
                    KnowledgeAttachmentRow(
                        id=deleting_id,
                        extraction_id=extraction_id,
                        project_id=harness.project_id,
                        knowledge_base_id=harness.base_id,
                        knowledge_document_id=harness.document_id,
                        sha256="1" * 64,
                        media_type="image/png",
                        size_bytes=12,
                        width=20,
                        height=10,
                        storage_key=f"test-only/{deleting_id}",
                        state="deleting",
                        upload_state="delete_pending",
                        quota_state="committed",
                    ),
                    KnowledgeAttachmentRow(
                        id=unbound_id,
                        extraction_id=extraction_id,
                        project_id=harness.project_id,
                        knowledge_base_id=harness.base_id,
                        knowledge_document_id=harness.document_id,
                        sha256=unbound_ref,
                        media_type="image/webp",
                        size_bytes=12,
                        width=20,
                        height=10,
                        storage_key=f"test-only/{unbound_id}",
                        state="ready",
                        upload_state="stored",
                        quota_state="committed",
                    ),
                ]
            )

        service = _document_service(harness)
        items, document_version = await service.list_document_attachments(
            harness.project_id,
            harness.document_id,
            authority=authority,
        )

        assert document_version == 1
        assert [item.ref for item in items] == sorted([unbound_ref, rows["attachments"][0].sha256])
        assert {item.attachment_id for item in items} == {bound_id, unbound_id}


@pytest.mark.asyncio
async def test_package_selector_revalidates_authority_and_rejects_noneditable_publication(
    postgres_database_url: str,
    tmp_path,
) -> None:
    async with extraction_harness(postgres_database_url) as harness:
        await harness.seed_attachment_read(tmp_path)
        service = _document_service(harness)

        revoked = ToggleKnowledgeAuthority(harness.project_id, uuid.uuid4(), revoked=True)
        with pytest.raises(KnowledgeError) as revoked_error:
            await service.list_document_attachments(
                harness.project_id,
                harness.document_id,
                authority=revoked,
            )
        assert revoked_error.value.code == KNOWLEDGE_NOT_FOUND

        authority = ToggleKnowledgeAuthority(harness.project_id, uuid.uuid4())
        for status in ("failed", "deleting"):
            async with harness.session_factory() as session, session.begin():
                document = await session.get(KnowledgeDocumentRow, harness.document_id)
                assert document is not None
                document.status = status
                document.error_message = "test failure" if status == "failed" else None
                if status == "failed":
                    document.version = 2
            with pytest.raises(KnowledgeError) as status_error:
                await service.list_document_attachments(
                    harness.project_id,
                    harness.document_id,
                    authority=authority,
                )
            assert status_error.value.code == KNOWLEDGE_INVALID_REQUEST

        async with harness.session_factory() as session, session.begin():
            document = await session.get(KnowledgeDocumentRow, harness.document_id)
            assert document is not None
            document.status = "ready"
            document.version = 2
        with pytest.raises(KnowledgeError) as stale_error:
            await service.list_document_attachments(
                harness.project_id,
                harness.document_id,
                authority=authority,
            )
        assert stale_error.value.code == KNOWLEDGE_INVALID_REQUEST

        unpublished_id = uuid.uuid4()
        async with harness.session_factory() as session, session.begin():
            session.add(
                KnowledgeDocumentRow(
                    id=unpublished_id,
                    project_id=harness.project_id,
                    knowledge_base_id=harness.base_id,
                    name="never-published",
                    original_name="never-published.md",
                    storage_key=f"test-only/{unpublished_id}.md",
                    size_bytes=1,
                    status="ready",
                )
            )
        with pytest.raises(KnowledgeError) as unpublished_error:
            await service.list_document_attachments(
                harness.project_id,
                unpublished_id,
                authority=authority,
            )
        assert unpublished_error.value.code == KNOWLEDGE_INVALID_REQUEST

        with pytest.raises(KnowledgeError) as cross_document_error:
            await service.list_document_attachments(
                harness.project_id,
                uuid.uuid4(),
                authority=authority,
            )
        assert cross_document_error.value.code == KNOWLEDGE_NOT_FOUND
