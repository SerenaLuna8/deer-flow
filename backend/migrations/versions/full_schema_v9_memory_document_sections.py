"""Freeze database-configured section titles into Memory documents and Runs."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v9"
down_revision = "full_schema_v8"
branch_labels = None
depends_on = None

_DEFAULT_SECTIONS_JSON = """["用户偏好与协作方式","项目背景","长期约束与架构决策","当前仍有效的目标"]"""
_DEFAULT_POLICY_VALUE_JSON = """{"sections":["用户偏好与协作方式","项目背景","长期约束与架构决策","当前仍有效的目标"]}"""
_DEFAULT_POLICY_VERSION_ID = "b8639139-1f8c-5413-a8a8-e8c035aed0c8"
_DEFAULT_POLICY_CHECKSUM = "df0f23d20ab7052a19e74424843acc75e78e4f6a4f7610bdd23ceb5973c0eb13"


def upgrade() -> None:
    op.execute("ALTER TABLE system_runtime_policies DROP CONSTRAINT ck_system_runtime_policies_section")
    op.execute("ALTER TABLE system_runtime_policies ADD CONSTRAINT ck_system_runtime_policies_section CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas'))")
    op.execute("ALTER TABLE system_runtime_policy_versions DROP CONSTRAINT ck_system_runtime_policy_versions_section")
    op.execute("ALTER TABLE system_runtime_policy_versions ADD CONSTRAINT ck_system_runtime_policy_versions_section CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas'))")
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
                IF EXISTS (SELECT 1 FROM memory_documents) THEN
                    RAISE EXCEPTION 'Memory documents require a bootstrapped runtime policy catalog before v9';
                END IF;
                -- A schema-only database is valid during setup/parity. The normal
                -- setup bootstrap will seed all four sections after the chain.
                NULL;
            ELSIF policy_count = 3 AND version_count >= 3 THEN
                SELECT updated_by_user_id
                  INTO bootstrap_actor_id
                  FROM system_runtime_policies
                 WHERE section = 'agent_runtime';
                IF bootstrap_actor_id IS NULL THEN
                    RAISE EXCEPTION 'runtime policy catalog must be complete before v9';
                END IF;

                INSERT INTO system_runtime_policies (
                    section, current_version_id, revision, updated_by_user_id
                ) VALUES (
                    'memory_document', '{_DEFAULT_POLICY_VERSION_ID}'::uuid, 1,
                    bootstrap_actor_id
                );
                INSERT INTO system_runtime_policy_versions (
                    id, section, version_number, schema_version, value,
                    payload_checksum, supersedes_version_id, created_by_user_id
                ) VALUES (
                    '{_DEFAULT_POLICY_VERSION_ID}'::uuid, 'memory_document', 1, 2,
                    '{_DEFAULT_POLICY_VALUE_JSON}'::jsonb,
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
                RAISE EXCEPTION 'runtime policy catalog is incomplete before v9';
            END IF;
        END;
        $$"""
    )

    op.execute("ALTER TABLE memory_documents ADD COLUMN sections JSONB")
    op.execute("ALTER TABLE memory_documents ADD COLUMN sections_policy_section VARCHAR(32) DEFAULT 'memory_document' NOT NULL")
    op.execute("ALTER TABLE memory_documents ADD COLUMN sections_policy_version_id UUID")
    op.execute(
        f"""UPDATE memory_documents
               SET sections = '{_DEFAULT_SECTIONS_JSON}'::jsonb,
                   sections_policy_section = 'memory_document',
                   sections_policy_version_id = '{_DEFAULT_POLICY_VERSION_ID}'::uuid"""
    )
    op.execute("ALTER TABLE memory_documents ALTER COLUMN sections SET NOT NULL")
    op.execute("ALTER TABLE memory_documents ALTER COLUMN sections_policy_version_id SET NOT NULL")
    op.execute(
        "ALTER TABLE memory_documents ADD CONSTRAINT fk_memory_documents_sections_policy_version FOREIGN KEY (sections_policy_section, sections_policy_version_id) REFERENCES system_runtime_policy_versions (section, id) ON DELETE RESTRICT"
    )
    op.execute("ALTER TABLE memory_documents ADD CONSTRAINT ck_memory_documents_sections_policy_section CHECK (sections_policy_section = 'memory_document')")
    op.execute(
        """ALTER TABLE memory_documents
        ADD CONSTRAINT ck_memory_documents_sections
        CHECK (
            jsonb_typeof(sections) = 'array'
            AND jsonb_array_length(sections) BETWEEN 2 AND 8
            AND NOT jsonb_path_exists(
                sections,
                '$[*] ? (@.type() != "string")'
            )
        )"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION prevent_memory_document_sections_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.sections IS DISTINCT FROM OLD.sections
               OR NEW.sections_policy_section IS DISTINCT FROM OLD.sections_policy_section
               OR NEW.sections_policy_version_id IS DISTINCT FROM OLD.sections_policy_version_id THEN
                RAISE EXCEPTION 'Memory document sections and policy provenance are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER trg_memory_documents_sections_immutable
        BEFORE UPDATE ON memory_documents
        FOR EACH ROW EXECUTE FUNCTION prevent_memory_document_sections_mutation()"""
    )

    op.execute("ALTER TABLE run_memory_context_snapshots ADD COLUMN sections JSONB")
    op.execute(
        f"""UPDATE run_memory_context_snapshots
               SET sections = '{_DEFAULT_SECTIONS_JSON}'::jsonb"""
    )
    op.execute("ALTER TABLE run_memory_context_snapshots ALTER COLUMN sections SET NOT NULL")
    op.execute(
        """ALTER TABLE run_memory_context_snapshots
        ADD CONSTRAINT ck_run_memory_context_snapshots_sections
        CHECK (
            jsonb_typeof(sections) = 'array'
            AND jsonb_array_length(sections) BETWEEN 2 AND 8
            AND NOT jsonb_path_exists(
                sections,
                '$[*] ? (@.type() != "string")'
            )
        )"""
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION prevent_run_memory_snapshot_sections_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            IF NEW.sections IS DISTINCT FROM OLD.sections THEN
                RAISE EXCEPTION 'Run Memory snapshot sections are immutable'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql"""
    )
    op.execute(
        """CREATE TRIGGER trg_run_memory_context_snapshots_sections_immutable
        BEFORE UPDATE ON run_memory_context_snapshots
        FOR EACH ROW EXECUTE FUNCTION prevent_run_memory_snapshot_sections_mutation()"""
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
