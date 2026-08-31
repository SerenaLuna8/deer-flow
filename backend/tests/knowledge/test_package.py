"""M0 gates: public export surface, dependency direction, settings validation."""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

BACKEND_ROOT = Path(__file__).resolve().parents[2]
KNOWLEDGE_SRC = BACKEND_ROOT / "packages" / "knowledge" / "actweave_knowledge"
HARNESS_SRC = BACKEND_ROOT / "packages" / "harness" / "deerflow"
APP_SRC = BACKEND_ROOT / "app"

PUBLIC_EXPORTS = [
    "create_knowledge_module",
    "create_knowledge_model_client",
    "create_knowledge_project_purger",
    "purge_knowledge_query_history",
    "retrieval_model_in_use",
    "KnowledgeModule",
    "KnowledgeProjectPurger",
    "KnowledgeProjectAuthority",
    "KnowledgeSettings",
    "KnowledgeModelPort",
    "KnowledgeModelType",
    "KnowledgeEmbeddingMaterial",
    "KnowledgeRerankMaterial",
    "KnowledgeError",
    "KnowledgeBaseCreate",
    "KnowledgeBaseUpdate",
    "KnowledgeBaseView",
    "KnowledgeDocumentUpload",
    "KnowledgeDocumentView",
    "KnowledgeSegmentView",
    "KnowledgeSearchRequest",
    "KnowledgeSearchResult",
    "KnowledgeCitation",
    "KnowledgeHealth",
    # M10 T1: hit / diagnostics / detail / reprocessing / metadata contracts.
    "KnowledgeSearchHit",
    "KnowledgeMatchedChild",
    "KnowledgeSearchDiagnostics",
    "KnowledgeHitDiagnostics",
    "KnowledgeRouteCounts",
    "KnowledgeSearchTimings",
    "KnowledgeSegmentDetail",
    "KnowledgeSegmentChildView",
    "KnowledgeReparseRequest",
    "KnowledgeReparsePreview",
    "KnowledgeTaskProgress",
    # M10 T2: base-level re-embed admission outcome.
    "KnowledgeRebuildResult",
    "KnowledgeFilterFieldView",
    "KnowledgeBaseFilterFields",
    "KnowledgeMetadataBatchPatch",
    "KnowledgeScoreKind",
    "KnowledgeRetrievalMode",
    "KnowledgeRecallRoute",
    "KnowledgeEmptyReason",
    "KnowledgeContentState",
    "KnowledgeTaskStage",
    "KnowledgeFilterFieldKind",
    "KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET",
    "KNOWLEDGE_MAX_MATCHED_CHILDREN",
    "KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE",
    "KNOWLEDGE_LEXICAL_VERSION",
    "KNOWLEDGE_STRATEGY_VERSION",
    "KNOWLEDGE_BUILTIN_FILTER_FIELDS",
    "KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS",
    "KNOWLEDGE_MAX_BATCH_METADATA_FIELDS",
    # M11 T1: segment-summary index, settings cache knobs, matched-via.
    "KnowledgeSegmentSummaryView",
    "KnowledgeSummaryBackfill",
    "KnowledgeBaseUpdateResult",
    "KnowledgeMatchedVia",
    "KNOWLEDGE_SUMMARY_PROMPT_VERSION",
    "KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS",
    "KNOWLEDGE_SUMMARY_MAX_CHARS",
    "KNOWLEDGE_SUMMARY_MAX_TOKENS",
]


def test_root_package_exports_the_public_surface() -> None:
    import actweave_knowledge

    for name in PUBLIC_EXPORTS:
        assert hasattr(actweave_knowledge, name), f"missing export: {name}"
        assert name in actweave_knowledge.__all__, f"missing from __all__: {name}"

    assert sorted(set(actweave_knowledge.__all__)) == sorted(actweave_knowledge.__all__), "__all__ must not contain duplicates"


def test_root_package_does_not_export_internals() -> None:
    import actweave_knowledge

    for internal in ("PostgreSQLStore", "MinioObjectStore", "KnowledgeModelClient"):
        assert internal not in actweave_knowledge.__all__


