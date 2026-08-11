-- G05 DISPOSABLE PROTOTYPE. Never use this file as a production migration.
-- G10 must author the atomic ORM/full-schema/Alembic release unit from the
-- then-current schema head and re-run fresh/upgrade catalog parity.
BEGIN;

ALTER TABLE system_runtime_policies
    DROP CONSTRAINT ck_system_runtime_policies_section;
ALTER TABLE system_runtime_policies
    ADD CONSTRAINT ck_system_runtime_policies_section
    CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas', 'workflow_runtime'));

ALTER TABLE system_runtime_policy_versions
    DROP CONSTRAINT ck_system_runtime_policy_versions_section;
ALTER TABLE system_runtime_policy_versions
    ADD CONSTRAINT ck_system_runtime_policy_versions_section
    CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas', 'workflow_runtime'));

ALTER TABLE worker_nodes
    ADD COLUMN runtime_profile_digests_json JSONB DEFAULT '[]'::jsonb NOT NULL,
    ADD CONSTRAINT ck_worker_nodes_runtime_profiles_array
        CHECK (jsonb_typeof(runtime_profile_digests_json) = 'array');

CREATE TABLE workflow_definitions (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    status VARCHAR(16) NOT NULL,
    current_published_version_id UUID,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by VARCHAR(36) NOT NULL,
    updated_by VARCHAR(36) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT ck_workflow_definitions_status CHECK (status IN ('active', 'archived')),
    CONSTRAINT ck_workflow_definitions_revision CHECK (revision >= 1),
    CONSTRAINT uq_workflow_definitions_id_project UNIQUE (id, project_id),
    CONSTRAINT fk_workflow_definitions_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_definitions_created_by FOREIGN KEY (created_by)
        REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_definitions_updated_by FOREIGN KEY (updated_by)
        REFERENCES users(id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_workflow_definitions_project_name
    ON workflow_definitions (project_id, lower(name));

CREATE TABLE workflow_versions (
    id UUID PRIMARY KEY,
    workflow_id UUID NOT NULL,
    version_number BIGINT NOT NULL,
    graph_schema_version SMALLINT DEFAULT 1 NOT NULL,
    canvas_schema_version SMALLINT DEFAULT 1 NOT NULL,
    compiler_contract_version SMALLINT NOT NULL,
    spec_json JSONB NOT NULL,
    canvas_json JSONB NOT NULL,
    semantic_checksum CHAR(64) NOT NULL,
    published_by VARCHAR(36) NOT NULL,
    published_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT ck_workflow_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_workflow_versions_schema CHECK (
        graph_schema_version >= 1 AND canvas_schema_version >= 1
        AND compiler_contract_version >= 1
    ),
    CONSTRAINT ck_workflow_versions_spec_object CHECK (jsonb_typeof(spec_json) = 'object'),
    CONSTRAINT ck_workflow_versions_canvas_object CHECK (jsonb_typeof(canvas_json) = 'object'),
    CONSTRAINT ck_workflow_versions_checksum CHECK (semantic_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_workflow_versions_number UNIQUE (workflow_id, version_number),
    CONSTRAINT uq_workflow_versions_workflow_id UNIQUE (workflow_id, id),
    CONSTRAINT fk_workflow_versions_workflow FOREIGN KEY (workflow_id)
        REFERENCES workflow_definitions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_versions_published_by FOREIGN KEY (published_by)
        REFERENCES users(id) ON DELETE RESTRICT
);

ALTER TABLE workflow_definitions
    ADD CONSTRAINT fk_workflow_definitions_current_version
    FOREIGN KEY (id, current_published_version_id)
    REFERENCES workflow_versions(workflow_id, id) ON DELETE RESTRICT
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE workflow_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    workflow_id UUID NOT NULL,
    workflow_version_id UUID NOT NULL,
    status VARCHAR(32) NOT NULL,
    input_json JSONB NOT NULL,
    output_json JSONB,
    input_digest CHAR(64) NOT NULL,
    idempotency_hash CHAR(64) NOT NULL,
    trigger_kind VARCHAR(16) NOT NULL,
    trigger_ref VARCHAR(128),
    origin_trace_id VARCHAR(512) NOT NULL,
    required_worker_profile_digest CHAR(64),
    execution_epoch BIGINT DEFAULT 1 NOT NULL,
    current_job_id UUID,
    retry_of_run_id UUID,
    error_code VARCHAR(64),
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT ck_workflow_runs_status CHECK (
        status IN ('queued','running','succeeded','failed','cancelled','side_effect_unknown')
    ),
    CONSTRAINT ck_workflow_runs_trigger CHECK (trigger_kind IN ('manual','api','automation')),
    CONSTRAINT ck_workflow_runs_epoch CHECK (execution_epoch >= 1),
    CONSTRAINT ck_workflow_runs_input_object CHECK (jsonb_typeof(input_json) = 'object'),
    CONSTRAINT ck_workflow_runs_output_object CHECK (
        output_json IS NULL OR jsonb_typeof(output_json) = 'object'
    ),
    CONSTRAINT ck_workflow_runs_input_digest CHECK (input_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_workflow_runs_idempotency CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_workflow_runs_profile_digest CHECK (
        required_worker_profile_digest IS NULL
        OR required_worker_profile_digest ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT uq_workflow_runs_scope UNIQUE (id, project_id, owner_user_id),
    CONSTRAINT uq_workflow_runs_trace_scope UNIQUE (
        id, project_id, owner_user_id, origin_trace_id
    ),
    CONSTRAINT uq_workflow_runs_epoch UNIQUE (id, execution_epoch),
    CONSTRAINT uq_workflow_runs_idempotency UNIQUE (
        project_id, owner_user_id, workflow_id, idempotency_hash
    ),
    CONSTRAINT fk_workflow_runs_project FOREIGN KEY (project_id)
        REFERENCES projects(id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_runs_owner FOREIGN KEY (owner_user_id)
        REFERENCES users(id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_runs_membership FOREIGN KEY (project_id, owner_user_id)
        REFERENCES project_memberships(project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_runs_definition FOREIGN KEY (workflow_id)
        REFERENCES workflow_definitions(id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_runs_version FOREIGN KEY (workflow_id, workflow_version_id)
        REFERENCES workflow_versions(workflow_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_runs_retry FOREIGN KEY (retry_of_run_id)
        REFERENCES workflow_runs(id) ON DELETE RESTRICT
);

ALTER TABLE jobs
    ADD COLUMN workflow_run_id UUID,
    ADD COLUMN workflow_epoch BIGINT,
    ADD COLUMN required_worker_profile_digest CHAR(64);

ALTER TABLE jobs DROP CONSTRAINT ck_jobs_authority_shape;
ALTER TABLE jobs DROP CONSTRAINT ck_jobs_type;
ALTER TABLE jobs
    ADD CONSTRAINT ck_jobs_type CHECK (
        job_type IN (
            'private_run','automation_run','workflow_run','retention_purge',
            'mcp_discovery','memory_dream','memory_seal'
        )
    ),
    ADD CONSTRAINT ck_jobs_workflow_epoch CHECK (
        (workflow_run_id IS NULL) = (workflow_epoch IS NULL)
        AND (workflow_epoch IS NULL OR workflow_epoch >= 1)
    ),
    ADD CONSTRAINT ck_jobs_workflow_profile_digest CHECK (
        required_worker_profile_digest IS NULL
        OR required_worker_profile_digest ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT ck_jobs_authority_shape CHECK (
        (job_type = 'private_run' AND run_id IS NOT NULL
            AND workflow_run_id IS NULL AND workflow_epoch IS NULL
            AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NOT NULL)
        OR (job_type = 'automation_run' AND run_id IS NOT NULL
            AND workflow_run_id IS NULL AND workflow_epoch IS NULL
            AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL
            AND origin_trace_id IS NOT NULL)
        OR (job_type = 'workflow_run' AND run_id IS NULL
            AND workflow_run_id IS NOT NULL AND workflow_epoch IS NOT NULL
            AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NOT NULL)
        OR (job_type = 'retention_purge' AND run_id IS NULL
            AND workflow_run_id IS NULL AND workflow_epoch IS NULL
            AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL
            AND run_id IS NULL AND workflow_run_id IS NULL AND workflow_epoch IS NULL
            AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL
            AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL
            AND workflow_run_id IS NULL AND workflow_epoch IS NULL
            AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
        OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL
            AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL
            AND workflow_run_id IS NULL AND workflow_epoch IS NULL
            AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)
    ),
    ADD CONSTRAINT uq_jobs_workflow_epoch_scope UNIQUE (
        id, project_id, owner_user_id, workflow_run_id, workflow_epoch
    ),
    ADD CONSTRAINT fk_jobs_workflow_run FOREIGN KEY (
        workflow_run_id, project_id, owner_user_id, origin_trace_id
    ) REFERENCES workflow_runs(id, project_id, owner_user_id, origin_trace_id)
        ON DELETE RESTRICT;

CREATE INDEX ix_jobs_workflow_claim
    ON jobs (
        status, job_type, required_worker_profile_digest,
        priority DESC, available_at, created_at, id
    )
    WHERE job_type = 'workflow_run';

CREATE TABLE workflow_run_jobs (
    workflow_run_id UUID NOT NULL,
    execution_epoch BIGINT NOT NULL,
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    cause VARCHAR(16) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    PRIMARY KEY (workflow_run_id, execution_epoch),
    CONSTRAINT uq_workflow_run_jobs_job UNIQUE (job_id),
    CONSTRAINT uq_workflow_run_jobs_run_epoch_job UNIQUE (
        workflow_run_id, execution_epoch, job_id
    ),
    CONSTRAINT ck_workflow_run_jobs_epoch CHECK (execution_epoch >= 1),
    CONSTRAINT ck_workflow_run_jobs_cause CHECK (cause IN ('initial','resume')),
    CONSTRAINT fk_workflow_run_jobs_run_epoch FOREIGN KEY (
        workflow_run_id, execution_epoch
    ) REFERENCES workflow_runs(id, execution_epoch) ON DELETE RESTRICT,
    CONSTRAINT fk_workflow_run_jobs_job_epoch FOREIGN KEY (
        job_id, project_id, owner_user_id, workflow_run_id, execution_epoch
    ) REFERENCES jobs(
        id, project_id, owner_user_id, workflow_run_id, workflow_epoch
    ) ON DELETE RESTRICT
);

ALTER TABLE workflow_runs
    ADD CONSTRAINT fk_workflow_runs_current_job FOREIGN KEY (
        current_job_id, project_id, owner_user_id, id, execution_epoch
    ) REFERENCES jobs(
        id, project_id, owner_user_id, workflow_run_id, workflow_epoch
    ) ON DELETE RESTRICT;

-- WORKFLOW_NODE_EFFECTS_G04_DDL
-- WORKFLOW_CODE_SANDBOX_LEASES_G03_DDL

CREATE TABLE workflow_run_event_invariants (
    workflow_run_id UUID PRIMARY KEY,
    next_seq BIGINT NOT NULL,
    terminal_event_type VARCHAR(64),
    terminal_seq BIGINT,
    updated_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT ck_workflow_event_invariants_next CHECK (next_seq >= 1),
    CONSTRAINT ck_workflow_event_invariants_terminal CHECK (
        (terminal_event_type IS NULL AND terminal_seq IS NULL)
        OR (terminal_event_type IN (
            'workflow.run.completed','workflow.run.failed',
            'workflow.run.cancelled','workflow.run.side_effect_unknown'
        ) AND terminal_seq >= 1)
    ),
    CONSTRAINT fk_workflow_event_invariants_run FOREIGN KEY (workflow_run_id)
        REFERENCES workflow_runs(id) ON DELETE CASCADE
);

CREATE TABLE workflow_run_events (
    workflow_run_id UUID NOT NULL,
    seq BIGINT NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ DEFAULT now() NOT NULL,
    CONSTRAINT ck_workflow_run_events_seq CHECK (seq >= 1),
    CONSTRAINT ck_workflow_run_events_payload CHECK (jsonb_typeof(payload) = 'object')
) PARTITION BY RANGE (occurred_at);

CREATE TABLE workflow_run_events_default
    PARTITION OF workflow_run_events DEFAULT;

CREATE INDEX ix_workflow_run_events_replay
    ON workflow_run_events (workflow_run_id, seq, occurred_at);

CREATE OR REPLACE FUNCTION enforce_workflow_run_event_invariants()
RETURNS TRIGGER AS $$
DECLARE
    current_next BIGINT;
    current_terminal VARCHAR(64);
    is_terminal BOOLEAN;
BEGIN
    SELECT next_seq, terminal_event_type
      INTO current_next, current_terminal
      FROM workflow_run_event_invariants
     WHERE workflow_run_id = NEW.workflow_run_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow event invariant row is missing'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF current_terminal IS NOT NULL THEN
        RAISE EXCEPTION 'workflow run already has a terminal event'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.seq <> current_next THEN
        RAISE EXCEPTION 'workflow event sequence is not contiguous'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    is_terminal := NEW.event_type IN (
        'workflow.run.completed','workflow.run.failed',
        'workflow.run.cancelled','workflow.run.side_effect_unknown'
    );
    UPDATE workflow_run_event_invariants
       SET next_seq = next_seq + 1,
           terminal_event_type = CASE WHEN is_terminal THEN NEW.event_type ELSE NULL END,
           terminal_seq = CASE WHEN is_terminal THEN NEW.seq ELSE NULL END,
           updated_at = now()
     WHERE workflow_run_id = NEW.workflow_run_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_workflow_run_events_invariants
BEFORE INSERT ON workflow_run_events
FOR EACH ROW EXECUTE FUNCTION enforce_workflow_run_event_invariants();

COMMIT;
