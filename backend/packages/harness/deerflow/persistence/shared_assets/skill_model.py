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
    LargeBinary,
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


class SkillRow(Base):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint(
            "(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)",
            name="ck_skills_scope_project",
        ),
        CheckConstraint("status IN ('active', 'archived', 'suspended')", name="ck_skills_status"),
        CheckConstraint("revision >= 1", name="ck_skills_revision"),
        UniqueConstraint("id", "scope", name="uq_skills_id_scope"),
        UniqueConstraint("project_id", "id", name="uq_skills_project_id_id"),
        UniqueConstraint("source_key", name="uq_skills_source_key"),
        ForeignKeyConstraint(
            ["id", "current_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_skills_current_version",
            use_alter=True,
        ),
        Index("uq_skills_system_slug", func.lower(slug), unique=True, postgresql_where=text("scope = 'system'")),
        Index(
            "uq_skills_project_slug",
            project_id,
            func.lower(slug),
            unique=True,
            postgresql_where=text(
                "scope = 'project' AND status != 'archived'",
            ),
        ),
        Index(
            "uq_skills_project_display_name",
            project_id,
            func.lower(display_name),
            unique=True,
            postgresql_where=text(
                "scope = 'project' AND status != 'archived'",
            ),
        ),
        Index(
            "ix_skills_archived_purge",
            project_id,
            id,
            postgresql_where=text(
                "scope = 'project' AND status = 'archived'",
            ),
        ),
    )


class SkillVersionRow(Base):
    __tablename__ = "skill_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    skill_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("skills.id", ondelete="RESTRICT"), nullable=False)
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    frontmatter: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    compatibility: Mapped[str | None] = mapped_column(String(255), nullable=True)
    secret_requirements: Mapped[list[object]] = mapped_column(JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb"))
    scan_decision: Mapped[str] = mapped_column(String(24), nullable=False)
    scan_summary: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb"))
    supersedes_version_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("skill_versions.id", ondelete="RESTRICT"), nullable=True)
    payload_checksum: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    files_sealed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    revocation_reason_code: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (
        CheckConstraint("version_number >= 1", name="ck_skill_versions_number"),
        CheckConstraint("scan_decision IN ('allow', 'warn', 'block')", name="ck_skill_versions_scan_decision"),
        CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_skill_versions_checksum"),
        CheckConstraint(
            "file_count BETWEEN 1 AND 16384",
            name="ck_skill_versions_file_count",
        ),
        CheckConstraint(
            "content_size_bytes BETWEEN 0 AND 104857600",
            name="ck_skill_versions_content_size",
        ),
        CheckConstraint(
            "files_sealed IN (true, false)",
            name="ck_skill_versions_files_sealed",
        ),
        CheckConstraint(
            "(revoked_at IS NULL) = (revoked_by_user_id IS NULL) AND (revoked_at IS NULL) = (revocation_reason_code IS NULL)",
            name="ck_skill_versions_revocation",
        ),
        CheckConstraint(
            "revocation_reason_code IS NULL OR revocation_reason_code IN ('security', 'policy', 'integrity')",
            name="ck_skill_versions_revocation_reason",
        ),
        UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_asset_number"),
        UniqueConstraint("skill_id", "id", name="uq_skill_versions_asset_id"),
        UniqueConstraint(
            "skill_id",
            "id",
            "payload_checksum",
            "file_count",
            "content_size_bytes",
            name="uq_skill_versions_runtime_exact",
        ),
    )


class SkillVersionFileRow(Base):
    __tablename__ = "skill_version_files"

    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        CheckConstraint("path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'", name="ck_skill_version_files_safe_path"),
        CheckConstraint("size_bytes >= 0 AND size_bytes <= 67108864", name="ck_skill_version_files_size"),
        CheckConstraint("size_bytes = octet_length(content)", name="ck_skill_version_files_content_size"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_skill_version_files_sha256"),
        Index(
            "ix_skill_version_files_version_path_c",
            "skill_version_id",
            text('path COLLATE "C"'),
        ),
    )
