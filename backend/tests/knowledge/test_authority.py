"""Host authority adapter gates for project-scoped Knowledge operations."""

from __future__ import annotations

import uuid

import httpx
import pytest
from actweave_knowledge import (
    KNOWLEDGE_NOT_FOUND,
    KNOWLEDGE_STORAGE_UNAVAILABLE,
    KnowledgeChunkPreview,
    KnowledgeChunkPreviewChunk,
    KnowledgeChunkPreviewRequest,
    KnowledgeError,
    KnowledgeSettings,
)
from actweave_knowledge.contracts import KnowledgeMinioSettings
from actweave_knowledge.module import KnowledgeModule
from actweave_knowledge.persistence.models import (
    KnowledgeBaseRow,
    KnowledgeDocumentRow,
    KnowledgeSegmentRow,
)
from fastapi import FastAPI
from registry_helpers import registry_model_port, seed_embedding_model, seed_provider
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.knowledge import gateway
from app.projects.capabilities import Capability
from app.projects.context import resolve_project_context
from deerflow.persistence.bootstrap import _install_full_schema


@pytest.mark.asyncio
async def test_project_authority_rejects_membership_revoked_after_request_admission(
    postgres_database_url: str,
) -> None:
    """The transaction guard must not trust the request-time ProjectContext."""

    from app.knowledge.authority import (
        PrivateWorkKnowledgeAuthority,
        ProjectKnowledgeAuthority,
    )
    from app.private_work.context import PrivateWorkContext

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO users (
                           id, email, username, system_role, created_at,
                           needs_setup, token_version
                       ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
                ),
                {
                    "user_id": str(user_id),
                    "email": f"{user_id.hex}@example.invalid",
                    "username": f"knowledge_auth_{user_id.hex[:8]}",
                },
            )
            await session.execute(
                text(
                    """INSERT INTO projects (
                           id, slug, display_name, created_by_user_id
                       ) VALUES (:project_id, :slug, 'Authority', :user_id)"""
                ),
                {
                    "project_id": project_id,
                    "slug": f"knowledge-auth-{project_id.hex[:8]}",
                    "user_id": str(user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO project_memberships (
                           id, project_id, user_id, role, status, version
                       ) VALUES (
                           :membership_id, :project_id, :user_id,
                           'editor', 'active', 1
                       )"""
                ),
                {
                    "membership_id": membership_id,
                    "project_id": project_id,
                    "user_id": str(user_id),
                },
            )

        async with factory() as session:
            context = await resolve_project_context(
                session,
                user_id,
                project_id,
                "req-knowledge-authority",
            )
        authority = ProjectKnowledgeAuthority(
            context,
            Capability.SHARED_ASSETS_EDIT,
        )
        private_authority = PrivateWorkKnowledgeAuthority(
            PrivateWorkContext.from_project(context),
            Capability.SHARED_ASSETS_EXECUTE,
        )

        async with factory() as session, session.begin():
            await authority.revalidate(session)
        async with factory() as session, session.begin():
            await private_authority.revalidate(session)

        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='removed', version=version + 1,
                              ended_at=now(), end_reason='removed'
                        WHERE id=:membership_id"""
                ),
                {"membership_id": membership_id},
            )

        async with factory() as session, session.begin():
            with pytest.raises(KnowledgeError) as error:
                await authority.revalidate(session)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
        async with factory() as session, session.begin():
            with pytest.raises(KnowledgeError) as error:
                await private_authority.revalidate(session)
        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_chunk_preview_revalidates_membership_after_parser_work(
    postgres_database_url: str,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parser output must not escape after the admitted editor is revoked."""

    from app.knowledge.authority import ProjectKnowledgeAuthority

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    module: KnowledgeModule | None = None
    await _install_full_schema(engine)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    source_path = tmp_path / "preview.txt"
    source_path.write_text("private preview input", encoding="utf-8")
    try:
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO users (
                           id, email, username, system_role, created_at,
                           needs_setup, token_version
                       ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
                ),
                {
                    "user_id": str(user_id),
                    "email": f"{user_id.hex}@example.invalid",
                    "username": f"knowledge_preview_{user_id.hex[:8]}",
                },
            )
            await session.execute(
                text(
                    """INSERT INTO projects (
                           id, slug, display_name, created_by_user_id
                       ) VALUES (:project_id, :slug, 'Preview authority', :user_id)"""
                ),
                {
                    "project_id": project_id,
                    "slug": f"knowledge-preview-{project_id.hex[:8]}",
                    "user_id": str(user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO project_memberships (
                           id, project_id, user_id, role, status, version
                       ) VALUES (
                           :membership_id, :project_id, :user_id,
                           'editor', 'active', 1
                       )"""
                ),
                {
                    "membership_id": membership_id,
                    "project_id": project_id,
                    "user_id": str(user_id),
                },
            )

        async with factory() as session:
            context = await resolve_project_context(
                session,
                user_id,
                project_id,
                "req-knowledge-preview-authority",
            )

        async def _parse_then_revoke(request, settings):  # noqa: ANN001
            del request, settings
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        """UPDATE project_memberships
                              SET status='removed', version=version + 1,
                                  ended_at=now(), end_reason='removed'
                            WHERE id=:membership_id"""
                    ),
                    {"membership_id": membership_id},
                )
            return KnowledgeChunkPreview(
                total=1,
                chunks=(
                    KnowledgeChunkPreviewChunk(
                        position=1,
                        content="撤权后不得返回的预览正文",
                        word_count=12,
                    ),
                ),
            )

        monkeypatch.setattr(
            "actweave_knowledge.module.preview_document_chunks",
            _parse_then_revoke,
        )
        module = KnowledgeModule(
            settings=KnowledgeSettings(),
            session_factory=factory,
            model_port=registry_model_port(),
        )
        authority = ProjectKnowledgeAuthority(
            context,
            Capability.SHARED_ASSETS_EDIT,
        )

        with pytest.raises(KnowledgeError) as error:
            await module.preview_document_chunks(
                KnowledgeChunkPreviewRequest(
                    original_name=source_path.name,
                    source_path=source_path,
                    size_bytes=source_path.stat().st_size,
                ),
                authority=authority,
            )

        assert error.value.code == KNOWLEDGE_NOT_FOUND
    finally:
        if module is not None:
            await module.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_segment_content_api_revalidates_membership_inside_the_read_transaction(
    postgres_database_url: str,
) -> None:
    """A request-time ProjectContext cannot read Segment text after revocation."""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    module: KnowledgeModule | None = None
    await _install_full_schema(engine)
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    membership_id = uuid.uuid4()
    base_id = uuid.uuid4()
    document_id = uuid.uuid4()
    segment_id = uuid.uuid4()
    try:
        provider_id = await seed_provider(factory)
        embedding_model_id = await seed_embedding_model(factory, provider_id, dimension=3)
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """INSERT INTO users (
                           id, email, username, system_role, created_at,
                           needs_setup, token_version
                       ) VALUES (:user_id, :email, :username, 'user', now(), false, 1)"""
                ),
                {
                    "user_id": str(user_id),
                    "email": f"{user_id.hex}@example.invalid",
                    "username": f"knowledge_read_{user_id.hex[:8]}",
                },
            )
            await session.execute(
                text(
                    """INSERT INTO projects (
                           id, slug, display_name, created_by_user_id
                       ) VALUES (:project_id, :slug, 'Read authority', :user_id)"""
                ),
                {
                    "project_id": project_id,
                    "slug": f"knowledge-read-{project_id.hex[:8]}",
                    "user_id": str(user_id),
                },
            )
            await session.execute(
                text(
                    """INSERT INTO project_memberships (
                           id, project_id, user_id, role, status, version
                       ) VALUES (
                           :membership_id, :project_id, :user_id,
                           'editor', 'active', 1
                       )"""
                ),
                {
                    "membership_id": membership_id,
                    "project_id": project_id,
                    "user_id": str(user_id),
                },
            )
            session.add(
                KnowledgeBaseRow(
                    id=base_id,
                    project_id=project_id,
                    name="私密知识库",
                    embedding_model_id=embedding_model_id,
                    status="active",
                )
            )
            await session.flush()
            session.add(
                KnowledgeDocumentRow(
                    id=document_id,
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    name="私密文档",
                    original_name="private.md",
                    storage_key=f"projects/{project_id}/knowledge/{base_id}/{document_id}.md",
                    size_bytes=64,
                    status="ready",
                    version=1,
                    chunk_size=1000,
                    chunk_overlap=100,
                )
            )
            await session.flush()
            session.add(
                KnowledgeSegmentRow(
                    id=segment_id,
                    project_id=project_id,
                    knowledge_base_id=base_id,
                    knowledge_document_id=document_id,
                    document_version=1,
                    position=1,
                    content="撤权后不得返回的正文",
                    source_position={},
                    embedding=[1.0, 0.0, 0.0],
                )
            )

        async with factory() as session:
            admitted_context = await resolve_project_context(
                session,
                user_id,
                project_id,
                "req-knowledge-read-authority",
            )

        # Simulate revocation after Gateway dependency admission but before the
        # module opens its actual Segment read transaction.
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    """UPDATE project_memberships
                          SET status='removed', version=version + 1,
                              ended_at=now(), end_reason='removed'
                        WHERE id=:membership_id"""
                ),
                {"membership_id": membership_id},
            )

        module = KnowledgeModule(
            settings=KnowledgeSettings(
                enabled=True,
                minio=KnowledgeMinioSettings(
                    endpoint="127.0.0.1:9000",
                    bucket="actweave-knowledge-test",
                    access_key="test",
                    secret_key="test",
                ),
            ),
            session_factory=factory,
            model_port=registry_model_port(),
        )
        app = FastAPI()
        app.include_router(gateway.project_router)
        app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: admitted_context
        app.state.knowledge_module = module

        # ``/model-options`` is intentionally absent: since M9 it serves the
        # host registry's non-secret active options (no Knowledge rows), so its
        # membership check lives in request admission, not this transaction.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            responses = [
                await client.get(f"/api/projects/{project_id}/knowledge/health"),
                await client.get(f"/api/projects/{project_id}/knowledge/bases"),
                await client.get(f"/api/projects/{project_id}/knowledge/bases/{base_id}"),
                await client.get(f"/api/projects/{project_id}/knowledge/bases/{base_id}/metadata-fields"),
                await client.get(f"/api/projects/{project_id}/knowledge/bases/{base_id}/documents"),
                await client.get(f"/api/projects/{project_id}/knowledge/documents/{document_id}"),
                await client.get(f"/api/projects/{project_id}/knowledge/documents/{document_id}/segments"),
            ]

        assert [response.status_code for response in responses] == [404] * len(responses)
        assert all(response.json()["detail"]["code"] == KNOWLEDGE_NOT_FOUND for response in responses)
        assert all("撤权后不得返回的正文" not in response.text for response in responses)
    finally:
        if module is not None:
            await module.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_health_final_guard_database_failure_uses_public_error(
    postgres_database_url: str,
) -> None:
    """A final health-guard outage is not exposed as raw SQLAlchemy state."""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _install_full_schema(engine)
    project_id = uuid.uuid4()

    class _Authority:
        actor_user_id = uuid.uuid4()

        def __init__(self) -> None:
            self.project_id = project_id

        async def revalidate(self, session) -> None:  # noqa: ANN001
            del session

    class _DiesOnFinalGuard:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self):  # noqa: ANN204
            self.calls += 1
            if self.calls > 1:
                raise SQLAlchemyError("pool failed after health probes")
            return factory()

    module = KnowledgeModule(
        settings=KnowledgeSettings(),
        session_factory=_DiesOnFinalGuard(),  # type: ignore[arg-type]
        model_port=registry_model_port(),
    )
    try:
        with pytest.raises(KnowledgeError) as error:
            await module.health(authority=_Authority())
        assert error.value.code == KNOWLEDGE_STORAGE_UNAVAILABLE
    finally:
        await module.aclose()
        await engine.dispose()
