from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    LargeBinary,
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


class ProjectSkillSecretStateRow(Base):
    """Write-only state for one Project and exact Skill Version secret."""

    __tablename__ = "project_skill_secret_states"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_name: Mapped[str] = mapped_column(String(255), nullable=False)
    optional: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    current_generation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    updated_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
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
            "secret_name",
            name="pk_project_skill_secret_states",
        ),
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_skill_secret_states_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_skill_secret_states_skill_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["project_id", "skill_id", "skill_version_id", "secret_name", "current_generation_id"],
            [
                "project_skill_secret_generations.project_id",
                "project_skill_secret_generations.skill_id",
                "project_skill_secret_generations.skill_version_id",
                "project_skill_secret_generations.secret_name",
                "project_skill_secret_generations.id",
            ],
            name="fk_project_skill_secret_states_current_generation",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_project_skill_secret_states_updater",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_project_skill_secret_states_name",
        ),
        CheckConstraint(
            "revision >= 0",
            name="ck_project_skill_secret_states_revision",
        ),
    )


class ProjectSkillSecretGenerationRow(Base):
    __tablename__ = "project_skill_secret_generations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_name: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_skill_secret_generations_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_skill_secret_generations_skill_version",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_project_skill_secret_generations_creator",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "secret_name",
            "id",
            name="uq_project_skill_secret_generations_owner_id",
        ),
        UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "secret_name",
            "revision",
            name="uq_project_skill_secret_generations_revision",
        ),
        CheckConstraint(
            "secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'",
            name="ck_project_skill_secret_generations_name",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_skill_secret_generations_revision",
        ),
        CheckConstraint(
            "octet_length(nonce) = 12 AND octet_length(ciphertext) >= 16",
            name="ck_project_skill_secret_generations_envelope",
        ),
        CheckConstraint(
            "envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_skill_secret_generations_digest",
        ),
    )


class ProjectSkillSecretTombstoneRow(Base):
    __tablename__ = "project_skill_secret_tombstones"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    secret_name: Mapped[str] = mapped_column(String(255), nullable=False)
    destroyed_generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    envelope_digest: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(24), nullable=False)
    destroyed_by_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    destroyed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_project_skill_secret_tombstones_project",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["destroyed_by_user_id"],
            ["users.id"],
            name="fk_project_skill_secret_tombstones_actor",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "project_id",
            "skill_id",
            "skill_version_id",
            "secret_name",
            "destroyed_generation_id",
            name="uq_project_skill_secret_tombstones_generation",
        ),
        CheckConstraint(
            "revision >= 1",
            name="ck_project_skill_secret_tombstones_revision",
        ),
        CheckConstraint(
            "envelope_digest ~ '^[0-9a-f]{64}$'",
            name="ck_project_skill_secret_tombstones_digest",
        ),
        CheckConstraint(
            "reason IN ('replace', 'clear', 'version_purge', 'skill_delete')",
            name="ck_project_skill_secret_tombstones_reason",
        ),
    )


__all__ = [
    "ProjectSkillSecretGenerationRow",
    "ProjectSkillSecretStateRow",
    "ProjectSkillSecretTombstoneRow",
]
