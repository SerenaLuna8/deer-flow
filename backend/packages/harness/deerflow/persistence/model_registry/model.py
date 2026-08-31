"""Schema V1 ORM rows for the host-owned retrieval model registry.

``model_providers`` stores one OpenAI-compatible endpoint plus its encrypted
API Key; ``model_provider_models`` stores the type-specific embedding and
rerank models offered by that endpoint. Knowledge Bases bind these model rows
by UUID; the reverse foreign keys live in the Schema V1 SQL snapshot next to
the package-owned ``knowledge_bases`` table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ModelProviderRow(Base):
    """One retrieval model endpoint and its current encrypted API Key."""

    __tablename__ = "model_providers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    request_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=30,
        server_default=text("30"),
    )
    api_key_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    api_key_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint("btrim(name) <> ''", name="ck_model_providers_name"),
        CheckConstraint("btrim(base_url) <> ''", name="ck_model_providers_base_url"),
        CheckConstraint(
            "request_timeout_seconds BETWEEN 1 AND 300",
            name="ck_model_providers_timeout",
        ),
        CheckConstraint(
            "octet_length(api_key_nonce) = 12 AND octet_length(api_key_ciphertext) >= 16",
            name="ck_model_providers_secret",
        ),
        Index("uq_model_providers_name", text("lower(name)"), unique=True),
    )

    def __repr__(self) -> str:
        # Encrypted API-Key components must not reach logs or diagnostics, so
        # the generic all-columns repr is narrowed to the non-secret columns.
        cols = ", ".join(f"{key}={value!r}" for key, value in self.to_dict(exclude={"api_key_nonce", "api_key_ciphertext"}).items())
        return f"{type(self).__name__}({cols})"


class ModelProviderModelRow(Base):
    """One immutable-identity embedding or rerank model on one Provider."""

    __tablename__ = "model_provider_models"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("model_providers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_type: Mapped[str] = mapped_column(String(16), nullable=False)
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_batch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "model_type IN ('embedding', 'rerank')",
            name="ck_model_provider_models_type",
        ),
        CheckConstraint(
            "btrim(model_name) <> ''",
            name="ck_model_provider_models_model_name",
        ),
        CheckConstraint(
            "(model_type = 'embedding') = (embedding_dimension IS NOT NULL) AND (embedding_dimension IS NULL OR embedding_dimension BETWEEN 1 AND 16000)",
            name="ck_model_provider_models_dimension",
        ),
        CheckConstraint(
            "(model_type = 'embedding' AND max_batch BETWEEN 1 AND 2048) OR (model_type = 'rerank' AND max_batch BETWEEN 1 AND 256)",
            name="ck_model_provider_models_max_batch",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="ck_model_provider_models_status",
        ),
        UniqueConstraint(
            "provider_id",
            "model_type",
            "model_name",
            name="uq_model_provider_models_identity",
        ),
    )


__all__ = [
    "ModelProviderModelRow",
    "ModelProviderRow",
]
