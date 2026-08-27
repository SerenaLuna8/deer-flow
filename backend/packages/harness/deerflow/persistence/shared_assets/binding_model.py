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
        CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_agent_bindings_system_scope"),
        CheckConstraint("version >= 1", name="ck_project_system_agent_bindings_version"),
    )


class ProjectSystemSkillBindingRow(Base):
    __tablename__ = "project_system_skill_bindings"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"), primary_key=True)
    system_skill_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    system_asset_scope: Mapped[str] = mapped_column(String(16), nullable=False, default="system", server_default="system")
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
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now, server_default=text("now()"))

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_asset_catalog_state_singleton"),
        CheckConstraint("generation >= 1", name="ck_asset_catalog_state_generation"),
    )


class SystemAssetUpgradeAuditRow(Base):
    __tablename__ = "system_asset_upgrade_audit"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    asset_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    before_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    after_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    package_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    operator_identity: Mapped[str] = mapped_column(String(255), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_now,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint("asset_kind IN ('agent', 'skill', 'mcp')", name="ck_system_asset_upgrade_audit_kind"),
        CheckConstraint("before_checksum ~ '^[0-9a-f]{64}$'", name="ck_system_asset_upgrade_audit_before_checksum"),
        CheckConstraint("after_checksum ~ '^[0-9a-f]{64}$'", name="ck_system_asset_upgrade_audit_after_checksum"),
        CheckConstraint("package_digest ~ '^[0-9a-f]{64}$'", name="ck_system_asset_upgrade_audit_package_digest"),
    )


_CREATE_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
DECLARE
    asset_scope text;
BEGIN
    IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
       AND TG_TABLE_NAME = 'mcp_server_versions' THEN
        SELECT scope INTO asset_scope FROM mcp_servers
        WHERE id = NEW.mcp_server_id;
        IF asset_scope = 'system' THEN
            RETURN NEW;
        END IF;
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code', 'files_sealed'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code', 'files_sealed'
    ]::text[]) THEN
        RAISE EXCEPTION 'shared asset version payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_SKILL_VERSION_SEAL_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_skill_version_files_seal_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.files_sealed IS NOT DISTINCT FROM OLD.files_sealed
       OR (OLD.files_sealed IS FALSE AND NEW.files_sealed IS TRUE) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid Skill version file seal transition'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql
"""

_CREATE_SKILL_VERSION_FACTS_FUNCTION = """
CREATE OR REPLACE FUNCTION verify_skill_version_file_facts()
RETURNS trigger AS $$
DECLARE
    current_version skill_versions%ROWTYPE;
    actual_file_count bigint;
    actual_content_size bigint;
BEGIN
    SELECT * INTO current_version
    FROM skill_versions
    WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF current_version.files_sealed IS NOT TRUE THEN
        RAISE EXCEPTION 'Skill version files must be sealed before commit'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT count(*), coalesce(sum(size_bytes), 0)
    INTO actual_file_count, actual_content_size
    FROM skill_version_files
    WHERE skill_version_id = current_version.id;
    IF actual_file_count IS DISTINCT FROM current_version.file_count
       OR actual_content_size IS DISTINCT FROM current_version.content_size_bytes THEN
        RAISE EXCEPTION 'Skill version file facts do not match persisted files'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
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

