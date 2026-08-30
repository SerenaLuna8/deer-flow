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
    "create_knowledge_project_purger",
    "purge_knowledge_query_history",
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
