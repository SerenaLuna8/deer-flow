-- G04/G05 DISPOSABLE PROTOTYPE. G10 owns the production migration.
-- This is the single SQL source used by the focused store tests, the real
-- controlled-egress conformance and the wider G05 schema prototype.
CREATE TABLE workflow_node_effects (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    workflow_run_id UUID NOT NULL,
    node_id UUID NOT NULL,
    activation_key VARCHAR(128) NOT NULL,
    operation_key CHAR(64) NOT NULL,
    http_method VARCHAR(6) NOT NULL,
    status VARCHAR(16) NOT NULL,
    request_hmac CHAR(64) NOT NULL,
    provider_idempotency_key CHAR(64) NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    dispatch_job_id UUID,
    dispatch_execution_epoch BIGINT,
    dispatch_attempt BIGINT,
    dispatch_owner_id UUID,
    dispatch_lease_token_hash CHAR(64),
    dispatch_started_at TIMESTAMPTZ,
    outcome_json JSONB,
    outcome_digest CHAR(64),
    safe_error_code VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT ck_workflow_node_effects_method CHECK (
        http_method IN ('POST','PUT','PATCH','DELETE')
    ),
    CONSTRAINT ck_workflow_node_effects_status CHECK (
        status IN ('prepared','dispatching','settled','failed_safe','unknown')
    ),
    CONSTRAINT ck_workflow_node_effects_revision CHECK (revision >= 1),
    CONSTRAINT ck_workflow_node_effects_request_hmac CHECK (
        request_hmac ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_workflow_node_effects_operation_key CHECK (
        operation_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_workflow_node_effects_provider_key CHECK (
        provider_idempotency_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_workflow_node_effects_epoch_attempt CHECK (
        (dispatch_execution_epoch IS NULL OR dispatch_execution_epoch >= 1)
        AND (dispatch_attempt IS NULL OR dispatch_attempt >= 1)
    ),
    CONSTRAINT ck_workflow_node_effects_lease_hash CHECK (
        dispatch_lease_token_hash IS NULL
        OR dispatch_lease_token_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_workflow_node_effects_outcome_digest CHECK (
        outcome_digest IS NULL OR outcome_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_workflow_node_effects_safe_error CHECK (
        safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'
    ),
    CONSTRAINT ck_workflow_node_effects_state_shape CHECK (
        (
            status = 'prepared'
            AND dispatch_job_id IS NULL
            AND dispatch_execution_epoch IS NULL
            AND dispatch_attempt IS NULL
            AND dispatch_owner_id IS NULL
            AND dispatch_lease_token_hash IS NULL
            AND dispatch_started_at IS NULL
            AND outcome_json IS NULL
            AND outcome_digest IS NULL
            AND safe_error_code IS NULL
        ) OR (
            status = 'dispatching'
            AND dispatch_job_id IS NOT NULL
            AND dispatch_execution_epoch IS NOT NULL
            AND dispatch_attempt IS NOT NULL
            AND dispatch_owner_id IS NOT NULL
            AND dispatch_lease_token_hash IS NOT NULL
            AND dispatch_started_at IS NOT NULL
            AND outcome_json IS NULL
            AND outcome_digest IS NULL
            AND safe_error_code IS NULL
        ) OR (
            status = 'settled'
            AND dispatch_job_id IS NOT NULL
            AND dispatch_execution_epoch IS NOT NULL
            AND dispatch_attempt IS NOT NULL
            AND dispatch_owner_id IS NULL
            AND dispatch_lease_token_hash IS NULL
            AND dispatch_started_at IS NOT NULL
            AND outcome_json IS NOT NULL
            AND jsonb_typeof(outcome_json) = 'object'
            AND outcome_digest IS NOT NULL
            AND safe_error_code IS NULL
        ) OR (
            status = 'failed_safe'
            AND dispatch_job_id IS NOT NULL
            AND dispatch_execution_epoch IS NOT NULL
            AND dispatch_attempt IS NOT NULL
            AND dispatch_owner_id IS NULL
            AND dispatch_lease_token_hash IS NULL
            AND dispatch_started_at IS NOT NULL
            AND outcome_json IS NULL
            AND outcome_digest IS NULL
            AND safe_error_code IS NOT NULL
            AND safe_error_code <> 'SIDE_EFFECT_STATE_UNKNOWN'
        ) OR (
            status = 'unknown'
            AND dispatch_job_id IS NOT NULL
            AND dispatch_execution_epoch IS NOT NULL
            AND dispatch_attempt IS NOT NULL
            AND dispatch_owner_id IS NULL
            AND dispatch_lease_token_hash IS NULL
            AND dispatch_started_at IS NOT NULL
            AND outcome_json IS NULL
            AND outcome_digest IS NULL
            AND safe_error_code = 'SIDE_EFFECT_STATE_UNKNOWN'
        )
    ),
    CONSTRAINT uq_workflow_node_effects_operation UNIQUE (
        workflow_run_id, node_id, activation_key, operation_key
    ),
    CONSTRAINT uq_workflow_node_effects_activation UNIQUE (
        workflow_run_id, node_id, activation_key
    ),
    CONSTRAINT fk_workflow_node_effects_run FOREIGN KEY (
        workflow_run_id, project_id, owner_user_id
    ) REFERENCES workflow_runs(id, project_id, owner_user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_node_effects_dispatch_job FOREIGN KEY (
        dispatch_job_id, project_id, owner_user_id,
        workflow_run_id, dispatch_execution_epoch
    ) REFERENCES jobs(
        id, project_id, owner_user_id, workflow_run_id, workflow_epoch
    ) ON DELETE RESTRICT
);
