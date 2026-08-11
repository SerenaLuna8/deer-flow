# ruff: noqa: E501
"""Add the durable Workflow control-plane idempotency authority.

This revision is explicit and frozen.  It extends the v10 Workflow schema
without importing live ORM or application contracts.
"""

from __future__ import annotations

from alembic import op

revision = "full_schema_v11"
down_revision = "full_schema_v10"
branch_labels = None
depends_on = None

_DDL = r"""
CREATE TABLE workflow_control_operations (
    project_id UUID NOT NULL,
    workflow_id UUID NOT NULL,
    operation VARCHAR(32) NOT NULL,
    idempotency_hash CHAR(64) NOT NULL,
    request_digest CHAR(64) NOT NULL,
    result_version_id UUID NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_workflow_control_operations PRIMARY KEY (workflow_id, operation, idempotency_hash),
    CONSTRAINT ck_workflow_control_operations_operation CHECK (operation = 'publish'),
    CONSTRAINT ck_workflow_control_operations_idempotency_hash CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_workflow_control_operations_request_digest CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_workflow_control_operations_definition FOREIGN KEY(workflow_id, project_id)
        REFERENCES workflow_definitions (id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_control_operations_result_version FOREIGN KEY(result_version_id, workflow_id, project_id)
        REFERENCES workflow_versions (id, workflow_id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_control_operations_actor FOREIGN KEY(created_by)
        REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_workflow_control_operations_project_created
    ON workflow_control_operations (project_id, created_at DESC);

CREATE TRIGGER trg_workflow_control_operations_immutable
BEFORE UPDATE OR DELETE ON workflow_control_operations
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

ALTER TABLE workflow_version_credential_slots
    ADD CONSTRAINT uq_workflow_version_credential_slots_scope
    UNIQUE (workflow_version_id, project_id, slot_id);

CREATE TABLE workflow_version_code_requirements (
    workflow_version_id UUID NOT NULL,
    project_id UUID NOT NULL,
    node_id UUID NOT NULL,
    runtime_contract VARCHAR(128) NOT NULL,
    PRIMARY KEY (workflow_version_id, node_id),
    CONSTRAINT ck_workflow_version_code_requirements_contract
        CHECK (runtime_contract = 'python3.12-v1'),
    CONSTRAINT fk_workflow_version_code_requirements_version
        FOREIGN KEY(workflow_version_id, project_id)
        REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT
);

CREATE TABLE workflow_version_http_requirements (
    workflow_version_id UUID NOT NULL,
    project_id UUID NOT NULL,
    node_id UUID NOT NULL,
    method VARCHAR(8) NOT NULL,
    endpoint_policy_id VARCHAR(128) NOT NULL,
    injection_profile_id VARCHAR(128),
    credential_slot_id VARCHAR(128),
    PRIMARY KEY (workflow_version_id, node_id),
    CONSTRAINT ck_workflow_version_http_requirements_method
        CHECK (method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')),
    CONSTRAINT ck_workflow_version_http_requirements_endpoint
        CHECK (endpoint_policy_id ~ '^[a-z][a-z0-9._-]{0,127}$'),
    CONSTRAINT ck_workflow_version_http_requirements_injection
        CHECK (injection_profile_id IS NULL OR injection_profile_id ~ '^[a-z][a-z0-9._-]{0,127}$'),
    CONSTRAINT ck_workflow_version_http_requirements_slot
        CHECK (credential_slot_id IS NULL OR credential_slot_id ~ '^[a-z][a-z0-9._-]{0,127}$'),
    CONSTRAINT ck_workflow_version_http_requirements_auth_pair
        CHECK ((injection_profile_id IS NULL) = (credential_slot_id IS NULL)),
    CONSTRAINT fk_workflow_version_http_requirements_version
        FOREIGN KEY(workflow_version_id, project_id)
        REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_version_http_requirements_slot
        FOREIGN KEY(workflow_version_id, project_id, credential_slot_id)
        REFERENCES workflow_version_credential_slots (workflow_version_id, project_id, slot_id)
        ON DELETE RESTRICT
);

CREATE TRIGGER trg_workflow_version_code_requirements_immutable
BEFORE UPDATE OR DELETE ON workflow_version_code_requirements
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_http_requirements_immutable
BEFORE UPDATE OR DELETE ON workflow_version_http_requirements
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_DDL)


def downgrade() -> None:
    raise RuntimeError("full_schema_v11 is append-only and cannot be downgraded")
