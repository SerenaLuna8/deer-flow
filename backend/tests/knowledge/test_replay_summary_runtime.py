"""Real loopback Chat Completions through the production summary runtime."""

from __future__ import annotations

import hashlib
import os

import pytest
from _replay_fixture import build_config_yaml, install_replay_model_adapter, prepare_replay_runtime_catalog
from actweave_knowledge import KNOWLEDGE_TASK_FAILED, KnowledgeError
from actweave_knowledge.bases import KnowledgeBaseService
from actweave_knowledge.ingestion.reembed import KnowledgeReembedHandler
from actweave_knowledge.ingestion.summarize import KnowledgeSummarizeHandler
from actweave_knowledge.models import KnowledgeModelClient
from actweave_knowledge.persistence.models import KnowledgeBaseRow, KnowledgeSegmentRow, KnowledgeTaskRow
from actweave_knowledge.tasks import KnowledgeTaskWorker
from replay_knowledge import (
    DOC_RERANK_MARKER,
    REPLAY_SUMMARY_OUTPUT_MARKER,
    KnowledgeReplayState,
    ReplayKnowledgeProviderServer,
    ReplayMinioSettings,
    replay_embedding,
    seed_replay_knowledge_settings,
    seed_replay_model_registry,
    seed_replay_summary_model,
)
from sqlalchemy import update
from test_summaries import harness  # noqa: F401 - shared PostgreSQL fixture

from app.knowledge.composition import is_knowledge_project_active
from app.knowledge.model_port import RegistryKnowledgeModelPort
from app.knowledge.summary_runtime import DatabaseKnowledgeSummaryRuntime
from app.knowledge_settings.service import load_knowledge_settings_from_db
from app.system_settings import validation
from deerflow.config.app_config import AppConfig
from deerflow.secrets import SecretKey


@pytest.mark.asyncio
async def test_replay_seed_and_summary_use_database_model_and_real_http(harness, postgres_database_url, monkeypatch, tmp_path):  # noqa: F811 - imported fixture
    monkeypatch.setattr(validation, "PROVIDER_ADAPTERS", dict(validation.PROVIDER_ADAPTERS))
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    install_replay_model_adapter()
    assert "knowledge:" not in build_config_yaml(home=tmp_path)
    source = DOC_RERANK_MARKER + " 原始文档事实" * 50
    claim = await harness.seed([source])
    state = KnowledgeReplayState()
    provider = ReplayKnowledgeProviderServer(state)
    provider.start()
    client = KnowledgeModelClient()
    try:
        embedding_id, _ = await seed_replay_model_registry(postgres_database_url, base_url=provider.base_url)
        await prepare_replay_runtime_catalog(postgres_database_url)
        await prepare_replay_runtime_catalog(postgres_database_url)  # seed is idempotent
        summary_id = await seed_replay_summary_model(postgres_database_url)
        assert await seed_replay_summary_model(postgres_database_url) == summary_id
        await seed_replay_knowledge_settings(postgres_database_url, settings=ReplayMinioSettings("minio.invalid:9000", "replay-access", "replay-secret"), bucket="replay-summary-test", summary_model_name=str(summary_id))
        async with harness.factory() as session, session.begin():
            await session.execute(update(KnowledgeBaseRow).values(embedding_model_id=embedding_id))
            await session.execute(update(KnowledgeSegmentRow).values(embedding=replay_embedding(source, 64)))
            await session.execute(update(KnowledgeTaskRow).where(KnowledgeTaskRow.id == claim.id).values(status="queued", claim_token=None, lease_until=None, attempt_count=0))
        settings = await load_knowledge_settings_from_db(harness.factory, secret_key=SecretKey.from_environment())
        assert settings.enabled and settings.minio.bucket == "replay-summary-test"
        config = AppConfig.model_validate({"sandbox": {"use": "deerflow.sandbox.local:LocalSandboxProvider"}})
        port = RegistryKnowledgeModelPort(secret_key=SecretKey.from_environment(), model_runtime=DatabaseKnowledgeSummaryRuntime(app_config=config, session_factory=harness.factory))
        handler = KnowledgeSummarizeHandler(session_factory=harness.factory, model_client=client, model_port=port)
        worker = KnowledgeTaskWorker(
            session_factory=harness.factory,
            handlers={"summarize_document": handler, "reembed_document": KnowledgeReembedHandler(session_factory=harness.factory, model_client=client, model_port=port)},
            project_active_check=is_knowledge_project_active,
            concurrency=1,
            task_timeout_seconds=120,
        )
        assert await worker._run_once()
        summary = (await harness.summaries())[0]
        assert summary.content == f"{REPLAY_SUMMARY_OUTPUT_MARKER} {hashlib.sha256(source.encode()).hexdigest()[:16]}"
        assert len(summary.embedding) == 64 and summary.embedding[1] == 1
        assert state.snapshot()["chat_calls"] == 1 and state.snapshot()["embedding_calls"] == 1
        base_service = KnowledgeBaseService(session_factory=harness.factory, settings=settings, model_port=port)
        await base_service.rebuild_knowledge_base(harness.project_id, harness.base_id, embedding_model_id=embedding_id)
        assert await worker._run_once()
        rebuilt_summary = (await harness.summaries())[0]
        assert rebuilt_summary.id == summary.id and rebuilt_summary.content == summary.content and rebuilt_summary.document_version == 2
        assert state.snapshot()["chat_calls"] == 1 and state.snapshot()["embedding_calls"] == 2
        with state.lock:
            state.chat_failures_remaining = 1
        with pytest.raises(KnowledgeError) as caught:
            await port.generate_summary(model_ref=str(summary_id), prompt="源段落：\n回放错误")
        assert caught.value.code == KNOWLEDGE_TASK_FAILED
        assert state.snapshot()["chat_calls"] == 2  # no hidden SDK retry
    finally:
        await client.aclose()
        provider.stop()
