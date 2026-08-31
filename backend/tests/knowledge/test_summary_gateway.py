"""M11 project HTTP projection over real authorization and Knowledge storage."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from actweave_knowledge.persistence.models import KnowledgeBaseRow, KnowledgeDocumentRow, KnowledgeSegmentRow
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from support.system_model_seed import seed_system_model_config
from test_retrieval import _document_row
from test_retrieval_query_cache import _cache_harness, _CacheHarness, _revoke_membership
from test_summary_retrieval import _summary

from app.gateway.deps import project_session
from app.knowledge import gateway
from app.knowledge_settings.service import default_knowledge_settings_row
from app.projects.context import resolve_project_context
from deerflow.persistence.system_settings import SystemModelConfigRow


async def _app(harness: _CacheHarness) -> FastAPI:
    app = FastAPI()
    app.include_router(gateway.project_router)
    app.state.knowledge_module = harness.module
    async with harness.retrieval.factory() as session:
        context = await resolve_project_context(session, harness.authority.actor_user_id, harness.project_id, "summary-api")

    async def session_dependency() -> AsyncIterator[AsyncSession]:
        async with harness.retrieval.factory() as session:
            yield session

    app.dependency_overrides[gateway.require_project_knowledge_read] = lambda: context
    app.dependency_overrides[gateway.require_project_knowledge_edit] = lambda: context
    app.dependency_overrides[project_session] = session_dependency
    return app


@pytest.mark.asyncio
async def test_model_options_project_only_active_summary_model_identity(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url) as harness:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=await _app(harness)), base_url="http://test") as client:
            path = f"/api/projects/{harness.project_id}/knowledge/model-options"
            missing = await client.get(path)
            assert missing.status_code == 200
            assert missing.json()["summary_model"] is None
            model_id = uuid.uuid4()
            async with harness.retrieval.factory() as session, session.begin():
                await seed_system_model_config(session, model_id=model_id, owner_user_id=str(harness.authority.actor_user_id), display_name="系统摘要模型", provider_model="summary-model")
                row = default_knowledge_settings_row()
                row.summary_model_name = str(model_id)
                session.add(row)

            active = await client.get(path)
            assert active.status_code == 200
            assert active.json()["summary_model"] == {"model_name": str(model_id), "display_name": "系统摘要模型"}
            assert len(active.json()["embedding_models"]) == 1
            async with harness.retrieval.factory() as session, session.begin():
                model = await session.get(SystemModelConfigRow, model_id)
                model.status = "suspended"

            disabled = await client.get(path)
            assert disabled.status_code == 200
            assert disabled.json()["summary_model"] is None
            assert len(disabled.json()["embedding_models"]) == 1
            assert "provider_model" not in disabled.json()
            await _revoke_membership(harness)
            revoked = await client.get(path)
            assert revoked.status_code == 404


@pytest.mark.asyncio
async def test_base_patch_reports_atomic_summary_backfill_and_preserves_existing_response_item(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url) as harness:
        async with harness.retrieval.factory() as session, session.begin():
            model_id = uuid.uuid4()
            await seed_system_model_config(session, model_id=model_id, owner_user_id=str(harness.authority.actor_user_id), display_name="摘要模型", provider_model="summary-model")
            settings = default_knowledge_settings_row()
            settings.summary_model_name = str(model_id)
            session.add(settings)
            ready = await session.scalar(select(KnowledgeDocumentRow).where(KnowledgeDocumentRow.knowledge_base_id == harness.base_id))
            ready.published_version = ready.version
            unpublished = _document_row(harness.project_id, harness.base_id, name="未就绪文档", status="queued")
            session.add(unpublished)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=await _app(harness)), base_url="http://test") as client:
            path = f"/api/projects/{harness.project_id}/knowledge/bases/{harness.base_id}"
            enabled = await client.patch(path, json={"summary_index_enabled": True})

            assert enabled.status_code == 200
            assert enabled.json()["item"]["summary_index_enabled"] is True
            assert enabled.json()["summary_backfill"] == {"accepted_document_count": 1, "skipped_document_ids": [str(unpublished.id)]}
            fetched = await client.get(path)
            assert fetched.json()["item"]["summary_index_enabled"] is True
            unchanged = await client.patch(path, json={"description": "保持原有更新行为"})
            assert unchanged.status_code == 200
            assert unchanged.json()["summary_backfill"] is None
            disabled = await client.patch(path, json={"summary_index_enabled": False})
            assert disabled.json()["item"]["summary_index_enabled"] is False
            assert disabled.json()["summary_backfill"] is None
            invalid = await client.patch(path, json={"summary_index_enabled": "true"})
            assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_search_debug_projects_summary_attribution_and_query_cache_counts(postgres_database_url: str) -> None:
    async with _cache_harness(postgres_database_url) as harness:
        async with harness.retrieval.factory() as session, session.begin():
            base = await session.get(KnowledgeBaseRow, harness.base_id)
            base.summary_index_enabled = True
            segment = await session.scalar(select(KnowledgeSegmentRow).where(KnowledgeSegmentRow.knowledge_base_id == harness.base_id, KnowledgeSegmentRow.position == 2))
            session.add(_summary(segment))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=await _app(harness)), base_url="http://test") as client:
            path = f"/api/projects/{harness.project_id}/knowledge/search"
            cold = await client.post(path, json={"query": "产品维护", "debug": True})
            warm = await client.post(path, json={"query": "产品维护", "debug": True})

        assert cold.status_code == warm.status_code == 200
        assert cold.json()["diagnostics"]["counts"]["query_embedding_cache_misses"] == 1
        counts = warm.json()["diagnostics"]["counts"]
        assert counts["summary_candidates"] == 1
        assert counts["query_embedding_cache_hits"] == 1
        assert counts["query_embedding_cache_misses"] == 0
        details = {hit["segment_id"]: hit for hit in warm.json()["diagnostics"]["hit_diagnostics"]}
        assert details[str(segment.id)]["matched_via"] == "summary"
        assert cold.json()["citations"] == warm.json()["citations"]
        assert "SUMMARY_ONLY_MARKER" not in warm.text
