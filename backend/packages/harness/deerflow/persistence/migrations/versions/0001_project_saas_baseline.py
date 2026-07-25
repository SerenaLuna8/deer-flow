"""Fresh-install-only M7 project SaaS baseline.

Generated once from the audited final SQLAlchemy metadata.  This revision is
self-contained: it never imports application models at migration runtime.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_project_saas_baseline"
down_revision = None
branch_labels = None
depends_on = None

_FINAL_POSTGRES_DDL = (
    "\n"
    "CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()\n"
    "RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    IF (to_jsonb(NEW) - ARRAY[\n"
    "        'workflow_status', 'status', 'submitted_at', 'reviewed_at',\n"
    "        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',\n"
    "        'revoked_by_user_id'\n"
    "    ]::text[]) IS DISTINCT FROM\n"
    "       (to_jsonb(OLD) - ARRAY[\n"
    "        'workflow_status', 'status', 'submitted_at', 'reviewed_at',\n"
    "        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',\n"
    "        'revoked_by_user_id'\n"
    "    ]::text[]) THEN\n"
    "        RAISE EXCEPTION 'shared asset version payload is immutable'\n"
    "            USING ERRCODE = 'integrity_constraint_violation';\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "\n"
    "CREATE OR REPLACE FUNCTION bump_asset_catalog_generation()\n"
    "RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    INSERT INTO asset_catalog_state (id, generation, updated_at)\n"
    "    VALUES (1, 1, now())\n"
    "    ON CONFLICT (id) DO UPDATE\n"
    "      SET generation = asset_catalog_state.generation + 1,\n"
    "          updated_at = now();\n"
    "    RETURN NULL;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "\n"
    "CREATE OR REPLACE FUNCTION ensure_system_binding_published_version()\n"
    "RETURNS trigger AS $$\n"
    "DECLARE\n"
    "    version_status text;\n"
    "BEGIN\n"
    "    CASE TG_TABLE_NAME\n"
    "        WHEN 'project_system_agent_bindings' THEN\n"
    "            SELECT workflow_status INTO version_status\n"
    "            FROM agent_versions\n"
    "            WHERE id = NEW.agent_version_id AND agent_id = NEW.system_agent_id\n"
    "            FOR UPDATE;\n"
    "        WHEN 'project_system_skill_bindings' THEN\n"
    "            SELECT workflow_status INTO version_status\n"
    "            FROM skill_versions\n"
    "            WHERE id = NEW.skill_version_id AND skill_id = NEW.system_skill_id\n"
    "            FOR UPDATE;\n"
    "        WHEN 'project_system_mcp_bindings' THEN\n"
    "            SELECT workflow_status INTO version_status\n"
    "            FROM mcp_server_versions\n"
    "            WHERE id = NEW.mcp_server_version_id\n"
    "              AND mcp_server_id = NEW.system_mcp_server_id\n"
    "            FOR UPDATE;\n"
    "        ELSE\n"
    "            RAISE EXCEPTION 'unsupported system binding table';\n"
    "    END CASE;\n"
    "    IF version_status IS DISTINCT FROM 'published' THEN\n"
    "        RAISE EXCEPTION 'system binding requires published version'\n"
    "            USING ERRCODE = 'integrity_constraint_violation';\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "\n"
    "CREATE OR REPLACE FUNCTION prevent_bound_published_version_downgrade()\n"
    "RETURNS trigger AS $$\n"
    "DECLARE\n"
    "    is_bound boolean;\n"
    "BEGIN\n"
    "    IF OLD.workflow_status = 'published'\n"
    "       AND NEW.workflow_status IS DISTINCT FROM 'published' THEN\n"
    "        CASE TG_TABLE_NAME\n"
    "            WHEN 'agent_versions' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1 FROM project_system_agent_bindings\n"
    "                    WHERE agent_version_id = OLD.id\n"
    "                ) INTO is_bound;\n"
    "            WHEN 'skill_versions' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1 FROM project_system_skill_bindings\n"
    "                    WHERE skill_version_id = OLD.id\n"
    "                ) INTO is_bound;\n"
    "            WHEN 'mcp_server_versions' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1 FROM project_system_mcp_bindings\n"
    "                    WHERE mcp_server_version_id = OLD.id\n"
    "                ) INTO is_bound;\n"
    "            ELSE\n"
    "                is_bound := false;\n"
    "        END CASE;\n"
    "        IF is_bound THEN\n"
    "            RAISE EXCEPTION 'bound published version cannot change workflow status'\n"
    "                USING ERRCODE = 'integrity_constraint_violation';\n"
    "        END IF;\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "\n"
    "CREATE OR REPLACE FUNCTION prevent_published_version_child_mutation()\n"
    "RETURNS trigger AS $$\n"
    "DECLARE\n"
    "    parent_version_id uuid;\n"
    "    parent_status text;\n"
    "    purge_allowed boolean := false;\n"
    "BEGIN\n"
    "    CASE TG_TABLE_NAME\n"
    "        WHEN 'skill_version_files' THEN\n"
    "            parent_version_id := CASE WHEN TG_OP = 'DELETE'\n"
    "                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;\n"
    "            SELECT workflow_status INTO parent_status\n"
    "            FROM skill_versions WHERE id = parent_version_id FOR UPDATE;\n"
    "            IF TG_OP = 'DELETE' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1\n"
    "                    FROM skill_versions version\n"
    "                    JOIN skills asset ON asset.id = version.skill_id\n"
    "                    JOIN projects project ON project.id = asset.project_id\n"
    "                    WHERE version.id = OLD.skill_version_id\n"
    "                      AND asset.scope = 'project'\n"
    "                      AND project.status = 'pending_deletion'\n"
    "                      AND project.deletion_effective_at IS NOT NULL\n"
    "                      AND project.deletion_effective_at <= now()\n"
    "                ) INTO purge_allowed;\n"
    "            END IF;\n"
    "        WHEN 'agent_version_skill_refs' THEN\n"
    "            parent_version_id := CASE WHEN TG_OP = 'DELETE'\n"
    "                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;\n"
    "            SELECT workflow_status INTO parent_status\n"
    "            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;\n"
    "            IF TG_OP = 'DELETE' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1\n"
    "                    FROM agent_versions version\n"
    "                    JOIN agents asset ON asset.id = version.agent_id\n"
    "                    JOIN projects project ON project.id = asset.project_id\n"
    "                    WHERE version.id = OLD.agent_version_id\n"
    "                      AND asset.scope = 'project'\n"
    "                      AND project.status = 'pending_deletion'\n"
    "                      AND project.deletion_effective_at IS NOT NULL\n"
    "                      AND project.deletion_effective_at <= now()\n"
    "                ) OR EXISTS (\n"
    "                    SELECT 1\n"
    "                    FROM skill_versions version\n"
    "                    JOIN skills asset ON asset.id = version.skill_id\n"
    "                    JOIN projects project ON project.id = asset.project_id\n"
    "                    WHERE version.id = OLD.skill_version_id\n"
    "                      AND asset.scope = 'project'\n"
    "                      AND project.status = 'pending_deletion'\n"
    "                      AND project.deletion_effective_at IS NOT NULL\n"
    "                      AND project.deletion_effective_at <= now()\n"
    "                ) INTO purge_allowed;\n"
    "            END IF;\n"
    "        WHEN 'agent_version_mcp_refs' THEN\n"
    "            parent_version_id := CASE WHEN TG_OP = 'DELETE'\n"
    "                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;\n"
    "            SELECT workflow_status INTO parent_status\n"
    "            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;\n"
    "            IF TG_OP = 'DELETE' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1\n"
    "                    FROM agent_versions version\n"
    "                    JOIN agents asset ON asset.id = version.agent_id\n"
    "                    JOIN projects project ON project.id = asset.project_id\n"
    "                    WHERE version.id = OLD.agent_version_id\n"
    "                      AND asset.scope = 'project'\n"
    "                      AND project.status = 'pending_deletion'\n"
    "                      AND project.deletion_effective_at IS NOT NULL\n"
    "                      AND project.deletion_effective_at <= now()\n"
    "                ) OR EXISTS (\n"
    "                    SELECT 1\n"
    "                    FROM mcp_server_versions version\n"
    "                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id\n"
    "                    JOIN projects project ON project.id = asset.project_id\n"
    "                    WHERE version.id = OLD.mcp_server_version_id\n"
    "                      AND asset.scope = 'project'\n"
    "                      AND project.status = 'pending_deletion'\n"
    "                      AND project.deletion_effective_at IS NOT NULL\n"
    "                      AND project.deletion_effective_at <= now()\n"
    "                ) INTO purge_allowed;\n"
    "            END IF;\n"
    "        WHEN 'mcp_version_credential_slots' THEN\n"
    "            parent_version_id := CASE WHEN TG_OP = 'DELETE'\n"
    "                THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;\n"
    "            SELECT workflow_status INTO parent_status\n"
    "            FROM mcp_server_versions WHERE id = parent_version_id FOR UPDATE;\n"
    "            IF TG_OP = 'DELETE' THEN\n"
    "                SELECT EXISTS (\n"
    "                    SELECT 1\n"
    "                    FROM mcp_server_versions version\n"
    "                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id\n"
    "                    JOIN projects project ON project.id = asset.project_id\n"
    "                    WHERE version.id = OLD.mcp_server_version_id\n"
    "                      AND asset.scope = 'project'\n"
    "                      AND project.status = 'pending_deletion'\n"
    "                      AND project.deletion_effective_at IS NOT NULL\n"
    "                      AND project.deletion_effective_at <= now()\n"
    "                ) INTO purge_allowed;\n"
    "            END IF;\n"
    "        ELSE\n"
    "            RAISE EXCEPTION 'unsupported version child table';\n"
    "    END CASE;\n"
    "    IF TG_OP = 'DELETE' AND purge_allowed THEN\n"
    "        RETURN OLD;\n"
    "    END IF;\n"
    "    IF parent_status IS DISTINCT FROM 'draft' THEN\n"
    "        RAISE EXCEPTION 'published version child rows are immutable'\n"
    "            USING ERRCODE = 'integrity_constraint_violation';\n"
    "    END IF;\n"
    "    IF TG_OP = 'DELETE' THEN\n"
    "        RETURN OLD;\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "\n"
    "CREATE OR REPLACE FUNCTION enforce_shared_asset_version_state_transition()\n"
    "RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    IF TG_TABLE_NAME = 'credential_versions' THEN\n"
    "        IF NEW.status = OLD.status\n"
    "           OR (OLD.status = 'active' AND NEW.status IN ('retired', 'revoked'))\n"
    "           OR (OLD.status = 'retired' AND NEW.status = 'revoked') THEN\n"
    "            RETURN NEW;\n"
    "        END IF;\n"
    "        RAISE EXCEPTION 'invalid credential version status transition'\n"
    "            USING ERRCODE = 'integrity_constraint_violation';\n"
    "    END IF;\n"
    "\n"
    "    IF NEW.workflow_status = OLD.workflow_status\n"
    "       OR (OLD.workflow_status = 'draft'\n"
    "           AND NEW.workflow_status IN ('pending_approval', 'published'))\n"
    "       OR (OLD.workflow_status = 'pending_approval'\n"
    "           AND NEW.workflow_status IN ('published', 'rejected')) THEN\n"
    "        RETURN NEW;\n"
    "    END IF;\n"
    "    RAISE EXCEPTION 'invalid shared asset version workflow transition'\n"
    "        USING ERRCODE = 'integrity_constraint_violation';\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
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
    "\n"
    "CREATE OR REPLACE FUNCTION enforce_scheduled_task_agent_project()\n"
    "RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    IF TG_TABLE_NAME = 'scheduled_tasks' THEN\n"
    "        IF NEW.agent_scope = 'project' THEN\n"
    "            PERFORM 1\n"
    "            FROM agents\n"
    "            WHERE id = NEW.agent_asset_id\n"
    "              AND scope = 'project'\n"
    "              AND project_id = NEW.project_id\n"
    "            FOR SHARE;\n"
    "            IF NOT FOUND THEN\n"
    "                RAISE EXCEPTION 'project Agent must belong to the scheduled task project'\n"
    "                    USING ERRCODE = 'foreign_key_violation';\n"
    "            END IF;\n"
    "        END IF;\n"
    "        RETURN NEW;\n"
    "    END IF;\n"
    "\n"
    "    IF TG_TABLE_NAME = 'agents'\n"
    "       AND NEW.project_id IS DISTINCT FROM OLD.project_id\n"
    "       AND EXISTS (\n"
    "           SELECT 1\n"
    "           FROM scheduled_tasks task\n"
    "           WHERE task.agent_asset_id = OLD.id\n"
    "             AND task.agent_scope = 'project'\n"
    "             AND task.project_id IS DISTINCT FROM NEW.project_id\n"
    "       ) THEN\n"
    "        RAISE EXCEPTION 'cannot move a project Agent referenced by scheduled tasks'\n"
    "            USING ERRCODE = 'foreign_key_violation';\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "CREATE TRIGGER trg_scheduled_tasks_agent_project BEFORE INSERT OR UPDATE OF project_id, agent_asset_id, agent_scope ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project()",
    "CREATE TRIGGER trg_agents_scheduled_task_project BEFORE UPDATE OF project_id ON agents FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project()",
    "\n"
    "CREATE OR REPLACE FUNCTION reject_m7_append_only_mutation()\n"
    "RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    RAISE EXCEPTION 'M7 append-only rows cannot be updated or deleted'\n"
    "        USING ERRCODE = 'integrity_constraint_violation';\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "CREATE TRIGGER trg_project_usage_ledger_append_only BEFORE UPDATE OR DELETE ON project_usage_ledger FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation()",
    "CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation()",
    "CREATE TRIGGER trg_dead_jobs_append_only BEFORE UPDATE OR DELETE ON dead_jobs FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation()",
    "\n"
    "CREATE OR REPLACE FUNCTION enforce_stream_terminal_invariant()\n"
    "RETURNS trigger AS $$\n"
    "BEGIN\n"
    "    IF NEW.category = 'stream' THEN\n"
    "        PERFORM 1 FROM thread_event_sequences\n"
    "         WHERE project_id = NEW.project_id\n"
    "           AND owner_user_id = NEW.owner_user_id\n"
    "           AND thread_id = NEW.thread_id\n"
    "         FOR UPDATE;\n"
    "        IF NEW.event_type <> 'stream.end' AND EXISTS (\n"
    "            SELECT 1 FROM run_events\n"
    "             WHERE project_id = NEW.project_id\n"
    "               AND owner_user_id = NEW.owner_user_id\n"
    "               AND thread_id = NEW.thread_id\n"
    "               AND run_id = NEW.run_id\n"
    "               AND category = 'stream'\n"
    "               AND event_type = 'stream.end'\n"
    "        ) THEN\n"
    "            RAISE EXCEPTION 'stream event cannot follow terminal event'\n"
    "                USING ERRCODE = 'integrity_constraint_violation';\n"
    "        END IF;\n"
    "    END IF;\n"
    "    RETURN NEW;\n"
    "END;\n"
    "$$ LANGUAGE plpgsql\n",
    "CREATE TRIGGER trg_run_events_stream_terminal BEFORE INSERT ON run_events FOR EACH ROW EXECUTE FUNCTION enforce_stream_terminal_invariant()",
    "\nCREATE OR REPLACE FUNCTION set_m7_updated_at()\nRETURNS trigger AS $$\nBEGIN\n    NEW.updated_at := now();\n    RETURN NEW;\nEND;\n$$ LANGUAGE plpgsql\n",
    "CREATE TRIGGER trg_agents_updated_at BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_asset_catalog_state_updated_at BEFORE UPDATE ON asset_catalog_state FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_channel_connections_updated_at BEFORE UPDATE ON channel_connections FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_channel_conversations_updated_at BEFORE UPDATE ON channel_conversations FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_channel_credentials_updated_at BEFORE UPDATE ON channel_credentials FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_credentials_updated_at BEFORE UPDATE ON credentials FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_files_updated_at BEFORE UPDATE ON files FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_mcp_servers_updated_at BEFORE UPDATE ON mcp_servers FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_invitation_rate_limits_updated_at BEFORE UPDATE ON project_invitation_rate_limits FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_memberships_updated_at BEFORE UPDATE ON project_memberships FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_quotas_updated_at BEFORE UPDATE ON project_quotas FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_system_agent_bindings_updated_at BEFORE UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_system_mcp_bindings_updated_at BEFORE UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_system_skill_bindings_updated_at BEFORE UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_project_usage_counters_updated_at BEFORE UPDATE ON project_usage_counters FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_runs_updated_at BEFORE UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_scheduled_task_runs_updated_at BEFORE UPDATE ON scheduled_task_runs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_scheduled_tasks_updated_at BEFORE UPDATE ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_skills_updated_at BEFORE UPDATE ON skills FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_threads_meta_updated_at BEFORE UPDATE ON threads_meta FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_user_project_memories_updated_at BEFORE UPDATE ON user_project_memories FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
    "CREATE TRIGGER trg_user_project_memory_facts_updated_at BEFORE UPDATE ON user_project_memory_facts FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at()",
)


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "asset_catalog_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("generation", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("generation >= 1", name="ck_asset_catalog_state_generation"),
        sa.CheckConstraint("id = 1", name="ck_asset_catalog_state_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "dead_jobs",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_ref_key_id", sa.String(length=64), nullable=True),
        sa.Column("owner_ref_hmac", sa.CHAR(length=64), nullable=True),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("retry_safety", sa.String(length=16), nullable=False),
        sa.Column("public_error_code", sa.String(length=64), nullable=False),
        sa.Column("dead_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_dead_jobs_retry_safety"),
        sa.CheckConstraint("attempt_count >= 1", name="ck_dead_jobs_attempt_count"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_dead_jobs_job", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_dead_jobs_project", ondelete="RESTRICT", use_alter=True),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_index("ix_dead_jobs_project_dead", "dead_jobs", ["project_id", sa.literal_column("dead_at DESC"), "job_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("automation_occurrence_id", sa.String(length=64), nullable=True),
        sa.Column("predecessor_dead_job_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="queued", nullable=False),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("lease_owner_id", sa.Uuid(), nullable=True),
        sa.Column("lease_token_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_safety", sa.String(length=16), server_default="safe", nullable=False),
        sa.Column("public_error_code", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(job_type = 'private_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL "
            "AND automation_occurrence_id IS NULL) OR (job_type = 'automation_run' AND run_id IS NOT NULL "
            "AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL) OR "
            "(job_type = 'retention_purge' AND run_id IS NULL AND automation_occurrence_id IS NULL)",
            name="ck_jobs_authority_shape",
        ),
        sa.CheckConstraint("job_type IN ('private_run', 'automation_run', 'retention_purge')", name="ck_jobs_type"),
        sa.CheckConstraint("retry_safety IN ('safe', 'unknown', 'unsafe')", name="ck_jobs_retry_safety"),
        sa.CheckConstraint("status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')", name="ck_jobs_status"),
        sa.CheckConstraint("attempt_count >= 0 AND max_attempts >= 1", name="ck_jobs_attempts"),
        sa.ForeignKeyConstraint(["lease_owner_id"], ["worker_nodes.id"], name="fk_jobs_lease_worker", ondelete="SET NULL", use_alter=True),
        sa.ForeignKeyConstraint(["predecessor_dead_job_id"], ["dead_jobs.job_id"], name="fk_jobs_predecessor_dead_job", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "automation_occurrence_id"],
            ["scheduled_task_runs.project_id", "scheduled_task_runs.owner_user_id", "scheduled_task_runs.id"],
            name="fk_jobs_automation_occurrence",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.run_id"], name="fk_jobs_private_run", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_jobs_project", ondelete="RESTRICT", use_alter=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_type", "idempotency_key", name="uq_jobs_type_idempotency"),
        sa.UniqueConstraint("predecessor_dead_job_id", name="uq_jobs_predecessor_dead_job"),
    )
    op.create_index("ix_jobs_active_lease", "jobs", ["lease_expires_at", "id"], unique=False, postgresql_where=sa.text("status IN ('leased', 'running')"))
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", sa.literal_column("priority DESC"), "created_at"], unique=False)
    op.create_index("ix_jobs_private_scope", "jobs", ["project_id", "owner_user_id", "created_at"], unique=False)
    # Shared fixed-window counters. The historical M2 table name is retained,
    # while domain-separated hashes isolate invitation, login, and registration
    # policies without introducing process-local state.
    op.create_table(
        "project_invitation_rate_limits",
        sa.Column("key_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="ck_project_invitation_rate_limits_key_hash"),
        sa.CheckConstraint("failure_count >= 1", name="ck_project_invitation_rate_limits_failure_count"),
        sa.PrimaryKeyConstraint("key_hash"),
    )
    op.create_index(op.f("ix_project_invitation_rate_limits_expires_at"), "project_invitation_rate_limits", ["expires_at"], unique=False)
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=128), nullable=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=True),
        sa.Column("multitask_strategy", sa.String(length=20), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("kwargs_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("first_human_message", sa.Text(), nullable=True),
        sa.Column("last_ai_message", sa.Text(), nullable=True),
        sa.Column("total_input_tokens", sa.Integer(), nullable=False),
        sa.Column("total_output_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("llm_call_count", sa.Integer(), nullable=False),
        sa.Column("lead_agent_tokens", sa.Integer(), nullable=False),
        sa.Column("subagent_tokens", sa.Integer(), nullable=False),
        sa.Column("middleware_tokens", sa.Integer(), nullable=False),
        sa.Column("token_usage_by_model", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("follow_up_to_run_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("execution_lease_token_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("execution_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_reason", sa.String(length=64), nullable=True),
        sa.Column("authorization_cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorization_cancel_reason", sa.String(length=64), nullable=True),
        sa.Column("finalization_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.CheckConstraint("finalization_status IN ('pending', 'finalizing', 'complete', 'failed')", name="ck_runs_finalization_status"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_runs_job", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_runs_owner", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_runs_private_thread", ondelete="CASCADE", use_alter=True),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_runs_project_membership", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_runs_project", ondelete="RESTRICT", use_alter=True),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "run_id", name="uq_runs_job_scope"),
        sa.UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", name="uq_runs_private_scope"),
    )
    op.create_index(op.f("ix_runs_owner_user_id"), "runs", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_runs_project_id"), "runs", ["project_id"], unique=False)
    op.create_index(op.f("ix_runs_thread_id"), "runs", ["thread_id"], unique=False)
    op.create_index("ix_runs_thread_status", "runs", ["thread_id", "status"], unique=False)
    op.create_table(
        "scheduled_task_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=64), nullable=False),
        sa.Column("task_version", sa.BigInteger(), nullable=False),
        sa.Column("occurrence_key", sa.CHAR(length=64), nullable=False),
        sa.Column("manual_idempotency_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_membership_id", sa.Uuid(), nullable=True),
        sa.Column("resolved_membership_version", sa.BigInteger(), nullable=True),
        sa.Column("launch_attempt_count", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('queued', 'launching', 'running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')", name="ck_scheduled_task_runs_status"),
        sa.CheckConstraint("trigger IN ('scheduled', 'manual')", name="ck_scheduled_task_runs_trigger"),
        sa.CheckConstraint("launch_attempt_count >= 0 AND (resolved_membership_version IS NULL OR resolved_membership_version >= 1)", name="ck_scheduled_task_runs_attempt_count"),
        sa.CheckConstraint("run_id IS NULL OR thread_id IS NOT NULL", name="ck_scheduled_task_runs_run_requires_thread"),
        sa.CheckConstraint("task_version >= 1", name="ck_scheduled_task_runs_task_version"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name="fk_scheduled_task_runs_job", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_scheduled_task_runs_owner", ondelete="RESTRICT", use_alter=True),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "task_id"], ["scheduled_tasks.project_id", "scheduled_tasks.owner_user_id", "scheduled_tasks.id"], name="fk_scheduled_task_runs_task", ondelete="CASCADE", use_alter=True),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_scheduled_task_runs_private_run", ondelete="RESTRICT", use_alter=True
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_scheduled_task_runs_private_thread", ondelete="RESTRICT", use_alter=True
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_scheduled_task_runs_project", ondelete="RESTRICT", use_alter=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "id", name="uq_scheduled_task_runs_job_scope"),
        sa.UniqueConstraint("project_id", "owner_user_id", "task_id", "occurrence_key", name="uq_scheduled_task_runs_occurrence"),
    )
    op.create_index("ix_scheduled_task_runs_active_occurrence", "scheduled_task_runs", ["project_id", "owner_user_id", "status", "scheduled_for", "id"], unique=False, postgresql_where=sa.text("status IN ('queued', 'launching', 'running')"))
    op.create_index("ix_scheduled_task_runs_history", "scheduled_task_runs", ["project_id", "owner_user_id", "task_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")], unique=False)
    op.create_index(op.f("ix_scheduled_task_runs_owner_user_id"), "scheduled_task_runs", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_scheduled_task_runs_project_id"), "scheduled_task_runs", ["project_id"], unique=False)
    op.create_index("uq_scheduled_task_runs_manual_idempotency", "scheduled_task_runs", ["project_id", "owner_user_id", "task_id", "manual_idempotency_hash"], unique=True, postgresql_where=sa.text("manual_idempotency_hash IS NOT NULL"))
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=128), nullable=True),
        sa.Column("system_role", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("oauth_provider", sa.String(length=32), nullable=True),
        sa.Column("oauth_id", sa.String(length=128), nullable=True),
        sa.Column("needs_setup", sa.Boolean(), nullable=False),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("system_role IN ('system_admin', 'user')", name="ck_users_system_role"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_users_oauth_identity", "users", ["oauth_provider", "oauth_id"], unique=True, postgresql_where=sa.text("oauth_provider IS NOT NULL AND oauth_id IS NOT NULL"))
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)
    op.create_table(
        "auth_sessions",
        sa.Column("session_id_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expiry"),
        sa.CheckConstraint("session_id_hash ~ '^[0-9a-f]{64}$'", name="ck_auth_sessions_hash"),
        sa.CheckConstraint("last_seen_at >= created_at AND last_seen_at <= expires_at", name="ck_auth_sessions_last_seen"),
        sa.CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="ck_auth_sessions_revoked_at"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id_hash"),
    )
    op.create_index(
        "ix_auth_sessions_expires_at",
        "auth_sessions",
        ["expires_at", "session_id_hash"],
        unique=False,
    )
    op.create_index(
        "ix_auth_sessions_revoked_at",
        "auth_sessions",
        ["revoked_at", "session_id_hash"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NOT NULL"),
    )
    op.create_index(
        "ix_auth_sessions_user_active",
        "auth_sessions",
        ["user_id", "expires_at"],
        unique=False,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )
    op.create_table(
        "worker_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("max_concurrent_jobs", sa.Integer(), nullable=False),
        sa.Column("draining", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("max_concurrent_jobs >= 1", name="ck_worker_nodes_capacity"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_worker_nodes_fresh", "worker_nodes", ["draining", "heartbeat_at"], unique=False)
    op.create_table(
        "job_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Uuid(), nullable=False),
        sa.Column("lease_token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=True),
        sa.Column("public_error_code", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_cursor", sa.String(length=128), nullable=True),
        sa.Column("stream_cursor", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("outcome IS NULL OR outcome IN ('succeeded', 'retry', 'cancelled', 'failed', 'lease_lost', 'dead')", name="ck_job_attempts_outcome"),
        sa.CheckConstraint("attempt_number >= 1", name="ck_job_attempts_number"),
        sa.CheckConstraint("stream_cursor IS NULL OR stream_cursor >= 0", name="ck_job_attempts_stream_cursor"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_id"], ["worker_nodes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_job_attempts_number"),
    )
    op.create_index("ix_job_attempts_job_started", "job_attempts", ["job_id", sa.literal_column("started_at DESC")], unique=False)
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=500), server_default="", nullable=False),
        sa.Column("icon", sa.String(length=32), server_default="folder", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("deletion_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_effective_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deletion_requested_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("is_suspended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("membership_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name="ck_projects_slug_format"),
        sa.CheckConstraint("status IN ('active', 'pending_deletion')", name="ck_projects_status"),
        sa.CheckConstraint("char_length(slug) BETWEEN 3 AND 63", name="ck_projects_slug_length"),
        sa.CheckConstraint("membership_version >= 1", name="ck_projects_membership_version"),
        sa.CheckConstraint("slug = lower(slug)", name="ck_projects_slug_lowercase"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["deletion_requested_by_user_id"], ["users.id"], name="fk_projects_deletion_requested_by_user_id_users"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_projects_slug"),
    )
    op.create_table(
        "agents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("current_published_version_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)", name="ck_agents_scope_project"),
        sa.CheckConstraint("status IN ('active', 'archived', 'suspended')", name="ck_agents_status"),
        sa.CheckConstraint("version >= 1", name="ck_agents_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["id", "current_published_version_id"], ["agent_versions.agent_id", "agent_versions.id"], name="fk_agents_current_published_version", use_alter=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "scope", name="uq_agents_id_scope"),
        sa.UniqueConstraint("source_key", name="uq_agents_source_key"),
    )
    op.create_index("uq_agents_project_slug", "agents", ["project_id", sa.literal_column("lower(slug)")], unique=True, postgresql_where=sa.text("scope = 'project'"))
    op.create_index("uq_agents_system_slug", "agents", [sa.literal_column("lower(slug)")], unique=True, postgresql_where=sa.text("scope = 'system'"))
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_process", sa.String(length=32), nullable=True),
        sa.Column("actor_platform_role", sa.String(length=32), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_kind", sa.String(length=32), nullable=False),
        sa.Column("target_ref_key_id", sa.String(length=64), nullable=False),
        sa.Column("target_ref_hmac", sa.CHAR(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("public_error_code", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("attempt_id", sa.Uuid(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.CheckConstraint("outcome IN ('success', 'rejected', 'failed')", name="ck_audit_logs_outcome"),
        sa.CheckConstraint("(actor_user_id IS NOT NULL AND actor_process IS NULL) OR (actor_user_id IS NULL AND actor_process IS NOT NULL)", name="ck_audit_logs_actor"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["job_attempts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_logs_platform_cursor", "audit_logs", [sa.literal_column("occurred_at DESC"), sa.literal_column("id DESC")], unique=False)
    op.create_index("ix_audit_logs_project_cursor", "audit_logs", ["project_id", sa.literal_column("occurred_at DESC"), sa.literal_column("id DESC")], unique=False)
    op.create_table(
        "credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("credential_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("current_version_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)", name="ck_credentials_scope_project"),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_credentials_status"),
        sa.CheckConstraint("version >= 1", name="ck_credentials_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["id", "current_version_id"], ["credential_versions.credential_id", "credential_versions.id"], name="fk_credentials_current_version", use_alter=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "scope", name="uq_credentials_id_scope"),
        sa.UniqueConstraint("source_key", name="uq_credentials_source_key"),
    )
    op.create_index("uq_credentials_project_name", "credentials", ["project_id", sa.literal_column("lower(name)")], unique=True, postgresql_where=sa.text("scope = 'project'"))
    op.create_index("uq_credentials_system_name", "credentials", [sa.literal_column("lower(name)")], unique=True, postgresql_where=sa.text("scope = 'system'"))
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("current_published_version_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)", name="ck_mcp_servers_scope_project"),
        sa.CheckConstraint("status IN ('active', 'archived', 'suspended')", name="ck_mcp_servers_status"),
        sa.CheckConstraint("version >= 1", name="ck_mcp_servers_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["id", "current_published_version_id"], ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"], name="fk_mcp_servers_current_published_version", use_alter=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "scope", name="uq_mcp_servers_id_scope"),
        sa.UniqueConstraint("source_key", name="uq_mcp_servers_source_key"),
    )
    op.create_index("uq_mcp_servers_project_slug", "mcp_servers", ["project_id", sa.literal_column("lower(slug)")], unique=True, postgresql_where=sa.text("scope = 'project'"))
    op.create_index("uq_mcp_servers_system_slug", "mcp_servers", [sa.literal_column("lower(slug)")], unique=True, postgresql_where=sa.text("scope = 'system'"))
    op.create_table(
        "project_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("invited_email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("redeemed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('editor', 'runner', 'viewer')", name="ck_project_invitations_role"),
        sa.CheckConstraint("status IN ('pending', 'redeemed', 'revoked', 'expired')", name="ck_project_invitations_status"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_project_invitations_token_hash"),
        sa.CheckConstraint("version >= 1", name="ck_project_invitations_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["redeemed_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_project_invitations_token_hash"),
    )
    op.create_index("uq_project_invitations_pending_email", "project_invitations", ["project_id", "invited_email"], unique=True, postgresql_where=sa.text("status = 'pending'"))
    op.create_table(
        "user_notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recipient_user_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("project_invitation_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("kind = 'project_invitation'", name="ck_user_notifications_kind"),
        sa.CheckConstraint("version >= 1", name="ck_user_notifications_version"),
        sa.CheckConstraint("read_at IS NULL OR read_at >= created_at", name="ck_user_notifications_read_at"),
        sa.CheckConstraint("acted_at IS NULL OR acted_at >= created_at", name="ck_user_notifications_acted_at"),
        sa.CheckConstraint("acted_at IS NULL OR read_at IS NOT NULL", name="ck_user_notifications_acted_is_read"),
        sa.ForeignKeyConstraint(["project_invitation_id"], ["project_invitations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_invitation_id", name="uq_user_notifications_project_invitation_id"),
    )
    op.create_index(
        "ix_user_notifications_recipient_cursor",
        "user_notifications",
        ["recipient_user_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_user_notifications_recipient_unread",
        "user_notifications",
        ["recipient_user_id", "created_at"],
        unique=False,
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_table(
        "project_memberships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("end_reason", sa.String(length=16), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("activation_generation", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("end_reason IS NULL OR end_reason IN ('left', 'removed')", name="ck_project_memberships_end_reason"),
        sa.CheckConstraint("activation_generation >= 1", name="ck_project_memberships_activation_generation"),
        sa.CheckConstraint("role IN ('admin', 'editor', 'runner', 'viewer')", name="ck_project_memberships_role"),
        sa.CheckConstraint("status IN ('active', 'left', 'removed')", name="ck_project_memberships_status"),
        sa.CheckConstraint("version >= 1", name="ck_project_memberships_version"),
        sa.ForeignKeyConstraint(["ended_by_user_id"], ["users.id"], name="fk_project_memberships_ended_by_user_id_users"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_memberships_project_user"),
    )
    op.create_index("ix_project_memberships_user_id", "project_memberships", ["user_id"], unique=False)
    op.create_table(
        "project_quotas",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("member_limit", sa.Integer(), nullable=True),
        sa.Column("storage_bytes_limit", sa.BigInteger(), nullable=True),
        sa.Column("concurrent_run_limit", sa.Integer(), nullable=True),
        sa.Column("mcp_calls_daily_limit", sa.Integer(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "(member_limit IS NULL OR member_limit >= 1) AND "
            "(storage_bytes_limit IS NULL OR storage_bytes_limit >= 0) AND "
            "(concurrent_run_limit IS NULL OR concurrent_run_limit >= 1) AND "
            "(mcp_calls_daily_limit IS NULL OR mcp_calls_daily_limit >= 0) AND version >= 1",
            name="ck_project_quotas_limits",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id"),
    )
    op.create_table(
        "project_usage_counters",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("bucket", sa.String(length=32), server_default="lifetime", nullable=False),
        sa.Column("used", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("reserved", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')", name="ck_project_usage_counters_dimension"),
        sa.CheckConstraint("used >= 0 AND reserved >= 0 AND version >= 1", name="ck_project_usage_counters_values"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "dimension", "bucket"),
    )
    op.create_table(
        "project_usage_ledger",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("bucket", sa.String(length=32), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_ref_key_id", sa.String(length=64), nullable=False),
        sa.Column("source_ref_hmac", sa.CHAR(length=64), nullable=False),
        sa.Column("idempotency_key", sa.CHAR(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')", name="ck_project_usage_ledger_dimension"),
        sa.CheckConstraint("delta <> 0", name="ck_project_usage_ledger_delta"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "dimension", "idempotency_key", name="uq_project_usage_ledger_idempotency"),
    )
    op.create_index("ix_project_usage_ledger_project_cursor", "project_usage_ledger", ["project_id", sa.literal_column("occurred_at DESC"), sa.literal_column("id DESC")], unique=False)
    op.create_table(
        "skills",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("current_published_version_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("(scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)", name="ck_skills_scope_project"),
        sa.CheckConstraint("status IN ('active', 'archived', 'suspended')", name="ck_skills_status"),
        sa.CheckConstraint("version >= 1", name="ck_skills_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["id", "current_published_version_id"], ["skill_versions.skill_id", "skill_versions.id"], name="fk_skills_current_published_version", use_alter=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "scope", name="uq_skills_id_scope"),
        sa.UniqueConstraint("source_key", name="uq_skills_source_key"),
    )
    op.create_index("uq_skills_project_slug", "skills", ["project_id", sa.literal_column("lower(slug)")], unique=True, postgresql_where=sa.text("scope = 'project'"))
    op.create_index("uq_skills_system_slug", "skills", [sa.literal_column("lower(slug)")], unique=True, postgresql_where=sa.text("scope = 'system'"))
    op.create_table(
        "agent_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("workflow_status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("soul", sa.Text(), nullable=False),
        sa.Column("model_ref", sa.String(length=255), nullable=False),
        sa.Column("tool_groups", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("payload_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_agent_versions_checksum"),
        sa.CheckConstraint("workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')", name="ck_agent_versions_workflow_status"),
        sa.CheckConstraint("version_number >= 1", name="ck_agent_versions_number"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "id", name="uq_agent_versions_asset_id"),
        sa.UniqueConstraint("agent_id", "version_number", name="uq_agent_versions_asset_number"),
    )
    op.create_table(
        "channel_connections",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=False),
        sa.Column("external_account_name", sa.String(length=256), nullable=True),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("workspace_name", sa.String(length=256), nullable=True),
        sa.Column("bot_user_id", sa.String(length=128), nullable=True),
        sa.Column("scopes_json", sa.JSON(), nullable=False),
        sa.Column("capabilities_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('connected', 'frozen', 'revoked')", name="ck_channel_connections_status"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_channel_connections_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_channel_connections_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_channel_connections_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "id", name="uq_channel_connections_private_scope"),
        sa.UniqueConstraint("project_id", "owner_user_id", "provider", "external_account_id", "workspace_id", name="uq_channel_connection_owner_provider_identity"),
    )
    op.create_index("idx_channel_connections_event_lookup", "channel_connections", ["provider", "workspace_id", "bot_user_id"], unique=False)
    op.create_index(op.f("ix_channel_connections_owner_user_id"), "channel_connections", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_channel_connections_project_id"), "channel_connections", ["project_id"], unique=False)
    op.create_index(op.f("ix_channel_connections_provider"), "channel_connections", ["provider"], unique=False)
    op.create_index("uq_channel_connection_active_identity", "channel_connections", ["provider", "external_account_id", "workspace_id"], unique=True, postgresql_where=sa.text("status = 'connected'"))
    op.create_table(
        "channel_oauth_states",
        sa.Column("state_hash", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("code_verifier_encrypted", sa.Text(), nullable=True),
        sa.Column("nonce_hash", sa.String(length=128), nullable=True),
        sa.Column("redirect_after", sa.Text(), nullable=True),
        sa.Column("requested_scopes_json", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_channel_oauth_states_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_channel_oauth_states_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_channel_oauth_states_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("state_hash"),
    )
    op.create_index(op.f("ix_channel_oauth_states_owner_user_id"), "channel_oauth_states", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_channel_oauth_states_project_id"), "channel_oauth_states", ["project_id"], unique=False)
    op.create_index(op.f("ix_channel_oauth_states_provider"), "channel_oauth_states", ["provider"], unique=False)
    op.create_table(
        "credential_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("payload_schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("payload_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'retired', 'revoked')", name="ck_credential_versions_status"),
        sa.CheckConstraint("payload_schema_version >= 1", name="ck_credential_versions_payload_schema_version"),
        sa.CheckConstraint("version_number >= 1", name="ck_credential_versions_number"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["credential_id"], ["credentials.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["credential_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id", "id", name="uq_credential_versions_asset_id"),
        sa.UniqueConstraint("credential_id", "version_number", name="uq_credential_versions_asset_number"),
    )
    op.create_table(
        "feedback",
        sa.Column("feedback_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_feedback_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_feedback_private_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_feedback_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_feedback_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("feedback_id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", name="uq_feedback_private_run_owner"),
    )
    op.create_index(op.f("ix_feedback_owner_user_id"), "feedback", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_feedback_project_id"), "feedback", ["project_id"], unique=False)
    op.create_index(op.f("ix_feedback_run_id"), "feedback", ["run_id"], unique=False)
    op.create_index(op.f("ix_feedback_thread_id"), "feedback", ["thread_id"], unique=False)
    op.create_table(
        "mcp_server_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("workflow_status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("transport", sa.String(length=24), nullable=False),
        sa.Column("command", sa.Text(), nullable=True),
        sa.Column("args", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("non_secret_env", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("non_secret_headers", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("oauth_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("routing", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("tool_overrides", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), server_default=sa.text("30"), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("payload_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_mcp_server_versions_checksum"),
        sa.CheckConstraint("transport IN ('stdio', 'sse', 'http', 'streamable_http')", name="ck_mcp_server_versions_transport"),
        sa.CheckConstraint("workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')", name="ck_mcp_server_versions_workflow_status"),
        sa.CheckConstraint("timeout_seconds > 0", name="ck_mcp_server_versions_timeout"),
        sa.CheckConstraint("version_number >= 1", name="ck_mcp_server_versions_number"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["mcp_server_id"], ["mcp_servers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mcp_server_id", "id", name="uq_mcp_server_versions_asset_id"),
        sa.UniqueConstraint("mcp_server_id", "version_number", name="uq_mcp_server_versions_asset_number"),
    )
    op.create_table(
        "run_asset_versions",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("asset_kind", sa.String(length=16), nullable=False),
        sa.Column("dependency_order", sa.Integer(), nullable=False),
        sa.Column("asset_scope", sa.String(length=16), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("version_id", sa.Uuid(), nullable=False),
        sa.Column("payload_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("catalog_generation", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("asset_kind IN ('agent', 'skill', 'mcp')", name="ck_run_asset_versions_kind"),
        sa.CheckConstraint("asset_scope IN ('system', 'project')", name="ck_run_asset_versions_scope"),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_run_asset_versions_checksum"),
        sa.CheckConstraint("catalog_generation >= 0", name="ck_run_asset_versions_generation"),
        sa.CheckConstraint("dependency_order >= 0", name="ck_run_asset_versions_order"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_run_asset_versions_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_run_asset_versions_private_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_run_asset_versions_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_run_asset_versions_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id", "owner_user_id", "run_id", "asset_kind", "dependency_order", name="pk_run_asset_versions"),
    )
    op.create_table(
        "run_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_run_events_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_run_events_private_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_run_events_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_run_events_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", "seq", name="uq_run_events_private_seq"),
        sa.UniqueConstraint("thread_id", "seq", name="uq_events_thread_seq"),
    )
    op.create_index("ix_events_run", "run_events", ["thread_id", "run_id", "seq"], unique=False)
    op.create_index("ix_events_thread_cat_seq", "run_events", ["thread_id", "category", "seq"], unique=False)
    op.create_index(op.f("ix_run_events_owner_user_id"), "run_events", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_run_events_project_id"), "run_events", ["project_id"], unique=False)
    op.create_index(
        "uq_run_events_stream_terminal",
        "run_events",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        unique=True,
        postgresql_where=sa.text("category = 'stream' AND event_type = 'stream.end'"),
    )
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("skill_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("workflow_status", sa.String(length=24), server_default="draft", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("frontmatter", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("compatibility", sa.String(length=255), nullable=True),
        sa.Column("secret_requirements", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("scan_decision", sa.String(length=24), nullable=False),
        sa.Column("scan_summary", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("supersedes_version_id", sa.Uuid(), nullable=True),
        sa.Column("payload_checksum", sa.CHAR(length=64), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("payload_checksum ~ '^[0-9a-f]{64}$'", name="ck_skill_versions_checksum"),
        sa.CheckConstraint("scan_decision IN ('allow', 'warn', 'block')", name="ck_skill_versions_scan_decision"),
        sa.CheckConstraint("workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')", name="ck_skill_versions_workflow_status"),
        sa.CheckConstraint("version_number >= 1", name="ck_skill_versions_number"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["skill_id"], ["skills.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "id", name="uq_skill_versions_asset_id"),
        sa.UniqueConstraint("skill_id", "version_number", name="uq_skill_versions_asset_number"),
    )
    op.create_table(
        "threads_meta",
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("assistant_id", sa.String(length=128), nullable=True),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_asset_id", sa.Uuid(), nullable=False),
        sa.Column("agent_scope", sa.String(length=16), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checkpoint_delete_status", sa.String(length=24), server_default="not_requested", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("agent_scope IN ('system', 'project')", name="ck_threads_meta_agent_scope"),
        sa.CheckConstraint("checkpoint_delete_status IN ('not_requested', 'pending', 'complete', 'retry_required')", name="ck_threads_meta_checkpoint_delete_status"),
        sa.CheckConstraint("version >= 1", name="ck_threads_meta_version"),
        sa.ForeignKeyConstraint(["agent_asset_id", "agent_scope"], ["agents.id", "agents.scope"], name="fk_threads_meta_agent_asset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_threads_meta_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_threads_meta_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_threads_meta_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("thread_id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "thread_id", name="uq_threads_meta_private_scope"),
    )
    op.create_index(op.f("ix_threads_meta_assistant_id"), "threads_meta", ["assistant_id"], unique=False)
    op.create_index(op.f("ix_threads_meta_owner_user_id"), "threads_meta", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_threads_meta_project_id"), "threads_meta", ["project_id"], unique=False)
    op.create_table(
        "user_project_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("namespace", sa.String(length=255), server_default="default", nullable=False),
        sa.Column("context_summary", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("namespace <> ''", name="ck_user_project_memories_namespace"),
        sa.CheckConstraint("version >= 1", name="ck_user_project_memories_version"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_user_project_memories_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_user_project_memories_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_user_project_memories_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "id", name="uq_user_project_memories_private_scope"),
        sa.UniqueConstraint("project_id", "owner_user_id", "namespace", name="uq_user_project_memories_namespace"),
    )
    op.create_table(
        "agent_version_mcp_refs",
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="ck_agent_version_mcp_refs_sort_order"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mcp_server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("agent_version_id", "mcp_server_version_id"),
    )
    op.create_table(
        "agent_version_skill_refs",
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("sort_order", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("sort_order >= 0", name="ck_agent_version_skill_refs_sort_order"),
        sa.ForeignKeyConstraint(["agent_version_id"], ["agent_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("agent_version_id", "skill_version_id"),
    )
    op.create_table(
        "channel_conversations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("external_conversation_id", sa.String(length=128), nullable=False),
        sa.Column("external_topic_id", sa.String(length=128), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["channel_connections.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_channel_conversations_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "connection_id"], ["channel_connections.project_id", "channel_connections.owner_user_id", "channel_connections.id"], name="fk_channel_conversations_private_connection", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_channel_conversations_private_thread", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_channel_conversations_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_channel_conversations_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "external_conversation_id", "external_topic_id", name="uq_channel_conversation_connection_external"),
    )
    op.create_index(op.f("ix_channel_conversations_connection_id"), "channel_conversations", ["connection_id"], unique=False)
    op.create_index(op.f("ix_channel_conversations_owner_user_id"), "channel_conversations", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_channel_conversations_project_id"), "channel_conversations", ["project_id"], unique=False)
    op.create_index(op.f("ix_channel_conversations_provider"), "channel_conversations", ["provider"], unique=False)
    op.create_index(op.f("ix_channel_conversations_thread_id"), "channel_conversations", ["thread_id"], unique=False)
    op.create_table(
        "channel_credentials",
        sa.Column("connection_id", sa.String(length=64), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=True),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encrypted_extra_json", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["channel_connections.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("connection_id"),
    )
    op.create_table(
        "credential_envelopes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("envelope_generation", sa.BigInteger(), nullable=False),
        sa.Column("key_id", sa.String(length=255), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("rotated_from_envelope_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("envelope_generation >= 1", name="ck_credential_envelopes_generation"),
        sa.CheckConstraint("octet_length(ciphertext) >= 16", name="ck_credential_envelopes_ciphertext_size"),
        sa.CheckConstraint("octet_length(nonce) = 12", name="ck_credential_envelopes_nonce_size"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["rotated_from_envelope_id"], ["credential_envelopes.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_version_id", "envelope_generation", name="uq_credential_envelopes_version_generation"),
    )
    op.create_index("uq_credential_envelopes_active_version", "credential_envelopes", ["credential_version_id"], unique=True, postgresql_where=sa.text("is_active"))
    op.create_table(
        "files",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("logical_path", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), server_default="application/octet-stream", nullable=False),
        sa.Column("size", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="staging", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_run_id", sa.String(length=64), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("source_file_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("kind IN ('upload', 'workspace', 'output')", name="ck_files_kind"),
        sa.CheckConstraint("logical_path <> '' AND left(logical_path, 1) <> '/' AND logical_path !~ '(^|/)\\.\\.(/|$)' AND logical_path !~ '^[A-Za-z]:'", name="ck_files_logical_path"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_files_sha256"),
        sa.CheckConstraint("source_file_id IS NULL OR kind = 'workspace'", name="ck_files_source_kind"),
        sa.CheckConstraint("status IN ('staging', 'ready', 'deleted')", name="ck_files_status"),
        sa.CheckConstraint("size >= 0", name="ck_files_size"),
        sa.CheckConstraint("source_file_id IS NULL OR source_file_id <> id", name="ck_files_source_not_self"),
        sa.CheckConstraint("version >= 1", name="ck_files_version"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_files_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "created_by_run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_files_created_by_private_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "source_file_id"], ["files.project_id", "files.owner_user_id", "files.thread_id", "files.id"], name="fk_files_private_source", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_files_private_thread", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_files_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_files_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "thread_id", "id", name="uq_files_private_scope"),
    )
    op.create_index("uq_files_active_logical_path", "files", ["project_id", "owner_user_id", "thread_id", "logical_path"], unique=True, postgresql_where=sa.text("status != 'deleted'"))
    op.create_table(
        "mcp_version_credential_slots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=63), nullable=False),
        sa.Column("purpose", sa.Text(), server_default="", nullable=False),
        sa.Column("payload_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("required", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.ForeignKeyConstraint(["mcp_server_version_id"], ["mcp_server_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("mcp_server_version_id", "id", name="uq_mcp_credential_slots_version_id"),
        sa.UniqueConstraint("mcp_server_version_id", "name", name="uq_mcp_credential_slots_version_name"),
    )
    op.create_table(
        "project_system_agent_bindings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("system_agent_id", sa.Uuid(), nullable=False),
        sa.Column("system_asset_scope", sa.String(length=16), server_default="system", nullable=False),
        sa.Column("agent_version_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_agent_bindings_system_scope"),
        sa.CheckConstraint("version >= 1", name="ck_project_system_agent_bindings_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["system_agent_id", "agent_version_id"], ["agent_versions.agent_id", "agent_versions.id"], name="fk_project_system_agent_bindings_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["system_agent_id", "system_asset_scope"], ["agents.id", "agents.scope"], name="fk_project_system_agent_bindings_system_asset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("project_id", "system_agent_id"),
    )
    op.create_table(
        "project_system_mcp_bindings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("system_mcp_server_id", sa.Uuid(), nullable=False),
        sa.Column("system_asset_scope", sa.String(length=16), server_default="system", nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_mcp_bindings_system_scope"),
        sa.CheckConstraint("version >= 1", name="ck_project_system_mcp_bindings_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["system_mcp_server_id", "mcp_server_version_id"], ["mcp_server_versions.mcp_server_id", "mcp_server_versions.id"], name="fk_project_system_mcp_bindings_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["system_mcp_server_id", "system_asset_scope"], ["mcp_servers.id", "mcp_servers.scope"], name="fk_project_system_mcp_bindings_system_asset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("project_id", "system_mcp_server_id"),
    )
    op.create_table(
        "project_system_skill_bindings",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("system_skill_id", sa.Uuid(), nullable=False),
        sa.Column("system_asset_scope", sa.String(length=16), server_default="system", nullable=False),
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("updated_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("system_asset_scope = 'system'", name="ck_project_system_skill_bindings_system_scope"),
        sa.CheckConstraint("version >= 1", name="ck_project_system_skill_bindings_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["system_skill_id", "skill_version_id"], ["skill_versions.skill_id", "skill_versions.id"], name="fk_project_system_skill_bindings_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["system_skill_id", "system_asset_scope"], ["skills.id", "skills.scope"], name="fk_project_system_skill_bindings_system_asset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("project_id", "system_skill_id"),
    )
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("context_mode", sa.String(length=32), nullable=False),
        sa.Column("agent_asset_id", sa.Uuid(), nullable=False),
        sa.Column("agent_scope", sa.String(length=16), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("schedule_type", sa.String(length=16), nullable=False),
        sa.Column("schedule_spec", sa.JSON(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("overlap_policy", sa.String(length=16), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outcome", sa.String(length=24), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("run_count", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("(context_mode = 'reuse_thread' AND thread_id IS NOT NULL) OR (context_mode = 'fresh_thread_per_run' AND thread_id IS NULL)", name="ck_scheduled_tasks_thread_mode"),
        sa.CheckConstraint("agent_scope IN ('system', 'project')", name="ck_scheduled_tasks_agent_scope"),
        sa.CheckConstraint("context_mode IN ('fresh_thread_per_run', 'reuse_thread')", name="ck_scheduled_tasks_context_mode"),
        sa.CheckConstraint("last_outcome IS NULL OR last_outcome IN ('success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')", name="ck_scheduled_tasks_last_outcome"),
        sa.CheckConstraint("overlap_policy = 'skip'", name="ck_scheduled_tasks_overlap_policy"),
        sa.CheckConstraint("schedule_type IN ('once', 'cron')", name="ck_scheduled_tasks_schedule_type"),
        sa.CheckConstraint("status IN ('enabled', 'paused', 'completed', 'failed', 'cancelled')", name="ck_scheduled_tasks_status"),
        sa.CheckConstraint("run_count >= 0", name="ck_scheduled_tasks_run_count"),
        sa.CheckConstraint("version >= 1", name="ck_scheduled_tasks_version"),
        sa.ForeignKeyConstraint(["agent_asset_id", "agent_scope"], ["agents.id", "agents.scope"], name="fk_scheduled_tasks_agent_asset", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_scheduled_tasks_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_scheduled_tasks_private_thread", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_scheduled_tasks_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_scheduled_tasks_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "id", name="uq_scheduled_tasks_private_scope"),
    )
    op.create_index(op.f("ix_scheduled_tasks_owner_user_id"), "scheduled_tasks", ["owner_user_id"], unique=False)
    op.create_index(op.f("ix_scheduled_tasks_project_id"), "scheduled_tasks", ["project_id"], unique=False)
    op.create_table(
        "skill_version_files",
        sa.Column("skill_version_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.CheckConstraint("path <> '' AND path !~ '(^/|(^|/)\\.\\.(/|$))'", name="ck_skill_version_files_safe_path"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_skill_version_files_sha256"),
        sa.CheckConstraint("size_bytes = octet_length(content)", name="ck_skill_version_files_content_size"),
        sa.CheckConstraint("size_bytes >= 0 AND size_bytes <= 104857600", name="ck_skill_version_files_size"),
        sa.ForeignKeyConstraint(["skill_version_id"], ["skill_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("skill_version_id", "path"),
    )
    op.create_table(
        "thread_event_sequences",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("high_watermark", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("high_watermark >= 0", name="ck_thread_event_sequences_high_watermark"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_thread_event_sequences_thread", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id", "owner_user_id", "thread_id"),
    )
    op.create_table(
        "user_project_memory_facts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_thread_id", sa.String(length=64), nullable=True),
        sa.Column("source_run_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("content <> ''", name="ck_user_project_memory_facts_content"),
        sa.CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_user_project_memory_facts_confidence"),
        sa.CheckConstraint("source_run_id IS NULL OR source_thread_id IS NOT NULL", name="ck_user_project_memory_facts_source"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_user_project_memory_facts_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "memory_id"], ["user_project_memories.project_id", "user_project_memories.owner_user_id", "user_project_memories.id"], name="fk_user_project_memory_facts_memory", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "source_thread_id", "source_run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_user_project_memory_facts_source_run", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "owner_user_id", "source_thread_id"], ["threads_meta.project_id", "threads_meta.owner_user_id", "threads_meta.thread_id"], name="fk_user_project_memory_facts_source_thread", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_user_project_memory_facts_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_user_project_memory_facts_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.String(length=256), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("artifact_metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_artifacts_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "file_id"], ["files.project_id", "files.owner_user_id", "files.thread_id", "files.id"], name="fk_artifacts_private_file", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_artifacts_private_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_artifacts_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_artifacts_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "owner_user_id", "thread_id", "run_id", "id", name="uq_artifacts_private_scope"),
    )
    op.create_index("ix_artifacts_private_active", "artifacts", ["project_id", "owner_user_id", "thread_id", "created_at"], unique=False, postgresql_where=sa.text("deleted_at IS NULL"))
    op.create_table(
        "credential_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("mcp_server_version_id", sa.Uuid(), nullable=False),
        sa.Column("credential_slot_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by_user_id", sa.String(length=36), nullable=True),
        sa.CheckConstraint("status IN ('active', 'revoked')", name="ck_credential_grants_status"),
        sa.CheckConstraint("version >= 1", name="ck_credential_grants_version"),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mcp_server_version_id", "credential_slot_id"], ["mcp_version_credential_slots.mcp_server_version_id", "mcp_version_credential_slots.id"], name="fk_credential_grants_slot_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["revoked_by_user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_credential_grants_active_slot", "credential_grants", ["mcp_server_version_id", "credential_slot_id"], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table(
        "file_chunks",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.CHAR(length=64), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_file_chunks_sha256"),
        sa.CheckConstraint("chunk_index >= 0", name="ck_file_chunks_index"),
        sa.CheckConstraint("size = octet_length(content)", name="ck_file_chunks_content_size"),
        sa.CheckConstraint("size > 0 AND size <= 1048576", name="ck_file_chunks_bounded_size"),
        sa.CheckConstraint("size >= 0", name="ck_file_chunks_size"),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], name="fk_file_chunks_file_id_files", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("file_id", "chunk_index"),
    )
    op.create_table(
        "run_mcp_grant_snapshots",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("mcp_version_id", sa.Uuid(), nullable=False),
        sa.Column("credential_slot_id", sa.Uuid(), nullable=False),
        sa.Column("credential_grant_id", sa.Uuid(), nullable=False),
        sa.Column("credential_version_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["credential_grant_id"], ["credential_grants.id"], name="fk_run_mcp_grant_snapshots_grant", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["credential_slot_id"], ["mcp_version_credential_slots.id"], name="fk_run_mcp_grant_snapshots_slot", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["credential_version_id"], ["credential_versions.id"], name="fk_run_mcp_grant_snapshots_credential_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["mcp_version_id"], ["mcp_server_versions.id"], name="fk_run_mcp_grant_snapshots_mcp_version", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], name="fk_run_mcp_grant_snapshots_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id", "thread_id", "run_id"], ["runs.project_id", "runs.owner_user_id", "runs.thread_id", "runs.run_id"], name="fk_run_mcp_grant_snapshots_private_run", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id", "owner_user_id"], ["project_memberships.project_id", "project_memberships.user_id"], name="fk_run_mcp_grant_snapshots_project_membership", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], name="fk_run_mcp_grant_snapshots_project", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("project_id", "owner_user_id", "run_id", "mcp_version_id", "credential_slot_id", name="pk_run_mcp_grant_snapshots"),
    )
    op.create_foreign_key("fk_dead_jobs_job", "dead_jobs", "jobs", ["job_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_dead_jobs_project", "dead_jobs", "projects", ["project_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_jobs_lease_worker", "jobs", "worker_nodes", ["lease_owner_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key(
        "fk_jobs_predecessor_dead_job",
        "jobs",
        "dead_jobs",
        ["predecessor_dead_job_id"],
        ["job_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_jobs_automation_occurrence",
        "jobs",
        "scheduled_task_runs",
        ["project_id", "owner_user_id", "automation_occurrence_id"],
        ["project_id", "owner_user_id", "id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_jobs_private_run",
        "jobs",
        "runs",
        ["project_id", "owner_user_id", "run_id"],
        ["project_id", "owner_user_id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key("fk_jobs_project", "jobs", "projects", ["project_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_runs_job", "runs", "jobs", ["job_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key("fk_runs_owner", "runs", "users", ["owner_user_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_runs_private_thread",
        "runs",
        "threads_meta",
        ["project_id", "owner_user_id", "thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_runs_project_membership",
        "runs",
        "project_memberships",
        ["project_id", "owner_user_id"],
        ["project_id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key("fk_runs_project", "runs", "projects", ["project_id"], ["id"], ondelete="RESTRICT")
    op.create_foreign_key(
        "fk_scheduled_task_runs_job",
        "scheduled_task_runs",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_task_runs_owner",
        "scheduled_task_runs",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_task_runs_task",
        "scheduled_task_runs",
        "scheduled_tasks",
        ["project_id", "owner_user_id", "task_id"],
        ["project_id", "owner_user_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_scheduled_task_runs_private_run",
        "scheduled_task_runs",
        "runs",
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ["project_id", "owner_user_id", "thread_id", "run_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_task_runs_private_thread",
        "scheduled_task_runs",
        "threads_meta",
        ["project_id", "owner_user_id", "thread_id"],
        ["project_id", "owner_user_id", "thread_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_scheduled_task_runs_project",
        "scheduled_task_runs",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_agents_current_published_version",
        "agents",
        "agent_versions",
        ["id", "current_published_version_id"],
        ["agent_id", "id"],
    )
    op.create_foreign_key(
        "fk_credentials_current_version",
        "credentials",
        "credential_versions",
        ["id", "current_version_id"],
        ["credential_id", "id"],
    )
    op.create_foreign_key(
        "fk_mcp_servers_current_published_version",
        "mcp_servers",
        "mcp_server_versions",
        ["id", "current_published_version_id"],
        ["mcp_server_id", "id"],
    )
    op.create_foreign_key(
        "fk_skills_current_published_version",
        "skills",
        "skill_versions",
        ["id", "current_published_version_id"],
        ["skill_id", "id"],
    )
    # ### end Alembic commands ###
    for statement in _FINAL_POSTGRES_DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError("M7 baseline downgrade is unsupported; recreate a new database")
