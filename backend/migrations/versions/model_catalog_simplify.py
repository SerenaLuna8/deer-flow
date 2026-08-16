"""Use model UUID references and remove obsolete System Model fields.

Revision ID: model_catalog_simplify
Revises: approval_output_delivery
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "model_catalog_simplify"
down_revision = "approval_output_delivery"
branch_labels = None
depends_on = None


def _install_reference_migration_helpers() -> None:
    # The application checksum contracts use UTF-8, recursively sorted JSON
    # keys, and no insignificant whitespace. Keep a migration-local copy of
    # that algorithm so this historical revision never imports current app
    # code. PostgreSQL's core sha256(bytea) function needs no extension.
    op.execute(
        """
        CREATE FUNCTION pg_temp.actweave_canonical_jsonb(payload jsonb)
        RETURNS text
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        AS $$
        DECLARE
            payload_kind text := jsonb_typeof(payload);
            canonical text;
        BEGIN
            IF payload_kind = 'object' THEN
                SELECT '{' || COALESCE(
                    string_agg(
                        to_jsonb(entry.key)::text || ':' ||
                        pg_temp.actweave_canonical_jsonb(entry.value),
                        ',' ORDER BY entry.key COLLATE "C"
                    ),
                    ''
                ) || '}'
                  INTO canonical
                  FROM jsonb_each(payload) AS entry;
            ELSIF payload_kind = 'array' THEN
                SELECT '[' || COALESCE(
                    string_agg(
                        pg_temp.actweave_canonical_jsonb(entry.value),
                        ',' ORDER BY entry.ordinality
                    ),
                    ''
                ) || ']'
                  INTO canonical
                  FROM jsonb_array_elements(payload)
                       WITH ORDINALITY AS entry(value, ordinality);
            ELSE
                canonical := payload::text;
            END IF;
            RETURN canonical;
        END;
        $$
        """,
    )
    op.execute(
        """
        CREATE FUNCTION pg_temp.actweave_jsonb_sha256(payload jsonb)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        PARALLEL SAFE
        AS $$
            SELECT encode(
                sha256(
                    convert_to(
                        pg_temp.actweave_canonical_jsonb(payload),
                        'UTF8'
                    )
                ),
                'hex'
            )
        $$
        """,
    )
    op.execute(
        """
        CREATE TEMP TABLE actweave_model_ref_map
        ON COMMIT DROP
        AS
        SELECT logical_name, id::text AS model_ref
          FROM system_model_configs
        """,
    )
    op.execute(
        "CREATE UNIQUE INDEX actweave_model_ref_map_logical_name ON actweave_model_ref_map (logical_name)",
    )
    op.execute(
        "CREATE UNIQUE INDEX actweave_model_ref_map_model_ref ON actweave_model_ref_map (model_ref)",
    )
    op.execute(
        """
        CREATE FUNCTION pg_temp.actweave_exact_model_ref(
            source_ref text,
            allow_default boolean
        )
        RETURNS text
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            matched_count bigint;
            mapped_ref text;
        BEGIN
            IF source_ref IS NULL THEN
                RETURN NULL;
            END IF;
            IF allow_default AND source_ref = 'default' THEN
                RETURN source_ref;
            END IF;

            SELECT count(*), min(candidate.model_ref)
              INTO matched_count, mapped_ref
              FROM pg_temp.actweave_model_ref_map AS candidate
             WHERE candidate.logical_name = source_ref
                OR candidate.model_ref = source_ref;
            IF matched_count <> 1 THEN
                RAISE EXCEPTION
                    'model catalog reference cannot be migrated: %',
                    source_ref
                    USING ERRCODE = '23514';
            END IF;
            RETURN mapped_ref;
        END;
        $$
        """,
    )


def _migrate_agent_references() -> None:
    op.execute(
        """
        CREATE FUNCTION pg_temp.actweave_agent_payload_document(
            agent_version_id uuid,
            schema_version integer,
            description text,
            agents_instructions text,
            soul text,
            identity text,
            user_context text,
            model_ref text,
            model_settings jsonb,
            tool_groups jsonb
        )
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        STRICT
        AS $$
        DECLARE
            skill_version_ids jsonb;
            mcp_version_ids jsonb;
            document jsonb;
        BEGIN
            SELECT COALESCE(
                       jsonb_agg(
                           to_jsonb(reference.skill_version_id::text)
                           ORDER BY reference.sort_order
                       ),
                       '[]'::jsonb
                   )
              INTO skill_version_ids
              FROM agent_version_skill_refs AS reference
             WHERE reference.agent_version_id =
                   actweave_agent_payload_document.agent_version_id;

            SELECT COALESCE(
                       jsonb_agg(
                           to_jsonb(reference.mcp_server_version_id::text)
                           ORDER BY reference.sort_order
                       ),
                       '[]'::jsonb
                   )
              INTO mcp_version_ids
              FROM agent_version_mcp_refs AS reference
             WHERE reference.agent_version_id =
                   actweave_agent_payload_document.agent_version_id;

            document := jsonb_build_object(
                'description', description,
                'mcp_version_ids', mcp_version_ids,
                'model_ref', model_ref,
                'skill_version_ids', skill_version_ids,
                'soul', soul,
                'tool_groups', tool_groups
            );
            IF schema_version IN (2, 3) THEN
                document := document || jsonb_build_object(
                    'agents_instructions', agents_instructions,
                    'identity', identity,
                    'user_context', user_context
                );
            END IF;
            IF schema_version = 3 THEN
                document := document || jsonb_build_object(
                    'model_settings', model_settings
                );
            ELSIF schema_version NOT IN (1, 2) THEN
                RAISE EXCEPTION 'unsupported Agent payload schema version: %',
                    schema_version
                    USING ERRCODE = '23514';
            END IF;
            RETURN document;
        END;
        $$
        """,
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM agent_version_skill_refs
                 GROUP BY agent_version_id, sort_order
                HAVING count(*) > 1
            ) OR EXISTS (
                SELECT 1
                  FROM agent_version_mcp_refs
                 GROUP BY agent_version_id, sort_order
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    'Agent dependency order is ambiguous'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM agent_versions AS version
                 WHERE version.payload_checksum <>
                       pg_temp.actweave_jsonb_sha256(
                           pg_temp.actweave_agent_payload_document(
                               version.id,
                               version.payload_schema_version,
                               version.description,
                               version.agents_instructions,
                               version.soul,
                               version.identity,
                               version.user_context,
                               version.model_ref,
                               version.model_settings,
                               version.tool_groups
                           )
                       )
            ) THEN
                RAISE EXCEPTION
                    'Agent payload checksum is invalid before model reference migration'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM run_asset_versions AS snapshot
                  LEFT JOIN agent_versions AS version
                    ON version.id = snapshot.version_id
                 WHERE snapshot.asset_kind = 'agent'
                   AND (
                       version.id IS NULL
                       OR version.agent_id <> snapshot.asset_id
                       OR version.payload_checksum <>
                          snapshot.payload_checksum
                   )
            ) THEN
                RAISE EXCEPTION
                    'Run Agent snapshot is invalid before model reference migration'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """,
    )
    op.execute(
        """
        CREATE TEMP TABLE actweave_agent_model_updates
        ON COMMIT DROP
        AS
        SELECT version.id,
               mapped.model_ref,
               pg_temp.actweave_jsonb_sha256(
                   pg_temp.actweave_agent_payload_document(
                       version.id,
                       version.payload_schema_version,
                       version.description,
                       version.agents_instructions,
                       version.soul,
                       version.identity,
                       version.user_context,
                       mapped.model_ref,
                       version.model_settings,
                       version.tool_groups
                   )
               ) AS payload_checksum
          FROM agent_versions AS version
          CROSS JOIN LATERAL (
              SELECT pg_temp.actweave_exact_model_ref(
                         version.model_ref,
                         true
                     ) AS model_ref
          ) AS mapped
        """,
    )
    op.execute(
        "ALTER TABLE agent_versions DISABLE TRIGGER trg_agent_versions_immutable",
    )
    op.execute(
        """
        UPDATE agent_versions AS version
           SET model_ref = migration.model_ref,
               payload_checksum = migration.payload_checksum
          FROM pg_temp.actweave_agent_model_updates AS migration
         WHERE version.id = migration.id
           AND (
               version.model_ref IS DISTINCT FROM migration.model_ref
               OR version.payload_checksum IS DISTINCT FROM
                  migration.payload_checksum
           )
        """,
    )
    op.execute(
        """
        UPDATE run_asset_versions AS snapshot
           SET payload_checksum = migration.payload_checksum
          FROM pg_temp.actweave_agent_model_updates AS migration
         WHERE snapshot.asset_kind = 'agent'
           AND snapshot.version_id = migration.id
           AND snapshot.payload_checksum IS DISTINCT FROM
               migration.payload_checksum
        """,
    )
    op.execute(
        "ALTER TABLE agent_versions ENABLE TRIGGER trg_agent_versions_immutable",
    )


