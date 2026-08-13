"""Move Automation scheduler policy into the runtime-policy catalog."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v17"
down_revision = "full_schema_v16"
branch_labels = None
depends_on = None

_SECTION_CHECK = "section IN ('agent_runtime', 'auth', 'automations', 'memory_document', 'quotas')"
_DEFAULT_POLICY_VERSION_ID = "b83a268d-e534-50c5-80a3-61155aede852"
_DEFAULT_POLICY_CHECKSUM = "cd4eae7f36175c2eda25d142cb6d816becfa6d0984a3735a27f3d76465a1975f"


def upgrade() -> None:
    # Earlier chain revisions may have inserted deferred policy FKs in this
    # same Alembic transaction. Flush them before rewriting CHECK constraints.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE system_runtime_policies DROP CONSTRAINT ck_system_runtime_policies_section")
    op.execute(f"ALTER TABLE system_runtime_policies ADD CONSTRAINT ck_system_runtime_policies_section CHECK ({_SECTION_CHECK})")
    op.execute("ALTER TABLE system_runtime_policy_versions DROP CONSTRAINT ck_system_runtime_policy_versions_section")
    op.execute(f"ALTER TABLE system_runtime_policy_versions ADD CONSTRAINT ck_system_runtime_policy_versions_section CHECK ({_SECTION_CHECK})")
    # Restore deferred circular FKs so the automations seed can insert policy
    # and version rows in one statement block.
    op.execute("SET CONSTRAINTS ALL DEFERRED")
    op.execute(
        f"""DO $$
        DECLARE
            bootstrap_actor_id VARCHAR(36);
            policy_count BIGINT;
            version_count BIGINT;
        BEGIN
            SELECT count(*) INTO policy_count FROM system_runtime_policies;
            SELECT count(*) INTO version_count FROM system_runtime_policy_versions;
            IF policy_count = 0 AND version_count = 0 THEN
                NULL;
            ELSIF policy_count = 4 AND version_count >= 4 THEN
                SELECT updated_by_user_id
                  INTO bootstrap_actor_id
                  FROM system_runtime_policies
                 WHERE section = 'agent_runtime';
                IF bootstrap_actor_id IS NULL THEN
                    RAISE EXCEPTION 'runtime policy catalog must be complete before v17';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM system_runtime_policies WHERE section = 'automations'
                ) THEN
                    RAISE EXCEPTION 'automations runtime policy already exists before v17';
                END IF;

                INSERT INTO system_runtime_policies (
                    section, current_version_id, revision, updated_by_user_id
                ) VALUES (
                    'automations', '{_DEFAULT_POLICY_VERSION_ID}'::uuid, 1,
                    bootstrap_actor_id
                );
                INSERT INTO system_runtime_policy_versions (
                    id, section, version_number, schema_version, value,
                    payload_checksum, supersedes_version_id, created_by_user_id
                ) VALUES (
                    '{_DEFAULT_POLICY_VERSION_ID}'::uuid, 'automations', 1, 2,
                    jsonb_build_object(
                        'enabled', true,
                        'max_concurrent_runs', 3,
                        'min_once_delay_seconds', 60,
                        'poll_interval_seconds', 5
                    ),
                    '{_DEFAULT_POLICY_CHECKSUM}', NULL, bootstrap_actor_id
                );
                UPDATE system_runtime_policy_catalog_state
                   SET revision = revision + 1,
                       updated_by_user_id = bootstrap_actor_id,
                       updated_at = now()
                 WHERE id = 1;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'runtime policy catalog state is missing';
                END IF;
            ELSE
                RAISE EXCEPTION 'runtime policy catalog is incomplete before v17';
            END IF;
        END;
        $$"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
