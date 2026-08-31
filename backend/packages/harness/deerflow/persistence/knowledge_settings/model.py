"""Schema V1 ORM row for the PostgreSQL-administered Knowledge settings.

One host-owned singleton row (``id = 1``) carries the platform-level
Knowledge switches, worker limits, quotas, MinIO storage endpoint and the
System Model reference used for segment-summary generation. The Knowledge
package never imports this row; the host adapters project it into the
package's ``KnowledgeSettings``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class KnowledgeSystemSettingsRow(Base):
    """Singleton platform-level Knowledge configuration row."""

    __tablename__ = "knowledge_system_settings"

    id: Mapped[int] = mapped_column(
        SmallInteger,
        primary_key=True,
        default=1,
        server_default=text("1"),
    )
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    worker_concurrency: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        default=2,
        server_default=text("2"),
    )
    task_timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=900,
        server_default=text("900"),
    )
    upload_max_bytes: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=52428800,
        server_default=text("52428800"),
    )
    max_knowledge_bases_per_project: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=20,
        server_default=text("20"),
    )
    max_documents_per_knowledge_base: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=500,
        server_default=text("500"),
    )
    max_segments_per_document: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=5000,
        server_default=text("5000"),
    )
    minio_endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    minio_bucket: Mapped[str | None] = mapped_column(String(255), nullable=True)
    minio_access_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    minio_secure: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    # MinIO secret key sealed with the deerflow/secrets envelope (AES-GCM);
    # the plaintext never appears in responses, logs, audit or repr.
    minio_secret_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    minio_secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    # System Model UUID string (same semantics as vision_bridge's ModelName);
    # NULL means segment-summary generation is not configured system-wide.
    summary_model_name: Mapped[str | None] = mapped_column(String(36), nullable=True)
    query_cache_enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    query_cache_max_entries: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=512,
        server_default=text("512"),
    )
    query_cache_ttl_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=300,
        server_default=text("300"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_knowledge_system_settings_singleton"),
        CheckConstraint(
            "worker_concurrency BETWEEN 1 AND 16",
            name="ck_knowledge_system_settings_worker_concurrency",
        ),
        CheckConstraint(
            "task_timeout_seconds BETWEEN 30 AND 7200",
            name="ck_knowledge_system_settings_task_timeout",
        ),
        CheckConstraint(
            "upload_max_bytes BETWEEN 1 AND 52428800",
            name="ck_knowledge_system_settings_upload_max_bytes",
        ),
        CheckConstraint(
            "max_knowledge_bases_per_project >= 1",
            name="ck_knowledge_system_settings_max_bases",
        ),
        CheckConstraint(
            "max_documents_per_knowledge_base >= 1",
            name="ck_knowledge_system_settings_max_documents",
        ),
        CheckConstraint(
            "max_segments_per_document BETWEEN 1 AND 5000",
            name="ck_knowledge_system_settings_max_segments",
        ),
        CheckConstraint(
            "query_cache_max_entries BETWEEN 16 AND 65536",
            name="ck_knowledge_system_settings_cache_entries",
        ),
        CheckConstraint(
            "query_cache_ttl_seconds BETWEEN 5 AND 86400",
            name="ck_knowledge_system_settings_cache_ttl",
        ),
        # The nonce/ciphertext pair is set or cleared atomically, and a set
        # pair must look like a real AES-GCM envelope.
        CheckConstraint(
            "((minio_secret_nonce IS NULL) = (minio_secret_ciphertext IS NULL)) AND (minio_secret_nonce IS NULL OR (octet_length(minio_secret_nonce) = 12 AND octet_length(minio_secret_ciphertext) >= 16))",
            name="ck_knowledge_system_settings_secret_pair",
        ),
        # The module may only be switched on with a complete MinIO target.
        CheckConstraint(
            "NOT enabled OR (minio_endpoint IS NOT NULL AND minio_bucket IS NOT NULL AND minio_access_key IS NOT NULL AND minio_secret_nonce IS NOT NULL AND minio_secret_ciphertext IS NOT NULL)",
            name="ck_knowledge_system_settings_enabled_requires_minio",
        ),
    )

    def __repr__(self) -> str:
        # The encrypted MinIO secret components must not reach logs or
        # diagnostics, so the generic all-columns repr is narrowed.
        cols = ", ".join(f"{key}={value!r}" for key, value in self.to_dict(exclude={"minio_secret_nonce", "minio_secret_ciphertext"}).items())
        return f"{type(self).__name__}({cols})"


__all__ = ["KnowledgeSystemSettingsRow"]