def _migrate_agent_blueprints() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM agent_design_sessions
                 WHERE blueprint_json IS NOT NULL
                   AND (
                       jsonb_typeof(blueprint_json) <> 'object'
                       OR jsonb_typeof(blueprint_json -> 'model_ref')
                          IS DISTINCT FROM 'string'
                       OR blueprint_checksum IS DISTINCT FROM
                          pg_temp.actweave_jsonb_sha256(blueprint_json)
                   )
            ) THEN
                RAISE EXCEPTION
                    'Agent design blueprint is invalid before model reference migration'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """,
    )
    op.execute(
        """
        CREATE TEMP TABLE actweave_agent_blueprint_updates
        ON COMMIT DROP
        AS
        SELECT source.id,
               source.blueprint_json,
               pg_temp.actweave_jsonb_sha256(source.blueprint_json)
                   AS blueprint_checksum
          FROM (
              SELECT session.id,
                     jsonb_set(
                         session.blueprint_json,
                         '{model_ref}',
                         to_jsonb(
                             pg_temp.actweave_exact_model_ref(
                                 session.blueprint_json ->> 'model_ref',
                                 true
                             )
                         ),
                         false
                     ) AS blueprint_json
                FROM agent_design_sessions AS session
               WHERE session.blueprint_json IS NOT NULL
          ) AS source
        """,
    )
    op.execute(
        "ALTER TABLE agent_design_sessions DISABLE TRIGGER trg_agent_design_sessions_updated_at",
    )
    op.execute(
        """
        UPDATE agent_design_sessions AS session
           SET blueprint_json = migration.blueprint_json,
               blueprint_checksum = migration.blueprint_checksum
          FROM pg_temp.actweave_agent_blueprint_updates AS migration
         WHERE session.id = migration.id
           AND (
               session.blueprint_json IS DISTINCT FROM
                   migration.blueprint_json
               OR session.blueprint_checksum IS DISTINCT FROM
                  migration.blueprint_checksum
           )
        """,
    )
    op.execute(
        "ALTER TABLE agent_design_sessions ENABLE TRIGGER trg_agent_design_sessions_updated_at",
    )


def _migrate_runtime_policy_references() -> None:
    op.execute(
        """
        CREATE FUNCTION pg_temp.actweave_runtime_policy_model_refs(payload jsonb)
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        STRICT
        AS $$
        DECLARE
            migrated jsonb := payload;
            model_path text[];
            node jsonb;
        BEGIN
            FOREACH model_path SLICE 1 IN ARRAY ARRAY[
                ARRAY['title', 'model_name'],
                ARRAY['input_polish', 'model_name'],
                ARRAY['summarization', 'model_name'],
                ARRAY['memory', 'model_name'],
                ARRAY['vision_bridge', 'model_name']
            ] LOOP
                node := migrated #> model_path;
                IF node IS NULL OR jsonb_typeof(node) = 'null' THEN
                    CONTINUE;
                END IF;
                IF jsonb_typeof(node) <> 'string' THEN
                    RAISE EXCEPTION
                        'runtime policy model reference is not a string at path %',
                        model_path
                        USING ERRCODE = '23514';
                END IF;
                migrated := jsonb_set(
                    migrated,
                    model_path,
                    to_jsonb(
                        pg_temp.actweave_exact_model_ref(
                            migrated #>> model_path,
                            false
                        )
                    ),
                    false
                );
            END LOOP;
            RETURN migrated;
        END;
        $$
        """,
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM system_runtime_policy_versions AS version
                 WHERE version.payload_checksum IS DISTINCT FROM
                       pg_temp.actweave_jsonb_sha256(version.value)
            ) THEN
                RAISE EXCEPTION
                    'runtime policy checksum is invalid before model reference migration'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM run_runtime_policy_snapshots AS snapshot
                  LEFT JOIN system_runtime_policy_versions AS version
                    ON version.section = snapshot.section
                   AND version.id = snapshot.policy_version_id
                 WHERE version.id IS NULL
                    OR version.schema_version <> snapshot.schema_version
                    OR version.payload_checksum <> snapshot.payload_checksum
            ) THEN
                RAISE EXCEPTION
                    'Run runtime policy snapshot is invalid before model reference migration'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """,
    )
    op.execute(
        """
        CREATE TEMP TABLE actweave_runtime_policy_updates
        ON COMMIT DROP
        AS
        SELECT source.id,
               source.value,
               pg_temp.actweave_jsonb_sha256(source.value)
                   AS payload_checksum
          FROM (
              SELECT version.id,
                     pg_temp.actweave_runtime_policy_model_refs(
                         version.value
                     ) AS value
                FROM system_runtime_policy_versions AS version
               WHERE version.section = 'agent_runtime'
          ) AS source
        """,
    )
    op.drop_constraint(
        "fk_run_runtime_policy_snapshots_exact_policy",
        "run_runtime_policy_snapshots",
        type_="foreignkey",
    )
    op.execute(
        "ALTER TABLE system_runtime_policy_versions DISABLE TRIGGER trg_system_runtime_policy_versions_immutable",
    )
    op.execute(
        "ALTER TABLE run_runtime_policy_snapshots DISABLE TRIGGER trg_run_runtime_policy_snapshots_immutable",
    )
    op.execute(
        """
        UPDATE system_runtime_policy_versions AS version
           SET value = migration.value,
               payload_checksum = migration.payload_checksum
          FROM pg_temp.actweave_runtime_policy_updates AS migration
         WHERE version.id = migration.id
           AND (
               version.value IS DISTINCT FROM migration.value
               OR version.payload_checksum IS DISTINCT FROM
                  migration.payload_checksum
           )
        """,
    )
    op.execute(
        """
        UPDATE run_runtime_policy_snapshots AS snapshot
           SET payload_checksum = migration.payload_checksum
          FROM pg_temp.actweave_runtime_policy_updates AS migration
         WHERE snapshot.policy_version_id = migration.id
           AND snapshot.payload_checksum IS DISTINCT FROM
               migration.payload_checksum
        """,
    )
    op.create_foreign_key(
        "fk_run_runtime_policy_snapshots_exact_policy",
        "run_runtime_policy_snapshots",
        "system_runtime_policy_versions",
        ["section", "policy_version_id", "schema_version", "payload_checksum"],
        ["section", "id", "schema_version", "payload_checksum"],
        ondelete="RESTRICT",
    )
    op.execute(
        "ALTER TABLE run_runtime_policy_snapshots ENABLE TRIGGER trg_run_runtime_policy_snapshots_immutable",
    )
    op.execute(
        "ALTER TABLE system_runtime_policy_versions ENABLE TRIGGER trg_system_runtime_policy_versions_immutable",
    )


