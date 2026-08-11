CREATE TABLE workflow_code_sandbox_leases (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    workflow_run_id UUID NOT NULL,
    node_id UUID NOT NULL,
    activation_id VARCHAR(128) NOT NULL,
    activation_attempt INTEGER NOT NULL,
    job_id UUID NOT NULL,
    workflow_epoch BIGINT NOT NULL,
    job_attempt_number INTEGER NOT NULL,
    worker_id UUID NOT NULL,
    reconciliation_key_hash CHAR(64) NOT NULL,
    profile_digest CHAR(64) NOT NULL,
    state VARCHAR(24) NOT NULL,
    execution_lease_token_hash CHAR(64),
    cleanup_locator_ciphertext BYTEA,
    cleanup_deadline TIMESTAMPTZ NOT NULL,
    cleanup_handoff_at TIMESTAMPTZ,
    cleanup_owner_worker_id UUID,
    cleanup_lease_token_hash CHAR(64),
    cleanup_lease_expires_at TIMESTAMPTZ,
    cleanup_attempt INTEGER DEFAULT 0 NOT NULL,
    destroyed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT uq_workflow_code_leases_activation_attempt UNIQUE (
        workflow_run_id, node_id, activation_id, activation_attempt
    ),
    CONSTRAINT fk_workflow_code_leases_scope FOREIGN KEY (
        workflow_run_id, project_id, owner_user_id
    ) REFERENCES workflow_runs(id, project_id, owner_user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_code_leases_job_attempt FOREIGN KEY (
        job_id, job_attempt_number
    ) REFERENCES job_attempts(job_id, attempt_number) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_code_leases_job_scope FOREIGN KEY (
        job_id, project_id, owner_user_id, workflow_run_id, workflow_epoch
    ) REFERENCES jobs(
        id, project_id, owner_user_id, workflow_run_id, workflow_epoch
    ) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_code_leases_run_job_mapping FOREIGN KEY (
        workflow_run_id, workflow_epoch, job_id
    ) REFERENCES workflow_run_jobs(
        workflow_run_id, execution_epoch, job_id
    ) ON DELETE RESTRICT,
    CONSTRAINT ck_workflow_code_leases_shape CHECK (
        activation_attempt >= 1 AND workflow_epoch >= 1
        AND job_attempt_number >= 1 AND cleanup_attempt >= 0
        AND reconciliation_key_hash ~ '^[0-9a-f]{64}$'
        AND profile_digest ~ '^[0-9a-f]{64}$'
        AND (
            (state = 'provisioning'
                AND execution_lease_token_hash ~ '^[0-9a-f]{64}$'
                AND cleanup_locator_ciphertext IS NULL
                AND cleanup_handoff_at IS NULL
                AND cleanup_owner_worker_id IS NULL
                AND cleanup_lease_token_hash IS NULL
                AND cleanup_lease_expires_at IS NULL
                AND destroyed_at IS NULL)
            OR (state = 'running'
                AND execution_lease_token_hash ~ '^[0-9a-f]{64}$'
                AND cleanup_locator_ciphertext IS NOT NULL
                AND octet_length(cleanup_locator_ciphertext) > 0
                AND cleanup_handoff_at IS NULL
                AND cleanup_owner_worker_id IS NULL
                AND cleanup_lease_token_hash IS NULL
                AND cleanup_lease_expires_at IS NULL
                AND destroyed_at IS NULL)
            OR (state = 'cleanup_pending'
                AND execution_lease_token_hash IS NULL
                AND cleanup_handoff_at IS NOT NULL
                AND destroyed_at IS NULL
                AND (
                    (cleanup_owner_worker_id IS NULL
                        AND cleanup_lease_token_hash IS NULL
                        AND cleanup_lease_expires_at IS NULL)
                    OR (cleanup_owner_worker_id IS NOT NULL
                        AND cleanup_lease_token_hash ~ '^[0-9a-f]{64}$'
                        AND cleanup_lease_expires_at IS NOT NULL)
                ))
            OR (state = 'destroyed'
                AND execution_lease_token_hash IS NULL
                AND cleanup_locator_ciphertext IS NULL
                AND cleanup_handoff_at IS NULL
                AND cleanup_owner_worker_id IS NULL
                AND cleanup_lease_token_hash IS NULL
                AND cleanup_lease_expires_at IS NULL
                AND destroyed_at IS NOT NULL)
        )
    )
);

CREATE UNIQUE INDEX uq_workflow_code_leases_open_activation
    ON workflow_code_sandbox_leases (workflow_run_id, node_id, activation_id)
    WHERE state <> 'destroyed';

CREATE INDEX ix_workflow_code_leases_cleanup_claim
    ON workflow_code_sandbox_leases (
        state, cleanup_lease_expires_at, created_at, id
    ) WHERE state IN ('provisioning','running','cleanup_pending');