def test_model_client_factory_builds_the_internal_provider_client() -> None:
    """The factory is the only public construction path for a probe client.

    Hosts that need a registry-owned client (e.g. the Gateway lifespan for the
    retrieval model registry when Knowledge is disabled) obtain it here without
    the class itself joining the public surface.
    """

    import asyncio

    from actweave_knowledge import create_knowledge_model_client
    from actweave_knowledge.models.client import KnowledgeModelClient

    client = create_knowledge_model_client()
    assert isinstance(client, KnowledgeModelClient)
    asyncio.run(client.aclose())
    # Closing twice stays safe: the lifespan may release it on failed startup.
    asyncio.run(client.aclose())


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_knowledge_package_never_imports_host_modules() -> None:
    forbidden = {"app", "deerflow"}
    offenders: list[str] = []
    for source in sorted(KNOWLEDGE_SRC.rglob("*.py")):
        overlap = _module_imports(source) & forbidden
        if overlap:
            offenders.append(f"{source.relative_to(BACKEND_ROOT)} imports {sorted(overlap)}")
    assert not offenders, "\n".join(offenders)


def test_harness_never_imports_knowledge_package() -> None:
    offenders: list[str] = []
    for source in sorted(HARNESS_SRC.rglob("*.py")):
        if "actweave_knowledge" in source.read_text(encoding="utf-8"):
            offenders.append(str(source.relative_to(BACKEND_ROOT)))
    assert not offenders, f"harness must not reference actweave_knowledge: {offenders}"


def test_settings_default_to_disabled_with_documented_quotas() -> None:
    from actweave_knowledge import KnowledgeSettings

    settings = KnowledgeSettings()

    assert settings.enabled is False
    assert settings.worker_concurrency == 2
    assert settings.task_timeout_seconds == 900
    assert settings.upload_max_bytes == 52428800
    assert settings.max_knowledge_bases_per_project == 20
    assert settings.max_documents_per_knowledge_base == 500
    assert settings.max_segments_per_document == 5000
    assert settings.minio is None


def test_settings_cap_the_per_document_vector_entry_budget() -> None:
    """Operators cannot re-enable an unbounded ingestion through config."""

    from actweave_knowledge import KnowledgeSettings

    assert KnowledgeSettings(max_segments_per_document=5000).max_segments_per_document == 5000
    with pytest.raises(ValidationError):
        KnowledgeSettings(max_segments_per_document=5001)


def test_settings_cap_uploads_at_the_bounded_single_put_limit() -> None:
    """Operators cannot configure a single PUT above the process memory budget."""

    from actweave_knowledge import KnowledgeSettings

    maximum = 50 * 1024**2
    assert KnowledgeSettings(upload_max_bytes=maximum).upload_max_bytes == maximum
    with pytest.raises(ValidationError):
        KnowledgeSettings(upload_max_bytes=maximum + 1)


def test_settings_require_minio_when_enabled() -> None:
    from actweave_knowledge import KnowledgeSettings

    with pytest.raises(ValidationError):
        KnowledgeSettings(enabled=True)

    settings = KnowledgeSettings.model_validate(
        {
            "enabled": True,
            "minio": {
                "endpoint": "127.0.0.1:9000",
                "bucket": "actweave-knowledge",
                "access_key": "minio-access-value",
                "secret_key": "minio-secret-value",
                "secure": False,
            },
        }
    )
    assert settings.minio is not None
    assert settings.minio.endpoint == "127.0.0.1:9000"
    assert settings.minio.secret_key.get_secret_value() == "minio-secret-value"

    # Credentials must never surface through repr or non-secret dumps.
    rendered = repr(settings) + str(settings.minio) + str(settings.minio.model_dump())
    assert "minio-secret-value" not in rendered
    assert "minio-access-value" not in repr(settings.minio)


def test_model_materials_hide_api_key_from_repr() -> None:
    from actweave_knowledge import KnowledgeEmbeddingMaterial, KnowledgeRerankMaterial

    embedding = KnowledgeEmbeddingMaterial(
        model_id=uuid4(),
        base_url="https://api.siliconflow.cn/v1",
        model_name="embed",
        dimension=1024,
        max_batch=64,
        request_timeout_seconds=30,
        api_key="plain-embedding-key",
    )
    rerank = KnowledgeRerankMaterial(
        model_id=uuid4(),
        base_url="https://api.siliconflow.cn/v1",
        model_name="rerank",
        max_batch=32,
        request_timeout_seconds=30,
        api_key="plain-rerank-key",
    )

    assert "plain-embedding-key" not in repr(embedding)
    assert "plain-rerank-key" not in repr(rerank)


def test_settings_reject_console_style_endpoint_with_scheme() -> None:
    from actweave_knowledge import KnowledgeSettings

    with pytest.raises(ValidationError):
        KnowledgeSettings.model_validate(
            {
                "enabled": True,
                "minio": {
                    "endpoint": "http://127.0.0.1:9001",
                    "bucket": "actweave-knowledge",
                    "access_key": "ak",
                    "secret_key": "sk",
                },
            }
        )


