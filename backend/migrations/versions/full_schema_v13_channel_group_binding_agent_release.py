"""Normalize disabled group routing and release Agent tombstone references."""

from __future__ import annotations

from alembic import op

revision = "full_schema_v13"
down_revision = "full_schema_v12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE project_channel_group_bindings ALTER COLUMN agent_scope DROP NOT NULL, ALTER COLUMN agent_asset_id DROP NOT NULL")
    op.execute("ALTER TABLE project_channel_group_bindings DROP CONSTRAINT ck_project_channel_group_bindings_agent_scope")
    op.execute("ALTER TABLE project_channel_group_bindings ADD CONSTRAINT ck_project_channel_group_bindings_agent_scope CHECK (agent_scope IS NULL OR agent_scope IN ('system', 'project'))")

    # Legacy tombstones predate the nullable Agent-pair contract. Legacy live
    # disabled bindings can also contain a connected row or stale Agent routing
    # because v12 project resume and disabled-Agent updates did not share the
    # v13 lazy-inbound boundary. Normalize both states while retaining stable
    # binding, principal, membership, user, connection, and Thread identifiers.
    op.execute(
        """UPDATE channel_external_principals AS principal
              SET status = 'frozen',
                  updated_at = GREATEST(
                      principal.updated_at,
                      COALESCE(binding.deleted_at, binding.updated_at)
                  )
             FROM project_channel_group_bindings AS binding
            WHERE principal.project_id = binding.project_id
              AND principal.group_binding_id = binding.id
              AND binding.status = 'disabled'
              AND principal.status = 'active'"""
    )
    op.execute(
        """DELETE FROM channel_conversations AS conversation
              USING channel_connections AS connection,
                    channel_external_principals AS principal,
                    project_channel_group_bindings AS binding
             WHERE binding.status = 'disabled'
               AND principal.project_id = binding.project_id
               AND principal.group_binding_id = binding.id
               AND connection.project_id = binding.project_id
               AND connection.channel_instance_id = binding.channel_instance_id
               AND connection.owner_user_id = principal.principal_user_id
               AND connection.id = replace(principal.id::text, '-', '')
               AND connection.status <> 'revoked'
               AND (
                   binding.deleted_at IS NOT NULL
                   OR (connection.metadata_json ->> 'agent_asset_id')
                      IS DISTINCT FROM binding.agent_asset_id::text
                   OR (connection.metadata_json ->> 'agent_scope')
                      IS DISTINCT FROM binding.agent_scope
               )
               AND conversation.project_id = binding.project_id
               AND conversation.owner_user_id = principal.principal_user_id
               AND conversation.connection_id = connection.id"""
    )
    op.execute(
        """UPDATE channel_connections AS connection
              SET status = 'frozen',
                  frozen_at = COALESCE(
                      connection.frozen_at,
                      binding.deleted_at,
                      binding.updated_at
                  ),
                  metadata_json = CASE
                      WHEN binding.deleted_at IS NOT NULL
                          THEN json_build_object(
                              'group_binding_id', binding.id::text
                          )
                      WHEN (connection.metadata_json ->> 'agent_asset_id')
                              IS DISTINCT FROM binding.agent_asset_id::text
                           OR (connection.metadata_json ->> 'agent_scope')
                              IS DISTINCT FROM binding.agent_scope
                          THEN json_build_object(
                              'group_binding_id', binding.id::text,
                              'agent_asset_id', binding.agent_asset_id::text,
                              'agent_scope', binding.agent_scope
                          )
                      ELSE connection.metadata_json
                  END,
                  updated_at = GREATEST(
                      connection.updated_at,
                      COALESCE(binding.deleted_at, binding.updated_at)
                  )
             FROM channel_external_principals AS principal,
                  project_channel_group_bindings AS binding
            WHERE principal.project_id = binding.project_id
              AND principal.group_binding_id = binding.id
              AND binding.status = 'disabled'
              AND connection.project_id = binding.project_id
              AND connection.channel_instance_id = binding.channel_instance_id
              AND connection.owner_user_id = principal.principal_user_id
              AND connection.id = replace(principal.id::text, '-', '')
              AND connection.status <> 'revoked'"""
    )
    op.execute(
        """UPDATE project_channel_group_bindings
              SET status = 'disabled',
                  agent_asset_id = NULL,
                  agent_scope = NULL
            WHERE deleted_at IS NOT NULL"""
    )

    op.execute("ALTER TABLE project_channel_group_bindings ADD CONSTRAINT ck_project_channel_group_bindings_agent_ref_pair CHECK ((agent_asset_id IS NULL) = (agent_scope IS NULL)) NOT VALID")
    op.execute("ALTER TABLE project_channel_group_bindings ADD CONSTRAINT ck_project_channel_group_bindings_agent_lifecycle CHECK ((deleted_at IS NULL) = (agent_asset_id IS NOT NULL)) NOT VALID")
    op.execute("ALTER TABLE project_channel_group_bindings VALIDATE CONSTRAINT ck_project_channel_group_bindings_agent_ref_pair")
    op.execute("ALTER TABLE project_channel_group_bindings VALIDATE CONSTRAINT ck_project_channel_group_bindings_agent_lifecycle")
    op.execute("COMMENT ON TABLE project_channel_group_bindings IS '保存外部渠道群组与项目之间的受管绑定及可复用身份锚点。'")
    op.execute("COMMENT ON COLUMN project_channel_group_bindings.agent_scope IS '渠道群组绑定：活动绑定的智能体范围；软删除后为空。'")
    op.execute("COMMENT ON COLUMN project_channel_group_bindings.agent_asset_id IS '渠道群组绑定：活动绑定的智能体资产标识；软删除后为空。'")
    op.execute("COMMENT ON COLUMN project_channel_group_bindings.deleted_at IS '渠道群组绑定：软删除时间；置值后保留身份锚点并释放智能体引用。'")


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