def _migrate_run_references() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                  FROM run_model_config_snapshots AS snapshot
                  LEFT JOIN pg_temp.actweave_model_ref_map AS model
                    ON model.model_ref = snapshot.model_config_id::text
                 WHERE model.model_ref IS NULL
                    OR snapshot.logical_name <> model.logical_name
            ) THEN
                RAISE EXCEPTION
                    'Run model snapshot identity is invalid before logical name removal'
                    USING ERRCODE = '23514';
            END IF;

            IF EXISTS (
                SELECT 1
                  FROM run_model_config_snapshots AS snapshot
                  JOIN runs AS run
                    ON run.project_id = snapshot.project_id
                   AND run.owner_user_id = snapshot.owner_user_id
                   AND run.thread_id = snapshot.thread_id
                   AND run.run_id = snapshot.run_id
                 WHERE snapshot.purpose = 'lead'
                   AND run.model_name IS NOT NULL
                   AND pg_temp.actweave_exact_model_ref(
                           run.model_name,
                           false
                       ) <> snapshot.model_config_id::text
            ) THEN
                RAISE EXCEPTION
                    'Run model reference does not match its lead snapshot'
                    USING ERRCODE = '23514';
            END IF;
        END;
        $$
        """,
    )
    op.execute(
        """
        CREATE FUNCTION pg_temp.actweave_run_execution_profile(
            p_payload jsonb,
            p_model_ref text
        )
        RETURNS jsonb
        LANGUAGE plpgsql
        IMMUTABLE
        STRICT
        AS $$
        DECLARE
            profile jsonb;
            requested_model jsonb;
            effective_model jsonb;
            migrated jsonb := p_payload;
        BEGIN
            IF NOT (p_payload ? '__run_execution_profile') THEN
                RETURN p_payload;
            END IF;
            profile := p_payload -> '__run_execution_profile';
            requested_model := profile #> '{requested,model_name}';
            effective_model := profile #> '{effective,model_name}';
            IF jsonb_typeof(profile) <> 'object'
               OR jsonb_typeof(profile -> 'requested') <> 'object'
               OR jsonb_typeof(profile -> 'effective') <> 'object'
               OR requested_model IS NULL
               OR jsonb_typeof(requested_model) NOT IN ('string', 'null')
               OR effective_model IS NULL
               OR jsonb_typeof(effective_model) <> 'string' THEN
                RAISE EXCEPTION
                    'Run execution profile is invalid before model reference migration'
                    USING ERRCODE = '23514';
            END IF;
            IF pg_temp.actweave_exact_model_ref(
                    profile #>> '{effective,model_name}',
                    false
               ) <> p_model_ref THEN
                RAISE EXCEPTION
                    'Run effective model reference is inconsistent before migration'
                    USING ERRCODE = '23514';
            END IF;
            IF jsonb_typeof(requested_model) = 'string' THEN
                IF pg_temp.actweave_exact_model_ref(
                        profile #>> '{requested,model_name}',
                        false
                   ) <> p_model_ref THEN
                    RAISE EXCEPTION
                        'Run requested model reference is inconsistent before migration'
                        USING ERRCODE = '23514';
                END IF;
                migrated := jsonb_set(
                    migrated,
                    '{__run_execution_profile,requested,model_name}',
                    to_jsonb(p_model_ref),
                    false
                );
            END IF;
            RETURN jsonb_set(
                migrated,
                '{__run_execution_profile,effective,model_name}',
                to_jsonb(p_model_ref),
                false
            );
        END;
        $$
        """,
    )
    op.execute(
        """
        CREATE TEMP TABLE actweave_run_model_updates
        ON COMMIT DROP
        AS
        SELECT run.project_id,
               run.owner_user_id,
               run.thread_id,
               run.run_id,
               COALESCE(
                   snapshot.model_config_id::text,
                   pg_temp.actweave_exact_model_ref(run.model_name, false)
               ) AS model_ref
          FROM runs AS run
          LEFT JOIN run_model_config_snapshots AS snapshot
            ON snapshot.project_id = run.project_id
           AND snapshot.owner_user_id = run.owner_user_id
           AND snapshot.thread_id = run.thread_id
           AND snapshot.run_id = run.run_id
           AND snapshot.purpose = 'lead'
         WHERE snapshot.model_config_id IS NOT NULL
            OR run.model_name IS NOT NULL
        """,
    )
    op.execute(
        "ALTER TABLE runs DISABLE TRIGGER trg_runs_updated_at",
    )
    op.execute(
        """
        UPDATE runs AS run
           SET model_name = migration.model_ref,
               kwargs_json =
                   pg_temp.actweave_run_execution_profile(
                       run.kwargs_json::jsonb,
                       migration.model_ref
                   )::json
          FROM pg_temp.actweave_run_model_updates AS migration
         WHERE run.project_id = migration.project_id
           AND run.owner_user_id = migration.owner_user_id
           AND run.thread_id = migration.thread_id
           AND run.run_id = migration.run_id
        """,
    )
    op.execute(
        "ALTER TABLE runs ENABLE TRIGGER trg_runs_updated_at",
    )


def upgrade() -> None:
    _install_reference_migration_helpers()
    _migrate_agent_references()
    _migrate_agent_blueprints()
    _migrate_runtime_policy_references()
    _migrate_run_references()

    op.drop_index(
        "uq_system_model_configs_logical_name",
        table_name="system_model_configs",
    )
    op.drop_index(
        "ix_system_model_configs_status_order",
        table_name="system_model_configs",
    )
    op.drop_constraint(
        "ck_system_model_configs_sort_order",
        "system_model_configs",
        type_="check",
    )
    op.drop_column("run_model_config_snapshots", "logical_name")
    op.drop_column("system_model_configs", "logical_name")
    op.drop_column("system_model_configs", "description")
    op.drop_column("system_model_configs", "sort_order")
    op.create_index(
        "ix_system_model_configs_status_created",
        "system_model_configs",
        ["status", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.execute(
        "COMMENT ON TABLE system_model_configs IS '保存系统模型配置的稳定标识、展示名称和当前版本指针。'",
    )


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade (D3); restore from the pre-upgrade backup instead",
    )
