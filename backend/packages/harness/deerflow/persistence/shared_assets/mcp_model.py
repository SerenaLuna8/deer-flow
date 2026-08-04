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


class McpToolDiscoveryAttemptRow(Base):
    """One durable MCP discovery request paired with one durable Job."""

    __tablename__ = "mcp_tool_discovery_attempts"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id",
            name="fk_mcp_tool_discovery_attempt_project",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_mcp_tool_discovery_attempt_requester",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    grant_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    result_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "project_id", "requested_by_user_id"],
            ["jobs.id", "jobs.project_id", "jobs.owner_user_id"],
            name="fk_mcp_tool_discovery_attempt_job",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["mcp_server_id", "mcp_server_version_id"],
            ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"],
            name="fk_mcp_tool_discovery_attempt_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "trigger IN ('auto', 'manual')",
            name="ck_mcp_tool_discovery_attempt_trigger",
        ),
        CheckConstraint(
            "payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_tool_discovery_attempt_checksum",
        ),
        CheckConstraint(
            "grant_digest ~ '^[0-9a-f]{64}$'",
            name="ck_mcp_tool_discovery_attempt_grant_digest",
        ),
        CheckConstraint(
            "result_status IS NULL OR result_status IN ('succeeded', 'failed', 'cancelled')",
            name="ck_mcp_tool_discovery_attempt_result_status",
        ),
        CheckConstraint(
            "(result_status IS NULL AND public_error_code IS NULL) "
            "OR (result_status = 'succeeded' AND public_error_code IS NULL) "
            "OR (result_status = 'cancelled' AND public_error_code IS NULL) "
            "OR (result_status = 'failed' AND public_error_code IN ('mcp_discovery_unavailable', 'mcp_catalog_invalid'))",
            name="ck_mcp_tool_discovery_attempt_result",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_mcp_tool_discovery_attempt_revision",
        ),
        Index(
            "ix_mcp_tool_discovery_attempts_version",
            "project_id",
            "mcp_server_id",
            "mcp_server_version_id",
            requested_at.desc(),
            "job_id",
        ),
        Index(
            "ix_mcp_tool_discovery_attempts_closure",
            "project_id",
            "mcp_server_id",
            "mcp_server_version_id",
            "payload_checksum",
            "grant_digest",
            requested_at.desc(),
        ),
    )


class ProjectMcpToolInventoryRow(Base):
    """Mutable, project-scoped observation of a Worker's last MCP discovery.

    This cache is display-only. Runtime execution always performs a fresh
    discovery and never treats these rows as tool authority.
    """

    __tablename__ = "project_mcp_tool_inventories"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "projects.id",
            name="fk_project_mcp_tool_inventory_project",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
    )
    mcp_server_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    attempt_payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    attempt_grant_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    attempt_status: Mapped[str] = mapped_column(String(16), nullable=False)
    public_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tools: Mapped[list[dict[str, str]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    tools_payload_checksum: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    tools_grant_digest: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["mcp_server_id", "mcp_server_version_id"],
            ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"],
            name="fk_project_mcp_tool_inventory_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "attempt_status IN ('ready', 'failed')",
            name="ck_project_mcp_tool_inventory_attempt_status",
        ),
        CheckConstraint(
            "attempt_payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_project_mcp_tool_inventory_attempt_checksum",
        ),
        CheckConstraint(
            "attempt_grant_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_mcp_tool_inventory_attempt_grant_digest",
        ),
        CheckConstraint(
            "tools_payload_checksum IS NULL OR tools_payload_checksum ~ '^[0-9a-f]{64}$'",
            name="ck_project_mcp_tool_inventory_tools_checksum",
        ),
        CheckConstraint(
            "tools_grant_digest IS NULL OR tools_grant_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_mcp_tool_inventory_tools_grant_digest",
        ),
        CheckConstraint(
            "(attempt_status = 'ready' AND public_error_code IS NULL) OR (attempt_status = 'failed' AND public_error_code IN ('mcp_discovery_unavailable', 'mcp_catalog_invalid'))",
            name="ck_project_mcp_tool_inventory_error",
        ),
        CheckConstraint(
            "(tools_payload_checksum IS NULL AND tools_grant_digest IS NULL AND last_success_at IS NULL) OR (tools_payload_checksum IS NOT NULL AND tools_grant_digest IS NOT NULL AND last_success_at IS NOT NULL)",
            name="ck_project_mcp_tool_inventory_success_shape",
        ),
        CheckConstraint(
            "jsonb_typeof(tools) = 'array' AND jsonb_array_length(tools) <= 128",
            name="ck_project_mcp_tool_inventory_tools_shape",
        ),
        CheckConstraint(
            "last_success_at IS NULL OR last_success_at <= last_attempt_at",
            name="ck_project_mcp_tool_inventory_time_order",
        ),
        CheckConstraint("revision >= 1", name="ck_project_mcp_tool_inventory_revision"),
        Index(
            "ix_project_mcp_tool_inventories_asset",
            "project_id",
            "mcp_server_id",
            "mcp_server_version_id",
        ),
    )