def test_settings_reject_unknown_fields() -> None:
    from actweave_knowledge import KnowledgeSettings

    with pytest.raises(ValidationError):
        KnowledgeSettings.model_validate({"enabled": False, "unknown_field": 1})


def test_create_knowledge_module_binds_host_resources() -> None:
    from actweave_knowledge import KnowledgeSettings, create_knowledge_module

    class _MemoryModelPort:
        async def lock_model_for_binding(self, session, model_id, model_type):  # pragma: no cover - shape only
            del session, model_id, model_type

        async def embedding_material(self, session, model_id):  # pragma: no cover - shape only
            raise NotImplementedError

        async def rerank_material(self, session, model_id):  # pragma: no cover - shape only
            raise NotImplementedError

    async def _project_active(session, project_id):  # pragma: no cover - shape only
        del session, project_id
        return True

    module = create_knowledge_module(
        settings=KnowledgeSettings(),
        session_factory=object(),  # type: ignore[arg-type]  # shape-only for M0
        model_port=_MemoryModelPort(),
        project_active_check=_project_active,
    )
    assert module.settings.enabled is False


def test_knowledge_error_carries_code_and_message() -> None:
    from actweave_knowledge import KnowledgeError

    error = KnowledgeError("KNOWLEDGE_NOT_FOUND", "Knowledge Base 不存在")
    assert error.code == "KNOWLEDGE_NOT_FOUND"
    assert error.message == "Knowledge Base 不存在"


def test_search_request_is_frozen_and_defaults_are_unset() -> None:
    from actweave_knowledge import KnowledgeSearchRequest

    request = KnowledgeSearchRequest(
        project_id=uuid4(),
        owner_user_id=uuid4(),
        query="hello",
    )
    assert request.knowledge_base_ids is None
    assert request.top_k is None
    assert request.score_threshold is None
    with pytest.raises(Exception):
        request.query = "changed"  # type: ignore[misc]


def _sample_hit(score: float = 0.9):
    from actweave_knowledge import KnowledgeCitation, KnowledgeSearchHit

    citation = KnowledgeCitation(
        knowledge_base_id=uuid4(),
        knowledge_base_name="产品手册",
        document_id=uuid4(),
        document_name="发布说明.pdf",
        segment_id=uuid4(),
        segment_position=1,
        snippet="短摘要",
        score=score,
        document_version=3,
        content_digest="a" * 64,
        score_kind="rerank",
    )
    return KnowledgeSearchHit(
        citation=citation,
        passage="完整的父分段正文，包含第 320 字符之后的答案内容。",
        document_version=3,
        content_digest="a" * 64,
        local_score=score,
        local_score_kind="rerank",
        score_domain=f"rerank:{citation.knowledge_base_id}",
        ranking_method="rerank",
        ranking_score=score,
    )


