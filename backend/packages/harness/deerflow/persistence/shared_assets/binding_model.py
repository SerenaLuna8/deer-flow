from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DDL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    SmallInteger,
    String,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from deerflow.persistence.base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectSystemAgentBindingRow(Base):
    __tablename__ = "project_system_agent_bindings"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True)
    system_agent_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    system_asset_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="system", server_default="system")
    agent_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    updated_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["system_agent_id", "system_asset_scope"],
            ["agents.id", "agents.scope"],
            name="fk_project_system_agent_bindings_system_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["system_agent_id", "agent_version_id"],
            ["agent_versions.agent_id", "agent_versions.id"],
            name="fk_project_system_agent_bindings_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_agent_bindings_system_scope"),
        CheckConstraint("version >= 1", name="ck_project_system_agent_bindings_version"),
    )


class ProjectSystemSkillBindingRow(Base):
    __tablename__ = "project_system_skill_bindings"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True)
    system_skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    system_asset_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="system", server_default="system")
    skill_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    updated_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["system_skill_id", "system_asset_scope"],
            ["skills.id", "skills.scope"],
            name="fk_project_system_skill_bindings_system_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["system_skill_id", "skill_version_id"],
            ["skill_versions.skill_id", "skill_versions.id"],
            name="fk_project_system_skill_bindings_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_skill_bindings_system_scope"),
        CheckConstraint("version >= 1", name="ck_project_system_skill_bindings_version"),
    )


class ProjectSystemMcpBindingRow(Base):
    __tablename__ = "project_system_mcp_bindings"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True)
    system_mcp_server_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    system_asset_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="system", server_default="system")
    mcp_server_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    created_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    updated_by_user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["system_mcp_server_id", "system_asset_scope"],
            ["mcp_servers.id", "mcp_servers.scope"],
            name="fk_project_system_mcp_bindings_system_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["system_mcp_server_id", "mcp_server_version_id"],
            ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"],
            name="fk_project_system_mcp_bindings_version",
            ondelete="RESTRICT",
        ),
        CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_mcp_bindings_system_scope"),
        CheckConstraint("version >= 1", name="ck_project_system_mcp_bindings_version"),
    )


class AssetCatalogStateRow(Base):
    __tablename__ = "asset_catalog_state"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1, server_default=text("1"))
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default=text("1"))
    cutover_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_asset_catalog_state_singleton"),
        CheckConstraint("generation >= 1", name="ck_asset_catalog_state_generation"),
    )


_CREATE_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id'
    ]::text[]) THEN
        RAISE EXCEPTION 'shared asset version payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_GENERATION_FUNCTION = """
CREATE OR REPLACE FUNCTION bump_asset_catalog_generation()
RETURNS trigger AS $$
BEGIN
    INSERT INTO asset_catalog_state (id, generation, updated_at)
    VALUES (1, 1, now())
    ON CONFLICT (id) DO UPDATE
      SET generation = asset_catalog_state.generation + 1,
          updated_at = now();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_TRIGGER_DDL = (
    _CREATE_IMMUTABLE_FUNCTION,
    _CREATE_GENERATION_FUNCTION,
    "CREATE TRIGGER trg_agent_versions_immutable BEFORE UPDATE ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_credential_versions_immutable BEFORE UPDATE ON credential_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_skill_refs_immutable BEFORE UPDATE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_mcp_refs_immutable BEFORE UPDATE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_credential_slots_immutable BEFORE UPDATE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, current_published_version_id ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_published_version_id ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_agent_versions_generation AFTER UPDATE OF workflow_status ON agent_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_versions_generation AFTER UPDATE OF workflow_status ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_server_versions_generation AFTER UPDATE OF workflow_status ON mcp_server_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credentials_generation AFTER UPDATE OF status, current_version_id ON credentials FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credential_versions_generation AFTER UPDATE OF status ON credential_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_agent_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_agent_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_skill_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_mcp_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_credential_grants_generation AFTER INSERT OR UPDATE OR DELETE ON credential_grants FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
)

for statement in _TRIGGER_DDL:
    event.listen(Base.metadata, "after_create", DDL(statement).execute_if(dialect="postgresql"))