_CREATE_BINDING_ELIGIBILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION ensure_system_binding_eligible_version()
RETURNS trigger AS $$
DECLARE
    version_revoked_at timestamp with time zone;
    current_id uuid;
    asset_status text;
    version_status text;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'project_system_agent_bindings' THEN
            SELECT definition_id, status INTO current_id, asset_status
            FROM agents
            WHERE id = NEW.system_agent_id AND scope = 'system'
            FOR UPDATE;
        WHEN 'project_system_skill_bindings' THEN
            SELECT current_version_id, status INTO current_id, asset_status
            FROM skills
            WHERE id = NEW.system_skill_id AND scope = 'system'
            FOR UPDATE;
            SELECT revoked_at INTO version_revoked_at
            FROM skill_versions
            WHERE id = current_id AND skill_id = NEW.system_skill_id
            FOR UPDATE;
            IF TG_OP = 'UPDATE'
               AND OLD.enabled IS TRUE
               AND NEW.enabled IS FALSE
               AND OLD.system_skill_id = NEW.system_skill_id THEN
                RETURN NEW;
            END IF;
        WHEN 'project_system_mcp_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM mcp_server_versions
            WHERE id = NEW.mcp_server_version_id
              AND mcp_server_id = NEW.system_mcp_server_id
            FOR UPDATE;
        ELSE
            RAISE EXCEPTION 'unsupported system binding table';
    END CASE;
    IF TG_TABLE_NAME = 'project_system_mcp_bindings'
       AND version_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'system MCP binding requires a published version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME IN ('project_system_agent_bindings', 'project_system_skill_bindings')
       AND (current_id IS NULL OR asset_status IS DISTINCT FROM 'active'
       OR (TG_TABLE_NAME = 'project_system_skill_bindings'
           AND version_revoked_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'system binding requires an eligible definition or Current Version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_SKILL_REVOCATION_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_system_skill_version_revocation()
RETURNS trigger AS $$
DECLARE
    asset_scope text;
    asset_project_id uuid;
    current_id uuid;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.revoked_at IS NOT NULL
           OR NEW.revoked_by_user_id IS NOT NULL
           OR NEW.revocation_reason_code IS NOT NULL THEN
            RAISE EXCEPTION 'skill version must be created unrevoked'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.revoked_at IS NOT DISTINCT FROM OLD.revoked_at
       AND NEW.revoked_by_user_id IS NOT DISTINCT FROM OLD.revoked_by_user_id
       AND NEW.revocation_reason_code IS NOT DISTINCT FROM OLD.revocation_reason_code THEN
        RETURN NEW;
    END IF;

    IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
       AND OLD.revoked_at IS NOT NULL
       AND NEW.revoked_at IS NULL
       AND NEW.revoked_by_user_id IS NULL
       AND NEW.revocation_reason_code IS NULL THEN
        SELECT scope, project_id, current_version_id
        INTO asset_scope, asset_project_id, current_id
        FROM skills
        WHERE id = NEW.skill_id
        FOR UPDATE;
        IF asset_scope = 'system'
           AND asset_project_id IS NULL
           AND current_id = NEW.id
           AND NEW.version_number = 1 THEN
            RETURN NEW;
        END IF;
    END IF;
    IF OLD.revoked_at IS NOT NULL
       OR OLD.revoked_by_user_id IS NOT NULL
       OR OLD.revocation_reason_code IS NOT NULL
       OR NEW.revoked_at IS NULL
       OR NEW.revoked_by_user_id IS NULL
       OR NEW.revocation_reason_code IS NULL
       OR NEW.revocation_reason_code NOT IN ('security', 'policy', 'integrity') THEN
        RAISE EXCEPTION 'system skill version revocation is irreversible'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT scope, project_id, current_version_id
    INTO asset_scope, asset_project_id, current_id
    FROM skills
    WHERE id = NEW.skill_id
    FOR UPDATE;
    IF asset_scope IS DISTINCT FROM 'system'
       OR asset_project_id IS NOT NULL
       OR current_id IS DISTINCT FROM NEW.id
       OR NEW.version_number != 1 THEN
        RAISE EXCEPTION 'only a System Skill Current v1 can be revoked'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_BOUND_VERSION_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_bound_mcp_published_version_downgrade()
RETURNS trigger AS $$
DECLARE
    is_bound boolean;
BEGIN
    IF OLD.workflow_status = 'published'
       AND NEW.workflow_status IS DISTINCT FROM 'published' THEN
        SELECT EXISTS (
            SELECT 1 FROM project_system_mcp_bindings
            WHERE mcp_server_version_id = OLD.id
        ) INTO is_bound;
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
CREATE OR REPLACE FUNCTION prevent_asset_version_child_mutation()
RETURNS trigger AS $$
DECLARE
    parent_version_id uuid;
    parent_status text;
    parent_scope text;
    parent_project_id uuid;
    parent_asset_id uuid;
    parent_files_sealed boolean;
    purge_allowed boolean := false;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'skill_version_files' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
            SELECT asset.scope, asset.project_id, asset.id, version.files_sealed
            INTO parent_scope, parent_project_id, parent_asset_id,
                 parent_files_sealed
            FROM skill_versions version
            JOIN skills asset ON asset.id = version.skill_id
            WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM skill_versions version
                    JOIN skills asset ON asset.id = version.skill_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.skill_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                      AND NOT EXISTS (
                          SELECT 1
                          FROM run_skill_version_refs pinned
                          WHERE pinned.skill_version_id = version.id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM run_asset_versions legacy
                          WHERE legacy.asset_kind = 'skill'
                            AND legacy.asset_scope = 'project'
                            AND legacy.asset_id = asset.id
                            AND legacy.version_id = version.id
                            AND legacy.snapshot_schema_version IN (2, 3)
                      )
                ) INTO purge_allowed;
            END IF;
        WHEN 'mcp_version_secret_slots' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;
            SELECT asset.scope, asset.project_id, asset.id,
                   version.workflow_status
            INTO parent_scope, parent_project_id, parent_asset_id, parent_status
            FROM mcp_server_versions version
            JOIN mcp_servers asset ON asset.id = version.mcp_server_id
            WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM mcp_server_versions version
                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.mcp_server_version_id
                      AND asset.scope = 'project'
                      AND (
                          (
                              project.status = 'pending_deletion'
                              AND project.deletion_effective_at IS NOT NULL
                              AND project.deletion_effective_at <= now()
                          )
                          OR (
                              project.status = 'active'
                              AND project.is_suspended IS FALSE
                              AND asset.status = 'archived'
                              AND asset.current_published_version_id IS NULL
                              AND current_setting(
                                  'deerflow.mcp_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) INTO purge_allowed;
            END IF;
        ELSE
            RAISE EXCEPTION 'unsupported version child table';
    END CASE;
    IF TG_TABLE_NAME = 'skill_version_files' THEN
        IF TG_OP = 'INSERT'
           AND parent_files_sealed IS FALSE
           AND current_setting('deerflow.asset_version_assembly', true)
               = parent_version_id::text THEN
            RETURN NEW;
        END IF;
        IF TG_OP = 'DELETE' AND purge_allowed THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Skill version files are immutable outside initial assembly'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME = 'mcp_version_secret_slots' THEN
        IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
           AND parent_scope = 'system' THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
        IF current_setting('deerflow.asset_version_assembly', true)
           = parent_version_id::text THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
        IF TG_OP = 'DELETE' AND purge_allowed THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'MCP version child rows are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' AND purge_allowed THEN
        RETURN OLD;
    END IF;
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

_CREATE_AGENT_DEFINITION_MUTATION_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_agent_definition_mutation()
RETURNS trigger AS $$
DECLARE
    target_agent_id uuid;
    target_scope text;
    target_project_id uuid;
    project_status text;
    project_deletion_effective_at timestamptz;
    referenced_scope text;
    referenced_project_id uuid;
    referenced_status text;
BEGIN
    IF TG_TABLE_NAME = 'agents' THEN
        IF OLD.scope = 'system'
           AND current_setting('deerflow.system_asset_upgrade', true)
               IS NOT DISTINCT FROM 'on' THEN
            IF NEW.definition_id IS DISTINCT FROM OLD.definition_id
               OR NEW.revision != OLD.revision + 1 THEN
                RAISE EXCEPTION 'System Agent definition identity is immutable and revision must advance once'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.scope IS DISTINCT FROM 'project'
           OR current_setting(
               'deerflow.agent_definition_mutation_id', true
           ) IS DISTINCT FROM OLD.id::text
           OR NEW.definition_id IS NOT DISTINCT FROM OLD.definition_id
           OR NEW.revision != OLD.revision + 1 THEN
            RAISE EXCEPTION 'Project Agent definition mutation requires its transaction fence and one revision advance'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    target_agent_id := CASE WHEN TG_OP = 'DELETE'
        THEN OLD.agent_id ELSE NEW.agent_id END;
    SELECT agent.scope, agent.project_id, project.status,
           project.deletion_effective_at
    INTO target_scope, target_project_id, project_status,
         project_deletion_effective_at
    FROM agents agent
    LEFT JOIN projects project ON project.id = agent.project_id
    WHERE agent.id = target_agent_id
    FOR UPDATE OF agent;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Agent definition reference requires an Agent'
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NOT (
        (target_scope = 'system'
         AND current_setting('deerflow.system_asset_upgrade', true)
             IS NOT DISTINCT FROM 'on')
        OR
        (target_scope = 'project'
         AND current_setting(
             'deerflow.agent_definition_mutation_id', true
         ) IS NOT DISTINCT FROM target_agent_id::text)
        OR
        (target_scope = 'project'
         AND project_status = 'pending_deletion'
         AND project_deletion_effective_at IS NOT NULL
         AND project_deletion_effective_at <= now())
        OR
        (target_scope = 'project'
         AND current_setting(
             'deerflow.agent_hard_delete_asset_id', true
         ) IS NOT DISTINCT FROM target_agent_id::text)
    ) THEN
        RAISE EXCEPTION 'Agent definition reference mutation requires its transaction fence'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF NEW.agent_id IS DISTINCT FROM target_agent_id THEN
        RAISE EXCEPTION 'Agent definition reference cannot move between Agents'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF TG_TABLE_NAME = 'agent_skill_refs' THEN
        SELECT skill.scope, skill.project_id, skill.status
        INTO referenced_scope, referenced_project_id, referenced_status
        FROM skills skill
        WHERE skill.id = NEW.skill_asset_id
          AND skill.scope = NEW.skill_asset_scope
        FOR SHARE;
    ELSIF TG_TABLE_NAME = 'agent_mcp_refs' THEN
        SELECT server.scope, server.project_id, server.status
        INTO referenced_scope, referenced_project_id, referenced_status
        FROM mcp_server_versions version
        JOIN mcp_servers server ON server.id = version.mcp_server_id
        WHERE version.id = NEW.mcp_server_version_id
        FOR SHARE OF version, server;
    ELSE
        RAISE EXCEPTION 'unsupported Agent definition reference table';
    END IF;
    IF NOT FOUND
       OR referenced_status = 'archived'
       OR (target_scope = 'system' AND referenced_scope != 'system')
       OR (
           target_scope = 'project'
           AND referenced_scope = 'project'
           AND referenced_project_id IS DISTINCT FROM target_project_id
       ) THEN
        RAISE EXCEPTION 'Agent definition reference crosses its governed scope'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_SKILL_ARCHIVE_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_skill_archive_transition()
RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'archived' AND NEW.status IS DISTINCT FROM 'archived' THEN
        RAISE EXCEPTION 'archived Skill status is terminal'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_CREATE_VERSION_STATE_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_shared_asset_version_state_transition()
RETURNS trigger AS $$
BEGIN
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
    _CREATE_SKILL_VERSION_SEAL_FUNCTION,
    _CREATE_SKILL_VERSION_FACTS_FUNCTION,
    _CREATE_GENERATION_FUNCTION,
    _CREATE_BINDING_ELIGIBILITY_FUNCTION,
    _CREATE_SKILL_REVOCATION_FUNCTION,
    _CREATE_BOUND_VERSION_FUNCTION,
    _CREATE_CHILD_IMMUTABILITY_FUNCTION,
    _CREATE_AGENT_DEFINITION_MUTATION_FUNCTION,
    _CREATE_SKILL_ARCHIVE_FUNCTION,
    _CREATE_VERSION_STATE_FUNCTION,
    (
        "CREATE TRIGGER trg_agents_definition_mutation BEFORE UPDATE OF "
        "definition_id, description, agents_instructions, soul, identity, "
        "user_context, model_ref, model_settings, tool_groups, "
        "payload_schema_version, payload_checksum ON agents FOR EACH ROW "
        "EXECUTE FUNCTION enforce_agent_definition_mutation()"
    ),
    "CREATE TRIGGER trg_agent_skill_refs_definition_mutation BEFORE INSERT OR UPDATE OR DELETE ON agent_skill_refs FOR EACH ROW EXECUTE FUNCTION enforce_agent_definition_mutation()",
    "CREATE TRIGGER trg_agent_mcp_refs_definition_mutation BEFORE INSERT OR UPDATE OR DELETE ON agent_mcp_refs FOR EACH ROW EXECUTE FUNCTION enforce_agent_definition_mutation()",
    "CREATE TRIGGER trg_skills_archive_terminal BEFORE UPDATE OF status ON skills FOR EACH ROW EXECUTE FUNCTION enforce_skill_archive_transition()",
    "CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_versions_files_seal_transition BEFORE UPDATE OF files_sealed ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_skill_version_files_seal_transition()",
    "CREATE CONSTRAINT TRIGGER trg_skill_versions_facts_complete AFTER INSERT OR UPDATE ON skill_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION verify_skill_version_file_facts()",
    "CREATE TRIGGER trg_skill_versions_revocation BEFORE INSERT OR UPDATE OF revoked_at, revoked_by_user_id, revocation_reason_code ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_system_skill_version_revocation()",
    "CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_mcp_secret_slots_immutable BEFORE UPDATE ON mcp_version_secret_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update()",
    "CREATE TRIGGER trg_agent_bindings_current BEFORE INSERT OR UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version()",
    "CREATE TRIGGER trg_skill_bindings_current BEFORE INSERT OR UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version()",
    "CREATE TRIGGER trg_mcp_bindings_published BEFORE INSERT OR UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version()",
    "CREATE TRIGGER trg_mcp_server_versions_bound_published BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_mcp_published_version_downgrade()",
    "CREATE TRIGGER trg_skill_version_files_child_immutable BEFORE INSERT OR DELETE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation()",
    "CREATE TRIGGER trg_mcp_secret_slots_child_immutable BEFORE INSERT OR DELETE ON mcp_version_secret_slots FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation()",
    "CREATE TRIGGER trg_mcp_server_versions_state_transition BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition()",
    "CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, definition_id, revision ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_version_id, revision ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_version_revocations_generation AFTER UPDATE OF revoked_at ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_server_versions_generation AFTER UPDATE OF workflow_status ON mcp_server_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_agent_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_agent_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_skill_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_skill_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
    "CREATE TRIGGER trg_mcp_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_mcp_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()",
)

_TRIGGER_TABLES = frozenset(
    {
        "agent_skill_refs",
        "agent_mcp_refs",
        "skill_versions",
        "skill_version_files",
        "mcp_server_versions",
        "mcp_version_secret_slots",
        "project_system_agent_bindings",
        "project_system_skill_bindings",
        "project_system_mcp_bindings",
        "agents",
        "skills",
        "mcp_servers",
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