class TestM10ContractShapes:
    """T1 gates: hits are the single result source and new DTOs hold shape."""

    def test_search_result_derives_citations_from_hits_only(self) -> None:
        from actweave_knowledge import KnowledgeSearchResult

        hit = _sample_hit()
        result = KnowledgeSearchResult(hits=(hit,))
        assert result.citations == (hit.citation,)
        with pytest.raises(TypeError):
            KnowledgeSearchResult(citations=(hit.citation,))  # type: ignore[call-arg]

    def test_citation_keeps_optional_provenance_for_old_messages(self) -> None:
        from actweave_knowledge import KnowledgeCitation

        legacy = KnowledgeCitation(
            knowledge_base_id=uuid4(),
            knowledge_base_name="旧库",
            document_id=uuid4(),
            document_name="旧文档",
            segment_id=uuid4(),
            segment_position=2,
            snippet="旧摘要",
            score=0.5,
        )
        assert legacy.document_version is None
        assert legacy.content_digest is None
        assert legacy.score_kind is None

    def test_metadata_filter_defaults_to_custom_field_kind(self) -> None:
        from actweave_knowledge import KnowledgeMetadataFilter

        item = KnowledgeMetadataFilter(name="部门", operator="eq", value="工程")
        assert item.field_kind == "custom"
        builtin = KnowledgeMetadataFilter(
            name="document_name",
            operator="contains",
            value="发布",
            field_kind="builtin",
        )
        assert builtin.field_kind == "builtin"

    def test_base_dtos_carry_retrieval_mode_with_semantic_default(self) -> None:
        from actweave_knowledge import KnowledgeBaseCreate, KnowledgeBaseUpdate

        create = KnowledgeBaseCreate(name="库", embedding_model_id=uuid4())
        assert create.retrieval_mode == "semantic"
        update = KnowledgeBaseUpdate()
        assert update.retrieval_mode is None

    def test_search_request_supports_debug_and_one_shot_mode_override(self) -> None:
        from actweave_knowledge import KnowledgeSearchRequest

        request = KnowledgeSearchRequest(
            project_id=uuid4(),
            owner_user_id=uuid4(),
            query="混合检索",
        )
        assert request.retrieval_mode is None
        assert request.debug is False

    def test_reparse_request_freezes_expected_version_and_chunk_settings(self) -> None:
        from actweave_knowledge import KnowledgeReparseRequest

        request = KnowledgeReparseRequest(expected_version=4)
        assert request.expected_version == 4
        assert request.chunking_mode == "general"
        assert request.chunk_size == 1000

    def test_task_progress_projection_never_carries_execution_material(self) -> None:
        import dataclasses

        from actweave_knowledge import KnowledgeTaskProgress

        names = {f.name for f in dataclasses.fields(KnowledgeTaskProgress)}
        assert {"stage", "completed_units", "total_units", "attempt_count"} <= names
        assert not names & {"claim_token", "lease_until", "storage_key"}

    def test_frozen_constants_match_the_t0_baseline_fixture(self) -> None:
        import json as json_module

        import actweave_knowledge as pkg

        baseline = json_module.loads((Path(__file__).parent / "fixtures" / "m10_contract_baseline.json").read_text(encoding="utf-8"))
        assert pkg.KNOWLEDGE_GLOBAL_PARENT_CANDIDATE_BUDGET == baseline["candidate_budget"]["global_parent_budget"]
        assert pkg.KNOWLEDGE_MAX_MATCHED_CHILDREN == baseline["child_match_projection"]["max_matched_children_per_hit"]
        assert pkg.KNOWLEDGE_SEGMENT_DETAIL_CHILD_PAGE_SIZE == baseline["segment_detail"]["child_page_size_max"]
        assert pkg.KNOWLEDGE_LEXICAL_VERSION == baseline["lexical_v1"]["lexical_version"]
        assert tuple(baseline["metadata_contract"]["builtin_fields"]) == pkg.KNOWLEDGE_BUILTIN_FILTER_FIELDS
        assert pkg.KNOWLEDGE_MAX_BATCH_METADATA_DOCUMENTS == baseline["metadata_contract"]["batch_limits"]["max_documents"]
        assert pkg.KNOWLEDGE_MAX_BATCH_METADATA_FIELDS == baseline["metadata_contract"]["batch_limits"]["max_fields"]


