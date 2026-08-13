"""ORM model for thread metadata."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column, synonym

from deerflow.persistence.base import Base


class ThreadMetaRow(Base):
    __tablename__ = "threads_meta"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assistant_id: Mapped[str | None] = mapped_column(String(128), index=True)
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    user_id = synonym("owner_user_id")
    display_name: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(20), default="idle")
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    agent_asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    agent_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Scheduling metadata for idle Memory sealing; not Memory content, so an
    # account Memory reset intentionally leaves it in place.
    memory_sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint_delete_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_requested", server_default="not_requested")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    thread_kind: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="chat",
        server_default="chat",
    )

    __table_args__ = (
        UniqueConstraint("project_id", "owner_user_id", "thread_id", name="uq_threads_meta_private_scope"),
        ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_threads_meta_project", ondelete="RESTRICT"),
        ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_threads_meta_owner", ondelete="RESTRICT"),
        ForeignKeyConstraint(
            ["project_id", "owner_user_id"],
            ["project_memberships.project_id", "project_memberships.user_id"],
            name="fk_threads_meta_project_membership",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["agent_asset_id", "agent_scope"],
            ["agents.id", "agents.scope"],
            name="fk_threads_meta_agent_asset",
            ondelete="RESTRICT",
        ),
        CheckConstraint("agent_scope IN ('system', 'project')", name="ck_threads_meta_agent_scope"),
        CheckConstraint(
            "thread_kind IN ('chat', 'skill_builder')",
            name="ck_threads_meta_kind",
        ),
        CheckConstraint(
            "checkpoint_delete_status IN ('not_requested', 'pending', 'complete', 'retry_required')",
            name="ck_threads_meta_checkpoint_delete_status",
        ),
        CheckConstraint("version >= 1", name="ck_threads_meta_version"),
    )
