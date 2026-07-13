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

_CREATE_BINDING_PUBLISHED_FUNCTION = """
CREATE OR REPLACE FUNCTION ensure_system_binding_published_version()
RETURNS trigger AS $$
DECLARE
    version_status text;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'project_system_agent_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM agent_versions
            WHERE id = NEW.agent_version_id AND agent_id = NEW.system_agent_id
            FOR UPDATE;
        WHEN 'project_system_skill_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM skill_versions
            WHERE id = NEW.skill_version_id AND skill_id = NEW.system_skill_id
            FOR UPDATE;
        WHEN 'project_system_mcp_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM mcp_server_versions
            WHERE id = NEW.mcp_server_version_id
              AND mcp_server_id = NEW.system_mcp_server_id
            FOR UPDATE;
        ELSE
            RAISE EXCEPTION 'unsupported system binding table';
    END CASE;
    IF version_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'system binding requires published version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_BOUND_VERSION_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_bound_published_version_downgrade()
RETURNS trigger AS $$
DECLARE
    is_bound boolean;
BEGIN
    IF OLD.workflow_status = 'published'
       AND NEW.workflow_status IS DISTINCT FROM 'published' THEN
        CASE TG_TABLE_NAME
            WHEN 'agent_versions' THEN
                SELECT EXISTS (
                    SELECT 1 FROM project_system_agent_bindings
                    WHERE agent_version_id = OLD.id
                ) INTO is_bound;
            WHEN 'skill_versions' THEN
                SELECT EXISTS (
                    SELECT 1 FROM project_system_skill_bindings
                    WHERE skill_version_id = OLD.id
                ) INTO is_bound;
            WHEN 'mcp_server_versions' THEN
                SELECT EXISTS (
                    SELECT 1 FROM project_system_mcp_bindings
                    WHERE mcp_server_version_id = OLD.id
                ) INTO is_bound;
            ELSE
                is_bound := false;
        END CASE;
        IF is_bound THEN
            RAISE EXCEPTION 'bound published version cannot change workflow status'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_CHILD_IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_published_version_child_mutation()
RETURNS trigger AS $$
DECLARE
    parent_version_id uuid;
    parent_status text;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'skill_version_files' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
            SELECT workflow_status INTO parent_status
            FROM skill_versions WHERE id = parent_version_id FOR UPDATE;
        WHEN 'agent_version_skill_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT workflow_status INTO parent_status
            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;
        WHEN 'agent_version_mcp_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT workflow_status INTO parent_status
            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;
        WHEN 'mcp_version_credential_slots' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;
            SELECT workflow_status INTO parent_status
            FROM mcp_server_versions WHERE id = parent_version_id FOR UPDATE;
        ELSE
            RAISE EXCEPTION 'unsupported version child table';
    END CASE;
    IF parent_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'published version child rows are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_VERSION_STATE_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_shared_asset_version_state_transition()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'credential_versions' THEN
        IF NEW.status = OLD.status
           OR (OLD.status = 'active' AND NEW.status IN ('retired', 'revoked'))
           OR (OLD.status = 'retired' AND NEW.status = 'revoked') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid credential version status transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.workflow_status = OLD.workflow_status
       OR (OLD.workflow_status = 'draft'
           AND NEW.workflow_status IN ('pending_approval', 'published'))
       OR (OLD.workflow_status = 'pending_approval'
           AND NEW.workflow_status IN ('published', 'rejected')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid shared asset version workflow transition'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""

_TRIGGER_DDL = (
    _CREATE_IMMUTABLE_FUNCTION,
    _CREATE_GENERATION_FUNCTION,
    _CREATE_BINDING_PUBLISHED_FUNCTION,
    _CREATE_BOUND_VERSION_FUNCTION,
    _CREATE_CHILD_IMMUTABILITY_FUNCTION,
    _CREATE_VERSION_STATE_FUNCTION,
    "CREATE TRIGGER trg_agent_versions_immutable BEFORE UPDATE ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_credential_versions_immutable BEFORE UPDATE ON credential_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_skill_refs_immutable BEFORE UPDATE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_version_mcp_refs_immutable BEFORE UPDATE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_credential_slots_immutable BEFORE UPDATE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_bindings_published BEFORE INSERT OR UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_published_version()",
    "CREATE TRIGGER trg_skill_bindings_published BEFORE INSERT OR UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_published_version()",
    "CREATE TRIGGER trg_mcp_bindings_published BEFORE INSERT OR UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_published_version()",
    "CREATE TRIGGER trg_agent_versions_bound_published BEFORE UPDATE OF workflow_status ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_published_version_downgrade()",
    "CREATE TRIGGER trg_skill_versions_bound_published BEFORE UPDATE OF workflow_status ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_published_version_downgrade()",
    "CREATE TRIGGER trg_mcp_server_versions_bound_published BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_published_version_downgrade()",
    "CREATE TRIGGER trg_skill_version_files_child_immutable BEFORE INSERT OR DELETE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation()",
    "CREATE TRIGGER trg_agent_version_skill_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation()",
    "CREATE TRIGGER trg_agent_version_mcp_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation()",
    "CREATE TRIGGER trg_mcp_credential_slots_child_immutable BEFORE INSERT OR DELETE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation()",
    "CREATE TRIGGER trg_agent_versions_state_transition BEFORE UPDATE OF workflow_status ON agent_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
    "CREATE TRIGGER trg_skill_versions_state_transition BEFORE UPDATE OF workflow_status ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
    "CREATE TRIGGER trg_mcp_server_versions_state_transition BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
    "CREATE TRIGGER trg_credential_versions_state_transition BEFORE UPDATE OF status ON credential_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
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

_TRIGGER_TABLES = frozenset(
    {
        "agent_versions",
        "agent_version_skill_refs",
        "agent_version_mcp_refs",
        "skill_versions",
        "skill_version_files",
        "mcp_server_versions",
        "mcp_version_credential_slots",
        "credential_versions",
        "credential_grants",
        "project_system_agent_bindings",
        "project_system_skill_bindings",
        "project_system_mcp_bindings",
        "agents",
        "skills",
        "mcp_servers",
        "credentials",
        "asset_catalog_state",
    }
)


def _install_shared_asset_triggers(_target, connection, **kwargs) -> None:
    created_tables = {table.name for table in kwargs.get("tables", ())}
    if not _TRIGGER_TABLES <= created_tables or connection.dialect.name != "postgresql":
        return
    for statement in _TRIGGER_DDL:
        connection.execute(DDL(statement))


event.listen(Base.metadata, "after_create", _install_shared_asset_triggers)
