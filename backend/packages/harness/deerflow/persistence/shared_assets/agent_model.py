from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
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


class AgentRow(Base):
    __tablename__ = "agents"

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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        onupdate=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_agents_scope_project",
        ),
        CheckConstraint("status IN ('active', 'archived', 'suspended')", name="ck_agents_status"),
        CheckConstraint("version >= 1", name="ck_agents_version"),
        UniqueConstraint("id", "scope", name="uq_agents_id_scope"),
        UniqueConstraint("project_id", "id", name="uq_agents_project_id_id"),
        UniqueConstraint("source_key", name="uq_agents_source_key"),
        ForeignKeyConstraint(
            ["id", "current_published_version_id"],
            ["agent_versions.agent_id", "agent_versions.id"],
            name="fk_agents_current_published_version",
            use_alter=True,
        ),
        Index(
            "uq_agents_system_slug",
            func.lower(slug),
            unique=True,
            postgresql_where=text("scope = 'system'"),
        ),
        Index(
            "uq_agents_project_slug",
            project_id,
            func.lower(slug),
            unique=True,
            postgresql_where=text("scope = 'project'"),
        ),
    )


class AgentVersionRow(Base):
    __tablename__ = "agent_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    workflow_status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft", server_default="draft")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    soul: Mapped[str] = mapped_column(Text, nullable=False)
    model_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    model_settings: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    tool_groups: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_versions.id", ondelete="RESTRICT"), nullable=True)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    # These fields were introduced after the original payload columns. Keep
    # their declaration order aligned with the consolidated physical catalog.
    agents_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    identity: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    user_context: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    payload_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default=text("1"))

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_agent_versions_number"),
        CheckConstraint(
            "workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')",
            name="ck_agent_versions_workflow_status",
        ),
        CheckConstraint(
            "payload_schema_version IN (1, 2, 3)",
            name="ck_agent_versions_payload_schema_version",
        ),
        CheckConstraint(
            """
            jsonb_typeof(model_settings) = 'object'
            AND (
                payload_schema_version = 3
                OR model_settings = '{}'::jsonb
            )
            AND model_settings - 'temperature' - 'max_tokens'
                - 'thinking_enabled' - 'reasoning_effort' = '{}'::jsonb
            AND (
                NOT (model_settings ? 'temperature')
                OR (
                    jsonb_typeof(model_settings->'temperature') = 'number'
                    AND (model_settings->>'temperature')::numeric BETWEEN 0 AND 2
                )
            )
            AND (
                NOT (model_settings ? 'max_tokens')
                OR (
                    jsonb_typeof(model_settings->'max_tokens') = 'number'
                    AND (model_settings->>'max_tokens')::numeric
                        = trunc((model_settings->>'max_tokens')::numeric)
                    AND (model_settings->>'max_tokens')::numeric
                        BETWEEN 1 AND 200000
                )
            )
            AND (
                NOT (model_settings ? 'thinking_enabled')
                OR jsonb_typeof(model_settings->'thinking_enabled') = 'boolean'
            )
            AND (
                NOT (model_settings ? 'reasoning_effort')
                OR (
                    jsonb_typeof(model_settings->'reasoning_effort') = 'string'
                    AND model_settings->>'reasoning_effort'
                        IN ('low', 'medium', 'high')
                )
            )
            """,
            name="ck_agent_versions_model_settings",
        ),
        CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_agent_versions_checksum"),
        UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_asset_number"),
        UniqueConstraint("agent_id", "id", name="uq_agent_versions_asset_id"),
    )


class AgentVersionSkillRefRow(Base):
    __tablename__ = "agent_version_skill_refs"

    agent_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    sort_order: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))

    __table_args__ = (
        ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        CheckConstraint("sort_order >= 0", name="ck_agent_version_skill_refs_sort_order"),
    )


class AgentVersionMcpRefRow(Base):
    __tablename__ = "agent_version_mcp_refs"

    agent_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    sort_order: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default=text("0"))

    __table_args__ = (
        ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        ForeignKeyConstraint(["mcp_server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        CheckConstraint("sort_order >= 0", name="ck_agent_version_mcp_refs_sort_order"),
    )
