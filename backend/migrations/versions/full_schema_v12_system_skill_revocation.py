"""Add irreversible governance revocation for published System Skill releases."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v12"
down_revision = "full_schema_v11"
branch_labels = None
depends_on = None


_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code'
    ]::text[]) THEN
        RAISE EXCEPTION 'shared asset version payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_REVOCATION_FUNCTION = """
CREATE OR REPLACE FUNCTION enforce_system_skill_version_revocation()
RETURNS trigger AS $$
DECLARE
    asset_scope text;
    asset_project_id uuid;
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
    IF OLD.revoked_at IS NOT NULL
       OR OLD.revoked_by_user_id IS NOT NULL
       OR OLD.revocation_reason_code IS NOT NULL
       OR NEW.revoked_at IS NULL
       OR NEW.revoked_by_user_id IS NULL
       OR NEW.revocation_reason_code IS NULL
       OR NEW.revocation_reason_code NOT IN ('security', 'policy', 'integrity')
       OR NEW.workflow_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'system skill version revocation is irreversible'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT scope, project_id
    INTO asset_scope, asset_project_id
    FROM skills
    WHERE id = NEW.skill_id
    FOR UPDATE;
    IF asset_scope IS DISTINCT FROM 'system' OR asset_project_id IS NOT NULL THEN
        RAISE EXCEPTION 'only published system skill versions can be revoked'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_BINDING_FUNCTION = """
CREATE OR REPLACE FUNCTION ensure_system_binding_published_version()
RETURNS trigger AS $$
DECLARE
    version_status text;
    version_revoked_at timestamp with time zone;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'project_system_agent_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM agent_versions
            WHERE id = NEW.agent_version_id AND agent_id = NEW.system_agent_id
            FOR UPDATE;
        WHEN 'project_system_skill_bindings' THEN
            SELECT workflow_status, revoked_at
            INTO version_status, version_revoked_at
            FROM skill_versions
            WHERE id = NEW.skill_version_id AND skill_id = NEW.system_skill_id
            FOR UPDATE;
            IF TG_OP = 'UPDATE'
               AND OLD.enabled IS TRUE
               AND NEW.enabled IS FALSE
               AND OLD.system_skill_id = NEW.system_skill_id
               AND OLD.skill_version_id = NEW.skill_version_id THEN
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
    IF version_status IS DISTINCT FROM 'published'
       OR (TG_TABLE_NAME = 'project_system_skill_bindings'
           AND version_revoked_at IS NOT NULL) THEN
        RAISE EXCEPTION 'system binding requires non-revoked published version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute("ALTER TABLE skill_versions ADD COLUMN revoked_at TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE skill_versions ADD COLUMN revoked_by_user_id VARCHAR(36)")
    op.execute("ALTER TABLE skill_versions ADD COLUMN revocation_reason_code VARCHAR(32)")
    op.execute("ALTER TABLE skill_versions ADD CONSTRAINT fk_skill_versions_revoked_by_user_id FOREIGN KEY (revoked_by_user_id) REFERENCES users (id)")
    op.execute("ALTER TABLE skill_versions ADD CONSTRAINT ck_skill_versions_revocation CHECK ((revoked_at IS NULL) = (revoked_by_user_id IS NULL) AND (revoked_at IS NULL) = (revocation_reason_code IS NULL))")
    op.execute("ALTER TABLE skill_versions ADD CONSTRAINT ck_skill_versions_revocation_reason CHECK (revocation_reason_code IS NULL OR revocation_reason_code IN ('security', 'policy', 'integrity'))")
    op.execute(_IMMUTABLE_FUNCTION)
    op.execute(_REVOCATION_FUNCTION)
    op.execute(_BINDING_FUNCTION)
    op.execute("CREATE TRIGGER trg_skill_versions_revocation BEFORE INSERT OR UPDATE OF revoked_at, revoked_by_user_id, revocation_reason_code ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_system_skill_version_revocation()")
    op.execute("CREATE TRIGGER trg_skill_version_revocations_generation AFTER UPDATE OF revoked_at ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation()")
    op.execute("COMMENT ON COLUMN skill_versions.revoked_at IS '技能版本：不可逆治理撤销时间。'")
    op.execute("COMMENT ON COLUMN skill_versions.revoked_by_user_id IS '技能版本：执行撤销的用户标识。'")
    op.execute("COMMENT ON COLUMN skill_versions.revocation_reason_code IS '技能版本：撤销原因代码。'")


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
