"""Knowledge configuration limits and secret-safe material contracts."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError


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
