# ruff: noqa: E501
"""Generalize Workflow control idempotency without adding a second authority.

The v11 migration stays frozen.  This explicit revision widens its append-only
receipt table to every Definition state mutation and aligns persisted slot IDs
with the already-published Definition contract.
"""

from __future__ import annotations

from alembic import op

revision = "full_schema_v12"
down_revision = "full_schema_v11"
branch_labels = None
depends_on = None

_DDL = r"""
ALTER TABLE workflow_control_operations
    DISABLE TRIGGER trg_workflow_control_operations_immutable;

ALTER TABLE workflow_control_operations
    DROP CONSTRAINT pk_workflow_control_operations,
    DROP CONSTRAINT ck_workflow_control_operations_operation,
    ALTER COLUMN result_version_id DROP NOT NULL,
    ADD COLUMN scope_key VARCHAR(512),
    ADD COLUMN result_revision BIGINT,
    ADD COLUMN result_checksum CHAR(64),
    ADD COLUMN result_slot_id VARCHAR(128),
    ADD COLUMN result_credential_id UUID,
    ADD COLUMN result_credential_version_id UUID,
    ADD COLUMN result_status VARCHAR(16),
    ADD COLUMN result_deleted BOOLEAN,
    ADD COLUMN result_created_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN result_updated_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN result_revoked_at TIMESTAMP WITH TIME ZONE,
    ADD COLUMN result_name VARCHAR(255),
    ADD COLUMN result_description VARCHAR(4096),
    ADD COLUMN result_lifecycle VARCHAR(16),
    ADD COLUMN result_published_version_id UUID,
    ADD COLUMN result_published_version_number BIGINT,
    ADD COLUMN result_draft_revision BIGINT,
    ADD COLUMN result_draft_checksum CHAR(64),
    ADD COLUMN result_missing_slot_ids_csv VARCHAR(33000);

UPDATE workflow_control_operations
SET scope_key = 'definition:' || workflow_id::text;

UPDATE workflow_control_operations AS operation_row
SET result_missing_slot_ids_csv = COALESCE(
    (
        SELECT string_agg(slot.slot_id, ',' ORDER BY slot.slot_id)
        FROM workflow_version_credential_slots AS slot
        WHERE slot.workflow_version_id = operation_row.result_version_id
          AND slot.required
          AND NOT EXISTS (
              SELECT 1
              FROM workflow_credential_grants AS grant_row
              WHERE grant_row.workflow_version_id = slot.workflow_version_id
                AND grant_row.slot_id = slot.slot_id
                AND grant_row.status = 'active'
          )
    ),
    ''
)
WHERE operation_row.operation = 'publish';

ALTER TABLE workflow_control_operations
    ALTER COLUMN scope_key SET NOT NULL,
    ADD CONSTRAINT pk_workflow_control_operations
        PRIMARY KEY (project_id, operation, scope_key, idempotency_hash),
    ADD CONSTRAINT ck_workflow_control_operations_operation
        CHECK (operation IN ('create','update','save_draft','archive','publish','draft_grant_put','draft_grant_delete','version_grant_put','version_grant_delete')),
    -- PostgreSQL bounds one regex repetition at 255.  VARCHAR(512) plus this
    -- character check is the exact ^[a-z][A-Za-z0-9:._-]{0,511}$ contract.
    ADD CONSTRAINT ck_workflow_control_operations_scope_key
        CHECK (char_length(scope_key) BETWEEN 1 AND 512 AND scope_key ~ '^[a-z][A-Za-z0-9:._-]*$'),
    ADD CONSTRAINT ck_workflow_control_operations_scope_shape
        CHECK (
            (operation = 'create' AND scope_key = 'project:' || project_id::text) OR
            (operation IN ('update','save_draft','archive','publish') AND scope_key = 'definition:' || workflow_id::text) OR
            (operation IN ('draft_grant_put','draft_grant_delete') AND scope_key = 'draft-slot:' || workflow_id::text || ':' || result_slot_id) OR
            (operation IN ('version_grant_put','version_grant_delete') AND scope_key = 'version-slot:' || workflow_id::text || ':' || result_version_id::text || ':' || result_slot_id)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_result_revision
        CHECK (result_revision IS NULL OR result_revision >= 1),
    ADD CONSTRAINT ck_workflow_control_operations_result_checksum
        CHECK (result_checksum IS NULL OR result_checksum ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_workflow_control_operations_result_draft_checksum
        CHECK (result_draft_checksum IS NULL OR result_draft_checksum ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_workflow_control_operations_result_slot
        CHECK (result_slot_id IS NULL OR result_slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'),
    ADD CONSTRAINT ck_workflow_control_operations_result_status
        CHECK (result_status IS NULL OR result_status IN ('active','revoked')),
    ADD CONSTRAINT ck_workflow_control_operations_result_lifecycle
        CHECK (result_lifecycle IS NULL OR result_lifecycle IN ('active','archived')),
    ADD CONSTRAINT ck_workflow_control_operations_result_published_version_number
        CHECK (result_published_version_number IS NULL OR result_published_version_number >= 1),
    ADD CONSTRAINT ck_workflow_control_operations_result_draft_revision
        CHECK (result_draft_revision IS NULL OR result_draft_revision >= 1),
    ADD CONSTRAINT ck_workflow_control_operations_result_missing_slots
        CHECK (result_missing_slot_ids_csv IS NULL OR result_missing_slot_ids_csv = '' OR result_missing_slot_ids_csv ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}(,[A-Za-z_][A-Za-z0-9_.:-]{0,127})*$'),
    ADD CONSTRAINT ck_workflow_control_operations_version_shape
        CHECK ((operation IN ('publish','version_grant_put','version_grant_delete')) = (result_version_id IS NOT NULL)),
    ADD CONSTRAINT ck_workflow_control_operations_slot_shape
        CHECK ((operation IN ('draft_grant_put','draft_grant_delete','version_grant_put','version_grant_delete')) = (result_slot_id IS NOT NULL)),
    ADD CONSTRAINT ck_workflow_control_operations_credential_shape
        CHECK (
            (operation IN ('draft_grant_put','version_grant_put','version_grant_delete') AND result_credential_id IS NOT NULL AND result_credential_version_id IS NOT NULL AND result_checksum IS NOT NULL) OR
            (operation NOT IN ('draft_grant_put','version_grant_put','version_grant_delete') AND result_credential_id IS NULL AND result_credential_version_id IS NULL AND result_checksum IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_delete_shape
        CHECK ((operation = 'draft_grant_delete' AND result_deleted IS TRUE) OR (operation <> 'draft_grant_delete' AND result_deleted IS NULL)),
    ADD CONSTRAINT ck_workflow_control_operations_definition_shape
        CHECK (
            (operation IN ('create','update','archive') AND result_name IS NOT NULL AND result_description IS NOT NULL AND result_lifecycle IS NOT NULL AND result_revision IS NOT NULL AND result_draft_revision IS NOT NULL AND result_draft_checksum IS NOT NULL AND result_created_at IS NOT NULL AND result_updated_at IS NOT NULL) OR
            (operation NOT IN ('create','update','archive') AND result_name IS NULL AND result_description IS NULL AND result_lifecycle IS NULL AND result_published_version_id IS NULL AND result_published_version_number IS NULL AND result_draft_revision IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_lifecycle_shape
        CHECK (
            (operation = 'archive' AND result_lifecycle = 'archived') OR
            (operation IN ('create','update') AND result_lifecycle = 'active') OR
            (operation NOT IN ('create','update','archive') AND result_lifecycle IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_publication_shape
        CHECK ((result_published_version_id IS NULL AND result_published_version_number IS NULL) OR (result_published_version_id IS NOT NULL AND result_published_version_number IS NOT NULL)),
    ADD CONSTRAINT ck_workflow_control_operations_draft_shape
        CHECK (
            (operation IN ('create','update','archive') AND result_draft_revision IS NOT NULL AND result_draft_checksum IS NOT NULL) OR
            (operation = 'save_draft' AND result_draft_revision IS NULL AND result_draft_checksum IS NOT NULL) OR
            (operation NOT IN ('create','update','archive','save_draft') AND result_draft_revision IS NULL AND result_draft_checksum IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_revision_shape
        CHECK (
            (operation IN ('create','update','save_draft','archive','version_grant_put','version_grant_delete') AND result_revision IS NOT NULL) OR
            (operation NOT IN ('create','update','save_draft','archive','version_grant_put','version_grant_delete') AND result_revision IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_status_shape
        CHECK (
            (operation = 'version_grant_put' AND result_status IS NOT NULL AND result_status = 'active') OR
            (operation = 'version_grant_delete' AND result_status IS NOT NULL AND result_status = 'revoked') OR
            (operation NOT IN ('version_grant_put','version_grant_delete') AND result_status IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_created_at_shape
        CHECK (
            (operation IN ('create','update','archive','version_grant_put','version_grant_delete') AND result_created_at IS NOT NULL) OR
            (operation NOT IN ('create','update','archive','version_grant_put','version_grant_delete') AND result_created_at IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_updated_at_shape
        CHECK (
            (operation IN ('create','update','save_draft','archive','draft_grant_put') AND result_updated_at IS NOT NULL) OR
            (operation NOT IN ('create','update','save_draft','archive','draft_grant_put') AND result_updated_at IS NULL)
        ),
    ADD CONSTRAINT ck_workflow_control_operations_revoked_at_shape
        CHECK ((operation = 'version_grant_delete' AND result_revoked_at IS NOT NULL) OR (operation <> 'version_grant_delete' AND result_revoked_at IS NULL)),
    ADD CONSTRAINT ck_workflow_control_operations_publish_shape
        CHECK ((operation = 'publish' AND result_missing_slot_ids_csv IS NOT NULL) OR (operation <> 'publish' AND result_missing_slot_ids_csv IS NULL)),
    ADD CONSTRAINT fk_workflow_control_operations_published_version
        FOREIGN KEY (result_published_version_id,workflow_id,project_id)
        REFERENCES workflow_versions (id,workflow_id,project_id)
        ON DELETE RESTRICT;

ALTER TABLE workflow_control_operations
    ENABLE TRIGGER trg_workflow_control_operations_immutable;

ALTER TABLE workflow_draft_credential_grant_intents
    DROP CONSTRAINT ck_workflow_draft_grant_intents_slot,
    ADD CONSTRAINT ck_workflow_draft_grant_intents_slot
        CHECK (slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$');

ALTER TABLE workflow_version_credential_slots
    DROP CONSTRAINT ck_workflow_version_credential_slots_slot,
    ADD CONSTRAINT ck_workflow_version_credential_slots_slot
        CHECK (slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$');

ALTER TABLE workflow_version_http_requirements
    DROP CONSTRAINT ck_workflow_version_http_requirements_slot,
    ADD CONSTRAINT ck_workflow_version_http_requirements_slot
        CHECK (credential_slot_id IS NULL OR credential_slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$');
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_DDL)


def downgrade() -> None:
    raise RuntimeError("full_schema_v12 is append-only and cannot be downgraded")
