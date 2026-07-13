from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class McpServerRow(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    current_published_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_mcp_servers_scope_project",
        ),
        CheckConstraint("status IN ('active', 'archived', 'suspended')", name="ck_mcp_servers_status"),
        CheckConstraint("version >= 1", name="ck_mcp_servers_version"),
        UniqueConstraint("id", "scope", name="uq_mcp_servers_id_scope"),
        UniqueConstraint("source_key", name="uq_mcp_servers_source_key"),
        ForeignKeyConstraint(
            ["id", "current_published_version_id"],
            ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"],
            name="fk_mcp_servers_current_published_version",
            use_alter=True,
        ),
        Index(
            "uq_mcp_servers_system_slug",
            func.lower(slug),
            unique=True,
            postgresql_where=text("scope = 'system'"),
        ),
        Index(
            "uq_mcp_servers_project_slug",
            project_id,
            func.lower(slug),
            unique=True,
            postgresql_where=text("scope = 'project'"),
        ),
    )


class McpServerVersionRow(Base):
    __tablename__ = "mcp_server_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mcp_servers.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workflow_status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    transport: Mapped[str] = mapped_column(String(24), nullable=False)
    command: Mapped[str | None] = mapped_column(Text, nullable=True)
    args: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    non_secret_env: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    non_secret_headers: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    oauth_metadata: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    routing: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    tool_overrides: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30, server_default=text("30"))
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mcp_server_versions.id", ondelete="RESTRICT"), nullable=True)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_mcp_server_versions_number"),
        CheckConstraint(
            "workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')",
            name="ck_mcp_server_versions_workflow_status",
        ),
        CheckConstraint(
            "transport IN ('stdio', 'sse', 'http', 'streamable_http')",
            name="ck_mcp_server_versions_transport",
        ),
        CheckConstraint("timeout_seconds > 0", name="ck_mcp_server_versions_timeout"),
        CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_mcp_server_versions_checksum"),
        UniqueConstraint("mcp_server_id", "version_number", name="uq_mcp_server_versions_asset_number"),
        UniqueConstraint("mcp_server_id", "id", name="uq_mcp_server_versions_asset_id"),
    )


class McpCredentialSlotRow(Base):
    __tablename__ = "mcp_version_credential_slots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mcp_server_versions.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(63), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    payload_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))

    __table_args__ = (
        UniqueConstraint("mcp_server_version_id", "name", name="uq_mcp_credential_slots_version_name"),
        UniqueConstraint("mcp_server_version_id", "id", name="uq_mcp_credential_slots_version_id"),
    )
