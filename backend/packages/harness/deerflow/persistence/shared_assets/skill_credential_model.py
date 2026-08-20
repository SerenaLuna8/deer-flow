from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectSkillCredentialConfigRow(Base):
    """One optimistic, project-local credential configuration per Skill."""

    __tablename__ = "project_skill_credential_configs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "projects.id",
            name="fk_project_skill_credential_configs_project",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey(
            "skills.id",
            name="fk_project_skill_credential_configs_skill",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    skill_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_project_skill_credential_configs_creator",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    updated_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_project_skill_credential_configs_updater",
            ondelete="RESTRICT",
        ),
        nullable=False,
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
        PrimaryKeyConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            name="pk_project_skill_credential_configs",
        ),
        ForeignKeyConstraint(
            ["skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_skill_credential_configs_skill_version",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_skill_credential_configs_revision",
        ),
        UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "revision",
            name="uq_project_skill_credential_configs_revision",
        ),
        Index(
            "ix_project_skill_credential_configs_skill_version",
            skill_id,
            skill_version_id,
        ),
    )


class ProjectSkillCredentialBindingRow(Base):
    """Immutable binding revision; replacement revokes instead of deleting."""

    __tablename__ = "project_skill_credential_bindings"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        primary_key=True,
        default=uuid.uuid4,
    )
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_name: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    credential_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    config_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default="active",
    )
    created_by_user_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_project_skill_credential_bindings_creator",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    revoked_by_user_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "users.id",
            name="fk_project_skill_credential_bindings_revoker",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    # New migration columns stay physically last so upgraded and fresh
    # PostgreSQL catalogs have identical attribute order.
    source_env_field_name: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint(
            "id",
            name="pk_project_skill_credential_bindings",
        ),
        ForeignKeyConstraint(
            ["project_id", "skill_id", "skill_version_id"],
            [
                "project_skill_credential_configs.project_id",
                "project_skill_credential_configs.skill_id",
                "project_skill_credential_configs.skill_version_id",
            ],
            name="fk_project_skill_credential_bindings_config",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_skill_credential_bindings_skill_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["credential_id", "credential_version_id"],
            ["credential_versions.credential_id", "credential_versions.id"],
            name="fk_project_skill_credential_bindings_credential_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["project_id", "credential_id"],
            ["credentials.project_id", "credentials.id"],
            name="fk_project_skill_credential_bindings_project_credential",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_project_skill_credential_bindings_secret_name",
        ),
        CheckConstraint(
            "length(source_env_field_name) BETWEEN 1 AND 255",
            name="ck_project_skill_credential_bindings_source_env_field_name",
        ),
        CheckConstraint(
            "config_revision >= 1",
            name="ck_project_skill_credential_bindings_revision",
        ),
        CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_project_skill_credential_bindings_status",
        ),
        CheckConstraint(
            "(status = 'active' AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)",
            name="ck_project_skill_credential_bindings_revocation",
        ),
        UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "id",
            name="uq_project_skill_credential_bindings_scope_id",
        ),
        Index(
            "uq_project_skill_credential_bindings_active_name",
            project_id,
            skill_id,
            skill_version_id,
            secret_name,
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "ix_project_skill_credential_bindings_credential",
            credential_id,
            credential_version_id,
            status,
        ),
        Index(
            "ix_project_skill_credential_bindings_config",
            project_id,
            skill_id,
            skill_version_id,
        ),
        Index(
            "ix_project_skill_credential_bindings_skill_version",
            skill_id,
            skill_version_id,
        ),
        Index(
            "ix_project_skill_credential_bindings_project_credential",
            project_id,
            credential_id,
        ),
    )
