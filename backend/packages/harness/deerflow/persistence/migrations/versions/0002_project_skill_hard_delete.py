"""Allow an authorized project Skill package to delete published file rows."""

from __future__ import annotations

from alembic import op

revision = "0002_project_skill_hard_delete"
down_revision = "0001_project_saas_baseline"
branch_labels = None
depends_on = None

_ALLOW_PROJECT_SKILL_DELETE = """
CREATE OR REPLACE FUNCTION prevent_published_version_child_mutation()
RETURNS trigger AS $$
DECLARE
    parent_version_id uuid;
    parent_status text;
    purge_allowed boolean := false;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'skill_version_files' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
            SELECT workflow_status INTO parent_status
            FROM skill_versions WHERE id = parent_version_id FOR UPDATE;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM skill_versions version
                    JOIN skills asset ON asset.id = version.skill_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.skill_version_id
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
                                  'deerflow.skill_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) INTO purge_allowed;
            END IF;
        WHEN 'agent_version_skill_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT workflow_status INTO parent_status
            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_versions version
                    JOIN agents asset ON asset.id = version.agent_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.agent_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) OR EXISTS (
                    SELECT 1
                    FROM skill_versions version
                    JOIN skills asset ON asset.id = version.skill_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.skill_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) INTO purge_allowed;
            END IF;
        WHEN 'agent_version_mcp_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT workflow_status INTO parent_status
            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_versions version
                    JOIN agents asset ON asset.id = version.agent_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.agent_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) OR EXISTS (
                    SELECT 1
                    FROM mcp_server_versions version
                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.mcp_server_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) INTO purge_allowed;
            END IF;
        WHEN 'mcp_version_credential_slots' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;
            SELECT workflow_status INTO parent_status
            FROM mcp_server_versions WHERE id = parent_version_id FOR UPDATE;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM mcp_server_versions version
                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.mcp_server_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) INTO purge_allowed;
            END IF;
        ELSE
            RAISE EXCEPTION 'unsupported version child table';
    END CASE;
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


def upgrade() -> None:
    op.execute(_ALLOW_PROJECT_SKILL_DELETE)


def downgrade() -> None:
    raise RuntimeError("forward-only schema downgrade is unsupported")