class TestM11ContractShapes:
    """M11 T1 gates: summary/settings/cache contracts hold their frozen shape."""

    def test_summary_constants_match_the_design_freeze(self) -> None:
        import actweave_knowledge as pkg

        assert pkg.KNOWLEDGE_SUMMARY_PROMPT_VERSION == 1
        assert pkg.KNOWLEDGE_SUMMARY_MIN_SOURCE_CHARS == 200
        assert pkg.KNOWLEDGE_SUMMARY_MAX_CHARS == 1000
        assert pkg.KNOWLEDGE_SUMMARY_MAX_TOKENS == 1024

    def test_matched_via_and_task_literals_cover_the_summary_route(self) -> None:
        from typing import get_args

        from actweave_knowledge import KnowledgeMatchedVia
        from actweave_knowledge.contracts import (
            KnowledgeIndexingTaskKind,
            KnowledgeTaskStage,
        )

        assert get_args(KnowledgeMatchedVia) == ("segment", "child", "summary")
        assert "summarize_document" in get_args(KnowledgeIndexingTaskKind)
        assert "summarizing" in get_args(KnowledgeTaskStage)

    def test_settings_carry_bounded_query_cache_knobs(self) -> None:
        from actweave_knowledge import KnowledgeSettings

        settings = KnowledgeSettings()
        assert settings.query_cache_enabled is True
        assert settings.query_cache_max_entries == 512
        assert settings.query_cache_ttl_seconds == 300

        assert KnowledgeSettings(query_cache_max_entries=16).query_cache_max_entries == 16
        assert KnowledgeSettings(query_cache_max_entries=65536).query_cache_max_entries == 65536
        assert KnowledgeSettings(query_cache_ttl_seconds=5).query_cache_ttl_seconds == 5
        assert KnowledgeSettings(query_cache_ttl_seconds=86400).query_cache_ttl_seconds == 86400
        for invalid in ({"query_cache_max_entries": 15}, {"query_cache_max_entries": 65537}, {"query_cache_ttl_seconds": 4}, {"query_cache_ttl_seconds": 86401}):
            with pytest.raises(ValidationError):
                KnowledgeSettings.model_validate(invalid)

    def test_base_view_and_update_carry_the_summary_index_switch(self) -> None:
        import dataclasses

        from actweave_knowledge import KnowledgeBaseUpdate, KnowledgeBaseView

        view_fields = {f.name for f in dataclasses.fields(KnowledgeBaseView)}
        assert "summary_index_enabled" in view_fields
        update = KnowledgeBaseUpdate()
        assert update.summary_index_enabled is None

    def test_update_result_wraps_base_view_with_optional_backfill(self) -> None:
        from datetime import UTC, datetime

        from actweave_knowledge import (
            KnowledgeBaseUpdateResult,
            KnowledgeBaseView,
            KnowledgeSummaryBackfill,
        )

        now = datetime.now(UTC)
        base = KnowledgeBaseView(
            id=uuid4(),
            project_id=uuid4(),
            name="库",
            description="",
            embedding_model_id=None,
            reranker_model_id=None,
            retrieval_mode="semantic",
            summary_index_enabled=False,
            status="active",
            document_count=0,
            default_top_k=4,
            default_score_threshold=0.2,
            delete_error=None,
            created_at=now,
            updated_at=now,
        )
        plain = KnowledgeBaseUpdateResult(base=base)
        assert plain.summary_backfill is None
        skipped = (uuid4(),)
        backfill = KnowledgeSummaryBackfill(accepted_document_count=3, skipped_document_ids=skipped)
        result = KnowledgeBaseUpdateResult(base=base, summary_backfill=backfill)
        assert result.summary_backfill is not None
        assert result.summary_backfill.accepted_document_count == 3
        assert result.summary_backfill.skipped_document_ids == skipped

    def test_hit_diagnostics_default_matched_via_is_segment(self) -> None:
        from actweave_knowledge import KnowledgeHitDiagnostics

        diagnostics = KnowledgeHitDiagnostics(
            segment_id=uuid4(),
            local_score=0.5,
            local_score_kind="cosine",
            score_domain="cosine:embed",
            ranking_method="cosine",
            ranking_score=0.5,
        )
        assert diagnostics.matched_via == "segment"

    def test_route_counts_add_summary_and_cache_counters_defaulting_zero(self) -> None:
        from actweave_knowledge import KnowledgeRouteCounts

        counts = KnowledgeRouteCounts()
        assert counts.summary_candidates == 0
        assert counts.query_embedding_cache_hits == 0
        assert counts.query_embedding_cache_misses == 0

    def test_segment_detail_carries_optional_system_summary(self) -> None:
        import dataclasses
        from datetime import UTC, datetime

        from actweave_knowledge import KnowledgeSegmentDetail, KnowledgeSegmentSummaryView

        detail_fields = {f.name: f for f in dataclasses.fields(KnowledgeSegmentDetail)}
        assert "summary" in detail_fields
        assert detail_fields["summary"].default is None
        summary = KnowledgeSegmentSummaryView(content="系统生成摘要", created_at=datetime.now(UTC))
        assert summary.content == "系统生成摘要"

    def test_model_port_protocol_declares_the_summary_methods(self) -> None:
        from actweave_knowledge import KnowledgeModelPort

        assert callable(getattr(KnowledgeModelPort, "resolve_summary_model", None))
        assert callable(getattr(KnowledgeModelPort, "generate_summary", None))


def _stub_module_with_health(*, storage_ok: bool):
    from actweave_knowledge import KnowledgeHealth

    class _Module:
        async def health(self) -> KnowledgeHealth:
            return KnowledgeHealth(
                enabled=True,
                database_ok=True,
                storage_ok=storage_ok,
                message="" if storage_ok else "对象存储 bucket 不可访问",
            )

    return _Module()


@pytest.mark.asyncio
async def test_startup_storage_check_fails_fast_when_bucket_unreachable() -> None:
    from app.knowledge.composition import (
        KnowledgeStorageNotReady,
        require_knowledge_storage_ready,
    )

    with pytest.raises(KnowledgeStorageNotReady, match="对象存储"):
        await require_knowledge_storage_ready(_stub_module_with_health(storage_ok=False))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_startup_storage_check_passes_when_bucket_reachable() -> None:
    from app.knowledge.composition import require_knowledge_storage_ready

    await require_knowledge_storage_ready(_stub_module_with_health(storage_ok=True))  # type: ignore[arg-type]
