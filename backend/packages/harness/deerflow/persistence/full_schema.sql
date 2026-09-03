BEGIN;

CREATE EXTENSION IF NOT EXISTS pg_trgm;



CREATE TABLE project_invitation_rate_limits (
    key_hash CHAR(64) NOT NULL,
    failure_count INTEGER NOT NULL,
    window_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (key_hash),
    CONSTRAINT ck_project_invitation_rate_limits_key_hash CHECK (key_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_invitation_rate_limits_failure_count CHECK (failure_count >= 1)
);

CREATE INDEX ix_project_invitation_rate_limits_expires_at ON project_invitation_rate_limits (expires_at);

CREATE TABLE users (
    id VARCHAR(36) NOT NULL,
    email VARCHAR(320),
    username VARCHAR(32),
    password_hash VARCHAR(128),
    principal_type VARCHAR(16) DEFAULT 'human' NOT NULL,
    system_role VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    oauth_provider VARCHAR(32),
    oauth_id VARCHAR(128),
    needs_setup BOOLEAN NOT NULL,
    token_version INTEGER NOT NULL,
    memory_enabled BOOLEAN DEFAULT true NOT NULL,
    preferences_version BIGINT DEFAULT 1 NOT NULL,
    private_retention_state VARCHAR(24) DEFAULT 'active' NOT NULL,
    private_retention_generation BIGINT DEFAULT 1 NOT NULL,
    private_retention_effective_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT ck_users_system_role CHECK (system_role IN ('system_admin', 'user')),
    CONSTRAINT ck_users_principal_type CHECK (principal_type IN ('human', 'channel_guest')),
    CONSTRAINT ck_users_oauth_identity_shape CHECK ((oauth_provider IS NULL AND oauth_id IS NULL) OR (oauth_provider IS NOT NULL AND oauth_id IS NOT NULL)),
    CONSTRAINT ck_users_username_format CHECK (username IS NULL OR username ~ '^[a-z][a-z0-9_]{2,31}$'),
    CONSTRAINT ck_users_channel_guest_identity CHECK ((principal_type = 'human' AND email IS NOT NULL) OR (principal_type = 'channel_guest' AND email IS NULL AND username IS NULL AND password_hash IS NULL AND oauth_provider IS NULL AND oauth_id IS NULL AND system_role = 'user' AND needs_setup IS FALSE AND token_version = 0)),
    CONSTRAINT ck_users_preferences_version CHECK (preferences_version >= 1),
    CONSTRAINT ck_users_private_retention_state CHECK (private_retention_state IN ('active', 'pending_deletion', 'purged')),
    CONSTRAINT ck_users_private_retention_generation CHECK (private_retention_generation >= 1),
    CONSTRAINT ck_users_private_retention_effective_at CHECK ((private_retention_state = 'pending_deletion' AND private_retention_effective_at IS NOT NULL) OR (private_retention_state IN ('active', 'purged') AND private_retention_effective_at IS NULL)),
    CONSTRAINT uq_users_id_principal_type UNIQUE (id, principal_type)
);

CREATE UNIQUE INDEX ix_users_username ON users (username) WHERE username IS NOT NULL;

CREATE UNIQUE INDEX idx_users_oauth_identity ON users (oauth_provider, oauth_id) WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL;

CREATE UNIQUE INDEX ix_users_email ON users (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE runs (
    run_id VARCHAR(64) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    assistant_id VARCHAR(128),
    owner_user_id VARCHAR(36) NOT NULL,
    status VARCHAR(20) NOT NULL,
    model_name VARCHAR(128),
    multitask_strategy VARCHAR(20) NOT NULL,
    metadata_json JSON NOT NULL,
    kwargs_json JSON NOT NULL,
    origin_trace_id VARCHAR(512) NOT NULL,
    error TEXT,
    message_count INTEGER NOT NULL,
    first_human_message TEXT,
    last_ai_message TEXT,
    total_input_tokens INTEGER NOT NULL,
    total_output_tokens INTEGER NOT NULL,
    total_tokens INTEGER NOT NULL,
    llm_call_count INTEGER NOT NULL,
    lead_agent_tokens INTEGER NOT NULL,
    subagent_tokens INTEGER NOT NULL,
    middleware_tokens INTEGER NOT NULL,
    token_usage_by_model JSON DEFAULT '{}' NOT NULL,
    follow_up_to_run_id VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    job_id UUID,
    execution_lease_token_hash CHAR(64),
    execution_lease_expires_at TIMESTAMP WITH TIME ZONE,
    execution_heartbeat_at TIMESTAMP WITH TIME ZONE,
    execution_started_at TIMESTAMP WITH TIME ZONE,
    cancel_requested_at TIMESTAMP WITH TIME ZONE,
    cancel_reason VARCHAR(64),
    authorization_cancel_requested_at TIMESTAMP WITH TIME ZONE,
    authorization_cancel_reason VARCHAR(64),
    finalization_status VARCHAR(20) DEFAULT 'pending' NOT NULL,
    asset_closure_sealed BOOLEAN DEFAULT false NOT NULL,
    PRIMARY KEY (run_id),
    CONSTRAINT uq_runs_private_scope UNIQUE (project_id, owner_user_id, thread_id, run_id),
    CONSTRAINT uq_runs_job_scope UNIQUE (project_id, owner_user_id, run_id),
    CONSTRAINT uq_runs_job_trace_scope UNIQUE (project_id, owner_user_id, run_id, origin_trace_id),
    CONSTRAINT ck_runs_finalization_status CHECK (finalization_status IN ('pending', 'finalizing', 'complete', 'failed')),
    CONSTRAINT ck_runs_asset_closure_sealed CHECK (asset_closure_sealed IN (true, false))
);

CREATE INDEX ix_runs_project_id ON runs (project_id);

CREATE INDEX ix_runs_owner_user_id ON runs (owner_user_id);

CREATE INDEX ix_runs_thread_id ON runs (thread_id);

CREATE INDEX ix_runs_thread_status ON runs (thread_id, status);

CREATE TABLE jobs (
    id UUID NOT NULL,
    job_type VARCHAR(32) NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36),
    owner_private_generation BIGINT,
    retention_resource_kind VARCHAR(16),
    retention_effective_at TIMESTAMP WITH TIME ZONE,
    retention_membership_id UUID,
    namespace VARCHAR(255),
    run_id VARCHAR(64),
    automation_occurrence_id VARCHAR(64),
    predecessor_dead_job_id UUID,
    origin_trace_id VARCHAR(512),
    idempotency_key CHAR(64) NOT NULL,
    status VARCHAR(16) DEFAULT 'queued' NOT NULL,
    priority SMALLINT DEFAULT 0 NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    attempt_count INTEGER DEFAULT 0 NOT NULL,
    max_attempts INTEGER NOT NULL,
    lease_owner_id UUID,
    lease_token_hash CHAR(64),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    heartbeat_at TIMESTAMP WITH TIME ZONE,
    retry_safety VARCHAR(16) DEFAULT 'safe' NOT NULL,
    public_error_code VARCHAR(64),
    cancel_requested_at TIMESTAMP WITH TIME ZONE,
    cancel_reason VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    execution_domain_affinity CHAR(64),
    PRIMARY KEY (id),
    CONSTRAINT uq_jobs_type_idempotency UNIQUE (job_type, idempotency_key),
    CONSTRAINT uq_jobs_id_project_owner UNIQUE (id, project_id, owner_user_id),
    CONSTRAINT uq_jobs_id_project_owner_run UNIQUE (id, project_id, owner_user_id, run_id),
    CONSTRAINT uq_jobs_id_project_owner_run_execution_domain UNIQUE (id, project_id, owner_user_id, run_id, execution_domain_affinity),
    CONSTRAINT uq_jobs_id_project_owner_namespace UNIQUE (id, project_id, owner_user_id, namespace),
    CONSTRAINT uq_jobs_predecessor_dead_job UNIQUE (predecessor_dead_job_id),
    CONSTRAINT ck_jobs_type CHECK (job_type IN ('private_run', 'automation_run', 'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_dream_prepare', 'memory_seal')),
    CONSTRAINT ck_jobs_status CHECK (status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')),
    CONSTRAINT ck_jobs_retry_safety CHECK (retry_safety IN ('safe', 'unknown', 'unsafe')),
    CONSTRAINT ck_jobs_execution_domain_affinity CHECK (execution_domain_affinity IS NULL OR (job_type = 'private_run' AND execution_domain_affinity ~ '^[0-9a-f]{64}$')),
    CONSTRAINT ck_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts >= 1),
    CONSTRAINT ck_jobs_owner_private_generation CHECK (owner_private_generation IS NOT NULL AND owner_private_generation >= 1 AND (job_type = 'retention_purge' OR owner_user_id IS NOT NULL)),
    CONSTRAINT ck_jobs_retention_authority CHECK ((job_type = 'retention_purge' AND retention_resource_kind IS NOT NULL AND retention_resource_kind IN ('project', 'former_owner', 'account') AND retention_effective_at IS NOT NULL AND ((retention_resource_kind = 'project' AND owner_user_id IS NULL AND retention_membership_id IS NULL) OR (retention_resource_kind = 'former_owner' AND owner_user_id IS NOT NULL AND retention_membership_id IS NOT NULL) OR (retention_resource_kind = 'account' AND owner_user_id IS NOT NULL AND retention_membership_id IS NULL))) OR (job_type <> 'retention_purge' AND retention_resource_kind IS NULL AND retention_effective_at IS NULL AND retention_membership_id IS NULL)),
    CONSTRAINT ck_jobs_authority_shape CHECK ((job_type = 'private_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NOT NULL) OR (job_type = 'automation_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL AND origin_trace_id IS NOT NULL) OR (job_type = 'retention_purge' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_dream_prepare' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)),
    CONSTRAINT ck_jobs_memory_namespace CHECK ((job_type IN ('memory_dream', 'memory_dream_prepare', 'memory_seal')) = (namespace IS NOT NULL))
);

CREATE INDEX ix_jobs_private_scope ON jobs (project_id, owner_user_id, created_at);

CREATE UNIQUE INDEX uq_jobs_active_memory_seal ON jobs (project_id, owner_user_id, namespace) WHERE job_type = 'memory_seal' AND status IN ('queued', 'leased', 'running', 'retry_wait');

CREATE INDEX ix_jobs_claim ON jobs (status, available_at, priority DESC, created_at);

CREATE INDEX ix_jobs_execution_domain_claim ON jobs (execution_domain_affinity, status, available_at, priority DESC, created_at) WHERE execution_domain_affinity IS NOT NULL;

CREATE INDEX ix_jobs_active_lease ON jobs (lease_expires_at, id) WHERE status IN ('leased', 'running');

CREATE TABLE dead_jobs (
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_ref_key_id VARCHAR(64),
    owner_ref_hmac CHAR(64),
    job_type VARCHAR(32) NOT NULL,
    attempt_count INTEGER NOT NULL,
    retry_safety VARCHAR(16) NOT NULL,
    public_error_code VARCHAR(64) NOT NULL,
    dead_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (job_id),
    CONSTRAINT ck_dead_jobs_attempt_count CHECK (attempt_count >= 1),
    CONSTRAINT ck_dead_jobs_retry_safety CHECK (retry_safety IN ('safe', 'unknown', 'unsafe'))
);

CREATE INDEX ix_dead_jobs_project_dead ON dead_jobs (project_id, dead_at DESC, job_id);

CREATE TABLE worker_nodes (
    id UUID NOT NULL,
    version VARCHAR(64) NOT NULL,
    capabilities_json JSON DEFAULT '[]' NOT NULL,
    max_concurrent_jobs INTEGER NOT NULL,
    execution_domain_affinity CHAR(64),
    draining BOOLEAN DEFAULT false NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_worker_nodes_capacity CHECK (max_concurrent_jobs >= 1),
    CONSTRAINT ck_worker_nodes_execution_domain_affinity CHECK (execution_domain_affinity IS NULL OR execution_domain_affinity ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_worker_nodes_fresh ON worker_nodes (draining, heartbeat_at);

CREATE INDEX ix_worker_nodes_fresh_affinity ON worker_nodes (execution_domain_affinity, heartbeat_at) WHERE draining = false;

CREATE TABLE run_event_partition_state (
    singleton BOOLEAN DEFAULT true NOT NULL,
    retained_from TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (singleton),
    CONSTRAINT ck_run_event_partition_state_singleton CHECK (singleton)
);

CREATE TABLE scheduled_task_runs (
    id VARCHAR(64) NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    task_id VARCHAR(64) NOT NULL,
    task_version BIGINT NOT NULL,
    occurrence_key CHAR(64) NOT NULL,
    manual_idempotency_hash CHAR(64),
    scheduled_for TIMESTAMP WITH TIME ZONE NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    status VARCHAR(20) NOT NULL,
    thread_id VARCHAR(64),
    run_id VARCHAR(64),
    job_id UUID,
    resolved_membership_id UUID,
    resolved_membership_version BIGINT,
    launch_attempt_count INTEGER NOT NULL,
    lease_owner VARCHAR(128),
    lease_expires_at TIMESTAMP WITH TIME ZONE,
    next_attempt_at TIMESTAMP WITH TIME ZONE,
    error_code VARCHAR(64),
    error_message TEXT,
    started_at TIMESTAMP WITH TIME ZONE,
    finished_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_scheduled_task_runs_occurrence UNIQUE (project_id, owner_user_id, task_id, occurrence_key),
    CONSTRAINT uq_scheduled_task_runs_job_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT ck_scheduled_task_runs_trigger CHECK (trigger IN ('scheduled', 'manual')),
    CONSTRAINT ck_scheduled_task_runs_status CHECK (status IN ('queued', 'launching', 'running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')),
    CONSTRAINT ck_scheduled_task_runs_run_requires_thread CHECK (run_id IS NULL OR thread_id IS NOT NULL),
    CONSTRAINT ck_scheduled_task_runs_attempt_count CHECK (launch_attempt_count >= 0 AND (resolved_membership_version IS NULL OR resolved_membership_version >= 1)),
    CONSTRAINT ck_scheduled_task_runs_task_version CHECK (task_version >= 1)
);

CREATE UNIQUE INDEX uq_scheduled_task_runs_manual_idempotency ON scheduled_task_runs (project_id, owner_user_id, task_id, manual_idempotency_hash) WHERE manual_idempotency_hash IS NOT NULL;

CREATE INDEX ix_scheduled_task_runs_active_occurrence ON scheduled_task_runs (project_id, owner_user_id, status, scheduled_for, id) WHERE status IN ('queued', 'launching', 'running');

CREATE INDEX ix_scheduled_task_runs_owner_user_id ON scheduled_task_runs (owner_user_id);

CREATE INDEX ix_scheduled_task_runs_history ON scheduled_task_runs (project_id, owner_user_id, task_id, created_at DESC, id DESC);

CREATE INDEX ix_scheduled_task_runs_project_id ON scheduled_task_runs (project_id);

CREATE TABLE asset_catalog_state (
    id SMALLINT DEFAULT 1 NOT NULL,
    generation BIGINT DEFAULT 1 NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_asset_catalog_state_singleton CHECK (id = 1),
    CONSTRAINT ck_asset_catalog_state_generation CHECK (generation >= 1)
);

CREATE TABLE system_asset_upgrade_audit (
    id UUID NOT NULL,
    asset_kind VARCHAR(16) NOT NULL,
    asset_id UUID NOT NULL,
    version_id UUID NOT NULL,
    before_checksum VARCHAR(64) NOT NULL,
    after_checksum VARCHAR(64) NOT NULL,
    package_digest VARCHAR(64) NOT NULL,
    operator_identity VARCHAR(255) NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_asset_upgrade_audit_kind CHECK (asset_kind IN ('agent', 'skill', 'mcp')),
    CONSTRAINT ck_system_asset_upgrade_audit_before_checksum CHECK (before_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_asset_upgrade_audit_after_checksum CHECK (after_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_asset_upgrade_audit_package_digest CHECK (package_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE projects (
    id UUID NOT NULL,
    slug VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    description VARCHAR(500) DEFAULT '' NOT NULL,
    icon VARCHAR(32) DEFAULT 'folder' NOT NULL,
    status VARCHAR(32) DEFAULT 'active' NOT NULL,
    deletion_requested_at TIMESTAMP WITH TIME ZONE,
    deletion_effective_at TIMESTAMP WITH TIME ZONE,
    deletion_requested_by_user_id VARCHAR(36),
    is_suspended BOOLEAN DEFAULT false NOT NULL,
    membership_version BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_projects_slug_length CHECK (char_length(slug) BETWEEN 3 AND 63),
    CONSTRAINT ck_projects_slug_lowercase CHECK (slug = lower(slug)),
    CONSTRAINT ck_projects_slug_format CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    CONSTRAINT ck_projects_status CHECK (status IN ('active', 'pending_deletion')),
    CONSTRAINT ck_projects_membership_version CHECK (membership_version >= 1),
    CONSTRAINT uq_projects_slug UNIQUE (slug),
    CONSTRAINT fk_projects_deletion_requested_by_user_id_users FOREIGN KEY(deletion_requested_by_user_id) REFERENCES users (id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

CREATE TABLE auth_sessions (
    session_id_hash CHAR(64) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (session_id_hash),
    CONSTRAINT ck_auth_sessions_hash CHECK (session_id_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_auth_sessions_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_auth_sessions_last_seen CHECK (last_seen_at >= created_at AND last_seen_at <= expires_at),
    CONSTRAINT ck_auth_sessions_revoked_at CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_auth_sessions_user_active ON auth_sessions (user_id, expires_at) WHERE revoked_at IS NULL;

CREATE INDEX ix_auth_sessions_revoked_at ON auth_sessions (revoked_at, session_id_hash) WHERE revoked_at IS NOT NULL;

CREATE INDEX ix_auth_sessions_expires_at ON auth_sessions (expires_at, session_id_hash);

CREATE TABLE job_attempts (
    id UUID NOT NULL,
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_id UUID NOT NULL,
    lease_token_hash CHAR(64) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    execution_started_at TIMESTAMP WITH TIME ZONE,
    heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    outcome VARCHAR(16),
    public_error_code VARCHAR(64),
    checkpoint_cursor VARCHAR(128),
    stream_cursor BIGINT,
    PRIMARY KEY (id),
    CONSTRAINT uq_job_attempts_number UNIQUE (job_id, attempt_number),
    CONSTRAINT uq_job_attempts_id_job UNIQUE (id, job_id),
    CONSTRAINT ck_job_attempts_number CHECK (attempt_number >= 1),
    CONSTRAINT ck_job_attempts_outcome CHECK (outcome IS NULL OR outcome IN ('succeeded', 'retry', 'cancelled', 'failed', 'lease_lost', 'dead')),
    CONSTRAINT ck_job_attempts_stream_cursor CHECK (stream_cursor IS NULL OR stream_cursor >= 0),
    FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
    FOREIGN KEY(worker_id) REFERENCES worker_nodes (id) ON DELETE RESTRICT
);

CREATE INDEX ix_job_attempts_job_started ON job_attempts (job_id, started_at DESC);

CREATE TABLE run_event_invariants (
    id BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    seq BIGINT NOT NULL,
    is_stream_terminal BOOLEAN DEFAULT false NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_events_thread_seq UNIQUE (thread_id, seq),
    CONSTRAINT uq_run_events_private_seq UNIQUE (project_id, owner_user_id, thread_id, run_id, seq),
    CONSTRAINT fk_run_event_invariants_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE
);

CREATE INDEX ix_run_event_invariants_created_at ON run_event_invariants (created_at);

CREATE UNIQUE INDEX uq_run_events_stream_terminal ON run_event_invariants (project_id, owner_user_id, thread_id, run_id) WHERE is_stream_terminal;

CREATE TABLE system_runtime_policy_catalog_state (
    id SMALLINT DEFAULT 1 NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    updated_by_user_id VARCHAR(36),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_runtime_policy_catalog_state_singleton CHECK (id = 1),
    CONSTRAINT ck_system_runtime_policy_catalog_state_revision CHECK (revision >= 1),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE system_runtime_policies (
    section VARCHAR(32) NOT NULL,
    current_version_id UUID NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (section),
    CONSTRAINT ck_system_runtime_policies_section CHECK (section IN ('agent_runtime', 'auth', 'automations', 'memory_document', 'quotas')),
    CONSTRAINT ck_system_runtime_policies_revision CHECK (revision >= 1),
    CONSTRAINT uq_system_runtime_policies_current_version UNIQUE (section, current_version_id),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE system_model_catalog_state (
    id SMALLINT DEFAULT 1 NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    default_model_config_id UUID,
    updated_by_user_id VARCHAR(36),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_model_catalog_state_singleton CHECK (id = 1),
    CONSTRAINT ck_system_model_catalog_state_revision CHECK (revision >= 1),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE system_model_configs (
    id UUID NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(16) DEFAULT 'suspended' NOT NULL,
    provider_id UUID NOT NULL,
    provider_adapter VARCHAR(64) NOT NULL,
    provider_model VARCHAR(255) NOT NULL,
    max_input_tokens BIGINT NOT NULL,
    settings JSONB DEFAULT '{}'::jsonb NOT NULL,
    supports_thinking BOOLEAN DEFAULT false NOT NULL,
    supports_reasoning_effort BOOLEAN DEFAULT false NOT NULL,
    supports_vision BOOLEAN DEFAULT false NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    current_secret_generation_id UUID,
    secret_revision BIGINT DEFAULT 0 NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_model_configs_status CHECK (status IN ('active', 'suspended')),
    CONSTRAINT ck_system_model_configs_deleted_state CHECK (deleted_at IS NULL OR status = 'suspended'),
    CONSTRAINT ck_system_model_configs_settings_object CHECK (jsonb_typeof(settings) = 'object'),
    CONSTRAINT ck_system_model_configs_max_input_tokens CHECK (max_input_tokens BETWEEN 1 AND 2000000),
    CONSTRAINT ck_system_model_configs_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_model_configs_secret_revision CHECK (secret_revision >= 0),
    CONSTRAINT ck_system_model_configs_revision CHECK (revision >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_system_model_configs_status_created ON system_model_configs (status, created_at DESC, id DESC) WHERE deleted_at IS NULL;

CREATE INDEX ix_system_model_configs_provider ON system_model_configs (provider_id);

CREATE TABLE audit_logs (
    id UUID NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    actor_user_id VARCHAR(36),
    actor_process VARCHAR(32),
    actor_platform_role VARCHAR(32),
    project_id UUID,
    action VARCHAR(64) NOT NULL,
    target_kind VARCHAR(32) NOT NULL,
    target_ref_key_id VARCHAR(64) NOT NULL,
    target_ref_hmac CHAR(64) NOT NULL,
    outcome VARCHAR(16) NOT NULL,
    public_error_code VARCHAR(64),
    request_id VARCHAR(128),
    job_id UUID,
    attempt_id UUID,
    metadata_json JSON DEFAULT '{}' NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_audit_logs_actor CHECK ((actor_user_id IS NOT NULL AND actor_process IS NULL) OR (actor_user_id IS NULL AND actor_process IS NOT NULL)),
    CONSTRAINT ck_audit_logs_outcome CHECK (outcome IN ('success', 'rejected', 'failed')),
    FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
    FOREIGN KEY(attempt_id) REFERENCES job_attempts (id) ON DELETE RESTRICT
);

CREATE INDEX ix_audit_logs_platform_cursor ON audit_logs (occurred_at DESC, id DESC);

CREATE INDEX ix_audit_logs_project_cursor ON audit_logs (project_id, occurred_at DESC, id DESC);

CREATE TABLE project_memberships (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    role VARCHAR(16) NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    ended_at TIMESTAMP WITH TIME ZONE,
    retention_until TIMESTAMP WITH TIME ZONE,
    ended_by_user_id VARCHAR(36),
    end_reason VARCHAR(16),
    version BIGINT DEFAULT 1 NOT NULL,
    activation_generation BIGINT DEFAULT 1 NOT NULL,
    is_pinned BOOLEAN DEFAULT false NOT NULL,
    last_entered_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_project_memberships_role CHECK (role IN ('admin', 'editor', 'runner', 'viewer', 'channel_guest')),
    CONSTRAINT ck_project_memberships_status CHECK (status IN ('active', 'left', 'removed')),
    CONSTRAINT ck_project_memberships_end_reason CHECK (end_reason IS NULL OR end_reason IN ('left', 'removed')),
    CONSTRAINT ck_project_memberships_version CHECK (version >= 1),
    CONSTRAINT ck_project_memberships_activation_generation CHECK (activation_generation >= 1),
    CONSTRAINT uq_project_memberships_project_user UNIQUE (project_id, user_id),
    CONSTRAINT uq_project_memberships_guest_identity UNIQUE (project_id, user_id, id, role),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_memberships_ended_by_user_id_users FOREIGN KEY(ended_by_user_id) REFERENCES users (id)
);

CREATE INDEX ix_project_memberships_user_id ON project_memberships (user_id);

CREATE TABLE project_channel_instances (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    provider VARCHAR(32) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    desired_status VARCHAR(16) DEFAULT 'disabled' NOT NULL,
    observed_status VARCHAR(16) DEFAULT 'stopped' NOT NULL,
    public_config JSONB DEFAULT '{}'::jsonb NOT NULL,
    provider_identity_digest VARCHAR(64) NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    last_error_code VARCHAR(64),
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_project_channel_instances PRIMARY KEY (id),
    CONSTRAINT ck_project_channel_instances_provider CHECK (provider ~ '^[a-z][a-z0-9_-]{0,31}$'),
    CONSTRAINT ck_project_channel_instances_desired_status CHECK (desired_status IN ('enabled', 'disabled')),
    CONSTRAINT ck_project_channel_instances_observed_status CHECK (observed_status IN ('stopped', 'starting', 'running', 'stopping', 'error')),
    CONSTRAINT ck_project_channel_instances_public_config CHECK (jsonb_typeof(public_config) = 'object' AND public_config::text !~* '"[^"]*(secret|token|password|api_key|private_key)[^"]*"[[:space:]]*:'),
    CONSTRAINT ck_project_channel_instances_identity_digest CHECK (provider_identity_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_channel_instances_revision CHECK (revision >= 1),
    CONSTRAINT uq_project_channel_instances_project_id UNIQUE (project_id, id),
    CONSTRAINT uq_project_channel_instances_project_provider UNIQUE (project_id, id, provider),
    CONSTRAINT fk_project_channel_instances_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_instances_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_instances_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_project_channel_instances_runtime ON project_channel_instances (desired_status, observed_status, id) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX uq_project_channel_instances_live_provider ON project_channel_instances (project_id, provider) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX uq_project_channel_instances_live_identity ON project_channel_instances (provider, provider_identity_digest) WHERE deleted_at IS NULL;

CREATE TABLE project_channel_secret_tombstones (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    channel_instance_id UUID NOT NULL,
    destroyed_generation_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    reason VARCHAR(24) NOT NULL,
    destroyed_by_user_id VARCHAR(36) NOT NULL,
    destroyed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_project_channel_secret_tombstones_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_secret_tombstones_actor FOREIGN KEY(destroyed_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_channel_secret_tombstones_generation UNIQUE (project_id, channel_instance_id, destroyed_generation_id),
    CONSTRAINT ck_project_channel_secret_tombstones_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_channel_secret_tombstones_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_channel_secret_tombstones_reason CHECK (reason IN ('replace', 'clear', 'delete', 'recipient_change'))
);

CREATE TABLE project_invitations (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    invited_email VARCHAR(320) NOT NULL,
    role VARCHAR(16) NOT NULL,
    token_hash CHAR(64) NOT NULL,
    status VARCHAR(16) DEFAULT 'pending' NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    redeemed_by_user_id VARCHAR(36),
    redeemed_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_project_invitations_role CHECK (role IN ('editor', 'runner', 'viewer')),
    CONSTRAINT ck_project_invitations_status CHECK (status IN ('pending', 'redeemed', 'revoked', 'expired')),
    CONSTRAINT ck_project_invitations_token_hash CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_invitations_version CHECK (version >= 1),
    CONSTRAINT uq_project_invitations_token_hash UNIQUE (token_hash),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(redeemed_by_user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX uq_project_invitations_pending_email ON project_invitations (project_id, invited_email) WHERE status = 'pending';

CREATE TABLE project_quotas (
    project_id UUID NOT NULL,
    member_limit INTEGER,
    storage_bytes_limit BIGINT,
    concurrent_run_limit INTEGER,
    mcp_calls_daily_limit INTEGER,
    version BIGINT DEFAULT 1 NOT NULL,
    updated_by_user_id VARCHAR(36),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id),
    CONSTRAINT ck_project_quotas_limits CHECK ((member_limit IS NULL OR member_limit >= 1) AND (storage_bytes_limit IS NULL OR storage_bytes_limit >= 0) AND (concurrent_run_limit IS NULL OR concurrent_run_limit >= 1) AND (mcp_calls_daily_limit IS NULL OR mcp_calls_daily_limit >= 0) AND version >= 1),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE project_usage_counters (
    project_id UUID NOT NULL,
    dimension VARCHAR(32) NOT NULL,
    bucket VARCHAR(32) DEFAULT 'lifetime' NOT NULL,
    used BIGINT DEFAULT 0 NOT NULL,
    reserved BIGINT DEFAULT 0 NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, dimension, bucket),
    CONSTRAINT ck_project_usage_counters_dimension CHECK (dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')),
    CONSTRAINT ck_project_usage_counters_values CHECK (used >= 0 AND reserved >= 0 AND version >= 1),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE TABLE project_usage_ledger (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    dimension VARCHAR(32) NOT NULL,
    delta BIGINT NOT NULL,
    bucket VARCHAR(32) NOT NULL,
    source_kind VARCHAR(32) NOT NULL,
    source_ref_key_id VARCHAR(64) NOT NULL,
    source_ref_hmac CHAR(64) NOT NULL,
    idempotency_key CHAR(64) NOT NULL,
    request_id VARCHAR(128),
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_project_usage_ledger_idempotency UNIQUE (project_id, dimension, idempotency_key),
    CONSTRAINT ck_project_usage_ledger_dimension CHECK (dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')),
    CONSTRAINT ck_project_usage_ledger_delta CHECK (delta <> 0),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
);

CREATE INDEX ix_project_usage_ledger_project_cursor ON project_usage_ledger (project_id, occurred_at DESC, id DESC);

CREATE TABLE agents (
    id UUID NOT NULL,
    scope VARCHAR(16) NOT NULL,
    project_id UUID,
    slug VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    definition_id UUID NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    soul TEXT DEFAULT '' NOT NULL,
    model_ref VARCHAR(255) DEFAULT 'default' NOT NULL,
    model_settings JSONB DEFAULT '{}'::jsonb NOT NULL,
    tool_groups JSONB DEFAULT '[]'::jsonb NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    agents_instructions TEXT DEFAULT '' NOT NULL,
    identity TEXT DEFAULT '' NOT NULL,
    user_context TEXT DEFAULT '' NOT NULL,
    payload_schema_version INTEGER DEFAULT 4 NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    source_key VARCHAR(255),
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_agents_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_agents_status CHECK (status IN ('active', 'archived', 'suspended')),
    CONSTRAINT ck_agents_payload_schema_version CHECK (payload_schema_version = 4),
    CONSTRAINT ck_agents_model_settings CHECK (
            jsonb_typeof(model_settings) = 'object'
            AND model_settings - 'temperature' - 'max_tokens'
                - 'thinking_enabled' - 'reasoning_effort' = '{}'::jsonb
            AND (
                NOT (model_settings ? 'temperature')
                OR (
                    jsonb_typeof(model_settings->'temperature') = 'number'
                    AND (model_settings->>'temperature')::numeric BETWEEN 0 AND 2
                )
            )
            AND (
                NOT (model_settings ? 'max_tokens')
                OR (
                    jsonb_typeof(model_settings->'max_tokens') = 'number'
                    AND (model_settings->>'max_tokens')::numeric
                        = trunc((model_settings->>'max_tokens')::numeric)
                    AND (model_settings->>'max_tokens')::numeric
                        BETWEEN 1 AND 200000
                )
            )
            AND (
                NOT (model_settings ? 'thinking_enabled')
                OR jsonb_typeof(model_settings->'thinking_enabled') = 'boolean'
            )
            AND (
                NOT (model_settings ? 'reasoning_effort')
                OR (
                    jsonb_typeof(model_settings->'reasoning_effort') = 'string'
                    AND model_settings->>'reasoning_effort'
                        IN ('low', 'medium', 'high')
                )
            )
            ),
    CONSTRAINT ck_agents_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_agents_revision CHECK (revision >= 1),
    CONSTRAINT uq_agents_definition_id UNIQUE (definition_id),
    CONSTRAINT uq_agents_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_agents_project_id_id UNIQUE (project_id, id),
    CONSTRAINT uq_agents_source_key UNIQUE (source_key),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX uq_agents_system_slug ON agents (lower(slug)) WHERE scope = 'system';

CREATE UNIQUE INDEX uq_agents_project_slug ON agents (project_id, lower(slug)) WHERE scope = 'project' AND status != 'archived';

CREATE TABLE mcp_servers (
    id UUID NOT NULL,
    scope VARCHAR(16) NOT NULL,
    project_id UUID,
    slug VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    current_published_version_id UUID,
    version BIGINT DEFAULT 1 NOT NULL,
    source_key VARCHAR(255),
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_mcp_servers_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_mcp_servers_status CHECK (status IN ('active', 'archived', 'suspended')),
    CONSTRAINT ck_mcp_servers_version CHECK (version >= 1),
    CONSTRAINT uq_mcp_servers_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_mcp_servers_source_key UNIQUE (source_key),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX uq_mcp_servers_system_slug ON mcp_servers (lower(slug)) WHERE scope = 'system';

CREATE UNIQUE INDEX uq_mcp_servers_project_slug ON mcp_servers (project_id, lower(slug)) WHERE scope = 'project';

CREATE TABLE project_mcp_secret_tombstones (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    slot_id UUID NOT NULL,
    destroyed_generation_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    reason VARCHAR(24) NOT NULL,
    destroyed_by_user_id VARCHAR(36) NOT NULL,
    destroyed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_project_mcp_secret_tombstones_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_tombstones_actor FOREIGN KEY(destroyed_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_mcp_secret_tombstones_generation UNIQUE (project_id, mcp_server_id, mcp_server_version_id, slot_id, destroyed_generation_id),
    CONSTRAINT ck_project_mcp_secret_tombstones_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_mcp_secret_tombstones_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_secret_tombstones_reason CHECK (reason IN ('replace', 'clear', 'definition_change', 'version_purge'))
);

CREATE TABLE skills (
    id UUID NOT NULL,
    scope VARCHAR(16) NOT NULL,
    project_id UUID,
    slug VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    current_version_id UUID,
    revision BIGINT DEFAULT 1 NOT NULL,
    source_key VARCHAR(255),
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_skills_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_skills_status CHECK (status IN ('active', 'archived', 'suspended')),
    CONSTRAINT ck_skills_revision CHECK (revision >= 1),
    CONSTRAINT uq_skills_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_skills_project_id_id UNIQUE (project_id, id),
    CONSTRAINT uq_skills_source_key UNIQUE (source_key),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX uq_skills_project_display_name ON skills (project_id, lower(display_name)) WHERE scope = 'project' AND status != 'archived';

CREATE UNIQUE INDEX uq_skills_system_slug ON skills (lower(slug)) WHERE scope = 'system';

CREATE UNIQUE INDEX uq_skills_project_slug ON skills (project_id, lower(slug)) WHERE scope = 'project' AND status != 'archived';

CREATE TABLE project_skill_secret_tombstones (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    secret_name VARCHAR(255) NOT NULL,
    destroyed_generation_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    reason VARCHAR(24) NOT NULL,
    destroyed_by_user_id VARCHAR(36) NOT NULL,
    destroyed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_project_skill_secret_tombstones_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_secret_tombstones_actor FOREIGN KEY(destroyed_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_skill_secret_tombstones_generation UNIQUE (project_id, skill_id, skill_version_id, secret_name, destroyed_generation_id),
    CONSTRAINT ck_project_skill_secret_tombstones_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_skill_secret_tombstones_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_skill_secret_tombstones_reason CHECK (reason IN ('replace', 'clear'))
);

CREATE TABLE system_runtime_policy_versions (
    id UUID NOT NULL,
    section VARCHAR(32) NOT NULL,
    version_number BIGINT NOT NULL,
    schema_version SMALLINT NOT NULL,
    value JSONB DEFAULT '{}'::jsonb NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    supersedes_version_id UUID,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_runtime_policy_versions_section CHECK (section IN ('agent_runtime', 'auth', 'automations', 'memory_document', 'quotas')),
    CONSTRAINT ck_system_runtime_policy_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_system_runtime_policy_versions_schema CHECK (schema_version >= 1),
    CONSTRAINT ck_system_runtime_policy_versions_value_object CHECK (jsonb_typeof(value) = 'object'),
    CONSTRAINT ck_system_runtime_policy_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_system_runtime_policy_versions_number UNIQUE (section, version_number),
    CONSTRAINT uq_system_runtime_policy_versions_section_id UNIQUE (section, id),
    CONSTRAINT uq_system_runtime_policy_versions_exact UNIQUE (section, id, schema_version, payload_checksum),
    CONSTRAINT fk_system_runtime_policy_versions_policy FOREIGN KEY(section) REFERENCES system_runtime_policies (section) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT fk_system_runtime_policy_versions_supersedes FOREIGN KEY(section, supersedes_version_id) REFERENCES system_runtime_policy_versions (section, id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_system_runtime_policy_versions_created_at ON system_runtime_policy_versions (section, created_at);

CREATE TABLE system_model_secret_generations (
    id UUID NOT NULL,
    model_config_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_model_secret_generations_revision CHECK (revision >= 1),
    CONSTRAINT ck_system_model_secret_generations_nonce_size CHECK (octet_length(nonce) = 12),
    CONSTRAINT ck_system_model_secret_generations_ciphertext_size CHECK (octet_length(ciphertext) >= 16),
    CONSTRAINT ck_system_model_secret_generations_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_system_model_secret_generations_revision UNIQUE (model_config_id, revision),
    CONSTRAINT uq_system_model_secret_generations_model_id UNIQUE (model_config_id, id),
    FOREIGN KEY(model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE system_model_secret_tombstones (
    generation_id UUID NOT NULL,
    model_config_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    reason VARCHAR(32) NOT NULL,
    destroyed_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    destroyed_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (generation_id),
    CONSTRAINT ck_system_model_secret_tombstones_revision CHECK (revision >= 1),
    CONSTRAINT ck_system_model_secret_tombstones_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_model_secret_tombstones_reason CHECK (reason IN ('replaced', 'cleared', 'recipient_changed')),
    CONSTRAINT uq_system_model_secret_tombstones_revision UNIQUE (model_config_id, revision),
    FOREIGN KEY(model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT,
    FOREIGN KEY(destroyed_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE project_default_agents (
    project_id UUID NOT NULL,
    agent_asset_id UUID,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id),
    CONSTRAINT fk_project_default_agents_project_agent FOREIGN KEY(project_id, agent_asset_id) REFERENCES agents (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_project_default_agents_revision CHECK (revision >= 1),
    CONSTRAINT fk_project_default_agents_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_default_agents_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_default_agents_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE project_channel_group_binding_challenges (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    channel_instance_id UUID NOT NULL,
    provider VARCHAR(32) NOT NULL,
    code_digest VARCHAR(64) NOT NULL,
    agent_asset_id UUID NOT NULL,
    agent_scope VARCHAR(16) NOT NULL,
    membership_id UUID NOT NULL,
    membership_version BIGINT NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consumed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_project_channel_group_binding_challenges PRIMARY KEY (id),
    CONSTRAINT uq_project_channel_group_binding_challenges_code_digest UNIQUE (code_digest),
    CONSTRAINT ck_project_channel_group_binding_challenges_provider CHECK (provider ~ '^[a-z][a-z0-9_-]{0,31}$'),
    CONSTRAINT ck_project_channel_group_binding_challenges_digest CHECK (code_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_channel_group_binding_challenges_agent_scope CHECK (agent_scope IN ('project', 'system')),
    CONSTRAINT ck_project_channel_group_binding_challenges_membership_version CHECK (membership_version >= 1),
    CONSTRAINT ck_project_channel_group_binding_challenges_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_project_channel_group_binding_challenges_consumed CHECK (consumed_at IS NULL OR consumed_at >= created_at),
    CONSTRAINT fk_project_channel_group_binding_challenges_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_group_binding_challenges_membership FOREIGN KEY(membership_id) REFERENCES project_memberships (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_group_binding_challenges_creator_membership FOREIGN KEY(project_id, created_by_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_group_binding_challenges_agent FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT
);

CREATE INDEX ix_project_channel_group_binding_challenges_pending ON project_channel_group_binding_challenges (channel_instance_id, provider, expires_at) WHERE consumed_at IS NULL;

CREATE INDEX ix_project_channel_group_binding_challenges_membership ON project_channel_group_binding_challenges (project_id, membership_id, membership_version);

CREATE TABLE project_channel_group_bindings (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    channel_instance_id UUID NOT NULL,
    provider VARCHAR(32) NOT NULL,
    external_group_ref CHAR(64) NOT NULL,
    external_group_name VARCHAR(256),
    agent_scope VARCHAR(16),
    agent_asset_id UUID,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    first_activity_at TIMESTAMP WITH TIME ZONE,
    last_activity_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT ck_project_channel_group_bindings_provider CHECK (provider ~ '^[a-z][a-z0-9_-]{0,31}$'),
    CONSTRAINT ck_project_channel_group_bindings_external_ref CHECK (external_group_ref ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_channel_group_bindings_agent_scope CHECK (agent_scope IS NULL OR agent_scope IN ('system', 'project')),
    CONSTRAINT ck_project_channel_group_bindings_agent_ref_pair CHECK ((agent_asset_id IS NULL) = (agent_scope IS NULL)),
    CONSTRAINT ck_project_channel_group_bindings_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_project_channel_group_bindings_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_channel_group_bindings_activity CHECK ((first_activity_at IS NULL AND last_activity_at IS NULL) OR (first_activity_at IS NOT NULL AND last_activity_at IS NOT NULL AND first_activity_at <= last_activity_at)),
    CONSTRAINT ck_project_channel_group_bindings_deleted_status CHECK (deleted_at IS NULL OR status = 'disabled'),
    CONSTRAINT ck_project_channel_group_bindings_agent_lifecycle CHECK ((deleted_at IS NULL) = (agent_asset_id IS NOT NULL)),
    CONSTRAINT uq_project_channel_group_bindings_project_id UNIQUE (project_id, id),
    CONSTRAINT fk_project_channel_group_bindings_instance FOREIGN KEY(project_id, channel_instance_id, provider) REFERENCES project_channel_instances (project_id, id, provider) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_group_bindings_agent FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_group_bindings_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_group_bindings_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_project_channel_group_bindings_live_group ON project_channel_group_bindings (channel_instance_id, external_group_ref) WHERE deleted_at IS NULL;

CREATE INDEX ix_project_channel_group_bindings_project_status ON project_channel_group_bindings (project_id, status, id) WHERE deleted_at IS NULL;

CREATE TABLE project_channel_instance_leases (
    channel_instance_id UUID NOT NULL,
    project_id UUID NOT NULL,
    holder_id UUID NOT NULL,
    lease_token_hash VARCHAR(64) NOT NULL,
    fencing_generation BIGINT NOT NULL,
    lease_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_heartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_project_channel_instance_leases PRIMARY KEY (channel_instance_id),
    CONSTRAINT ck_project_channel_instance_leases_token_hash CHECK (lease_token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_channel_instance_leases_generation CHECK (fencing_generation >= 1),
    CONSTRAINT fk_project_channel_instance_leases_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE CASCADE
);

CREATE INDEX ix_project_channel_instance_leases_expiry ON project_channel_instance_leases (lease_expires_at, channel_instance_id);

CREATE TABLE project_channel_secret_states (
    project_id UUID NOT NULL,
    channel_instance_id UUID NOT NULL,
    current_generation_id UUID,
    revision BIGINT DEFAULT 0 NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_project_channel_secret_states PRIMARY KEY (project_id, channel_instance_id),
    CONSTRAINT ck_project_channel_secret_states_revision CHECK (revision >= 0),
    CONSTRAINT fk_project_channel_secret_states_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_secret_states_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE TABLE project_channel_secret_generations (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    channel_instance_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_project_channel_secret_generations_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_secret_generations_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_channel_secret_generations_owner_id UNIQUE (project_id, channel_instance_id, id),
    CONSTRAINT uq_project_channel_secret_generations_revision UNIQUE (project_id, channel_instance_id, revision),
    CONSTRAINT ck_project_channel_secret_generations_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_channel_secret_generations_envelope CHECK (octet_length(nonce) = 12 AND octet_length(ciphertext) >= 16),
    CONSTRAINT ck_project_channel_secret_generations_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE channel_connections (
    id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    external_account_id VARCHAR(128) NOT NULL,
    external_account_name VARCHAR(256),
    workspace_id VARCHAR(128) NOT NULL,
    workspace_name VARCHAR(256),
    bot_user_id VARCHAR(128),
    scopes_json JSON NOT NULL,
    capabilities_json JSON NOT NULL,
    metadata_json JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_seen_at TIMESTAMP WITH TIME ZONE,
    last_error_at TIMESTAMP WITH TIME ZONE,
    project_id UUID NOT NULL,
    channel_instance_id UUID,
    frozen_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT fk_channel_connections_project_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_channel_connections_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT fk_channel_connections_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_connections_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_connections_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT ck_channel_connections_status CHECK (status IN ('connected', 'frozen', 'revoked'))
);

CREATE INDEX ix_channel_connections_project_id ON channel_connections (project_id);

CREATE UNIQUE INDEX uq_channel_connection_owner_instance_identity ON channel_connections (project_id, owner_user_id, channel_instance_id, external_account_id, workspace_id) WHERE channel_instance_id IS NOT NULL;

CREATE UNIQUE INDEX uq_channel_connection_active_instance_identity ON channel_connections (channel_instance_id, external_account_id, workspace_id) WHERE status = 'connected' AND channel_instance_id IS NOT NULL;

CREATE INDEX ix_channel_connections_channel_instance_id ON channel_connections (channel_instance_id);

CREATE INDEX ix_channel_connections_provider ON channel_connections (provider);

CREATE UNIQUE INDEX uq_channel_connection_owner_legacy_identity ON channel_connections (project_id, owner_user_id, provider, external_account_id, workspace_id) WHERE channel_instance_id IS NULL;

CREATE INDEX ix_channel_connections_owner_user_id ON channel_connections (owner_user_id);

CREATE INDEX idx_channel_connections_event_lookup ON channel_connections (channel_instance_id, provider, workspace_id, bot_user_id);

CREATE UNIQUE INDEX uq_channel_connection_active_legacy_identity ON channel_connections (provider, external_account_id, workspace_id) WHERE status = 'connected' AND channel_instance_id IS NULL;

CREATE TABLE channel_oauth_states (
    state_hash VARCHAR(128) NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    code_verifier_encrypted TEXT,
    nonce_hash VARCHAR(128),
    redirect_after TEXT,
    requested_scopes_json JSON NOT NULL,
    metadata_json JSON NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consumed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    channel_instance_id UUID,
    PRIMARY KEY (state_hash),
    CONSTRAINT fk_channel_oauth_states_project_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_oauth_states_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_oauth_states_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_oauth_states_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT
);

CREATE INDEX ix_channel_oauth_states_project_id ON channel_oauth_states (project_id);

CREATE INDEX ix_channel_oauth_states_owner_user_id ON channel_oauth_states (owner_user_id);

CREATE INDEX ix_channel_oauth_states_provider ON channel_oauth_states (provider);

CREATE INDEX ix_channel_oauth_states_channel_instance_id ON channel_oauth_states (channel_instance_id);

CREATE TABLE threads_meta (
    thread_id VARCHAR(64) NOT NULL,
    assistant_id VARCHAR(128),
    owner_user_id VARCHAR(36) NOT NULL,
    display_name VARCHAR(256),
    status VARCHAR(20) NOT NULL,
    metadata_json JSON NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    agent_asset_id UUID NOT NULL,
    agent_scope VARCHAR(16) NOT NULL,
    frozen_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    memory_sealed_at TIMESTAMP WITH TIME ZONE,
    checkpoint_delete_status VARCHAR(24) DEFAULT 'not_requested' NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    thread_kind VARCHAR(16) DEFAULT 'chat' NOT NULL,
    PRIMARY KEY (thread_id),
    CONSTRAINT uq_threads_meta_private_scope UNIQUE (project_id, owner_user_id, thread_id),
    CONSTRAINT fk_threads_meta_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_threads_meta_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_threads_meta_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_threads_meta_agent_asset FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT ck_threads_meta_agent_scope CHECK (agent_scope IN ('system', 'project')),
    CONSTRAINT ck_threads_meta_kind CHECK (thread_kind IN ('chat', 'skill_builder')),
    CONSTRAINT ck_threads_meta_checkpoint_delete_status CHECK (checkpoint_delete_status IN ('not_requested', 'pending', 'complete', 'retry_required')),
    CONSTRAINT ck_threads_meta_version CHECK (version >= 1)
);

CREATE INDEX ix_threads_meta_owner_user_id ON threads_meta (owner_user_id);

CREATE INDEX ix_threads_meta_project_id ON threads_meta (project_id);

CREATE INDEX ix_threads_meta_assistant_id ON threads_meta (assistant_id);

CREATE TABLE feedback (
    feedback_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    message_id VARCHAR(64),
    rating INTEGER NOT NULL,
    comment TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    PRIMARY KEY (feedback_id),
    CONSTRAINT uq_feedback_private_run_owner UNIQUE (project_id, owner_user_id, thread_id, run_id),
    CONSTRAINT fk_feedback_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_feedback_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_feedback_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_feedback_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE
);

CREATE INDEX ix_feedback_owner_user_id ON feedback (owner_user_id);

CREATE INDEX ix_feedback_project_id ON feedback (project_id);

CREATE INDEX ix_feedback_thread_id ON feedback (thread_id);

CREATE INDEX ix_feedback_run_id ON feedback (run_id);

CREATE TABLE run_events (
    id BIGSERIAL NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    category VARCHAR(16) NOT NULL,
    content TEXT NOT NULL,
    event_metadata JSON NOT NULL,
    seq BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    PRIMARY KEY (id, created_at),
    CONSTRAINT fk_run_events_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_events_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_events_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_events_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE
)
 PARTITION BY RANGE (created_at);

CREATE INDEX ix_run_events_stream_terminal ON run_events (project_id, owner_user_id, thread_id, run_id) WHERE category = 'stream' AND event_type = 'stream.end';

CREATE INDEX ix_events_run ON run_events (thread_id, run_id, seq);

CREATE INDEX ix_events_thread_cat_seq ON run_events (thread_id, category, seq);

CREATE INDEX ix_run_events_owner_user_id ON run_events (owner_user_id);

CREATE INDEX ix_run_events_project_id ON run_events (project_id);

CREATE TABLE user_notifications (
    id UUID NOT NULL,
    recipient_user_id VARCHAR(36) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    project_invitation_id UUID NOT NULL,
    read_at TIMESTAMP WITH TIME ZONE,
    acted_at TIMESTAMP WITH TIME ZONE,
    version BIGINT DEFAULT 1 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_user_notifications_kind CHECK (kind = 'project_invitation'),
    CONSTRAINT ck_user_notifications_version CHECK (version >= 1),
    CONSTRAINT ck_user_notifications_read_at CHECK (read_at IS NULL OR read_at >= created_at),
    CONSTRAINT ck_user_notifications_acted_at CHECK (acted_at IS NULL OR acted_at >= created_at),
    CONSTRAINT ck_user_notifications_acted_is_read CHECK (acted_at IS NULL OR read_at IS NOT NULL),
    CONSTRAINT uq_user_notifications_project_invitation_id UNIQUE (project_invitation_id),
    FOREIGN KEY(recipient_user_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY(project_invitation_id) REFERENCES project_invitations (id) ON DELETE CASCADE
);

CREATE INDEX ix_user_notifications_recipient_cursor ON user_notifications (recipient_user_id, created_at DESC, id DESC);

CREATE INDEX ix_user_notifications_recipient_unread ON user_notifications (recipient_user_id, created_at) WHERE read_at IS NULL;

CREATE TABLE memory_history_entries (
    id UUID NOT NULL,
    sequence BIGINT GENERATED BY DEFAULT AS IDENTITY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    origin VARCHAR(8) DEFAULT 'snip' NOT NULL,
    source_run_id VARCHAR(64),
    source_checkpoint_id VARCHAR(128),
    committed_checkpoint_id VARCHAR(128),
    source_digest CHAR(64) NOT NULL,
    status VARCHAR(16) DEFAULT 'pending' NOT NULL,
    tagged_text TEXT,
    content_digest CHAR(64) NOT NULL,
    preference_version BIGINT NOT NULL,
    snip_prompt_version VARCHAR(64) NOT NULL,
    summary_model_config_id UUID,
    summary_model_payload_checksum CHAR(64),
    summary_model_secret_generation_id UUID,
    summary_model_secret_envelope_digest CHAR(64),
    dream_job_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    consumed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT fk_memory_history_entries_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_history_entries_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_history_entries_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_memory_history_entries_sequence UNIQUE (sequence),
    CONSTRAINT uq_memory_history_entries_scope UNIQUE (project_id, owner_user_id, namespace, id),
    CONSTRAINT uq_memory_history_entries_source UNIQUE (project_id, owner_user_id, namespace, thread_id, source_digest),
    CONSTRAINT fk_memory_history_entries_dream_job FOREIGN KEY(dream_job_id, project_id, owner_user_id, namespace) REFERENCES jobs (id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_history_entries_summary_model FOREIGN KEY(summary_model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_history_entries_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_history_entries_origin CHECK (origin IN ('snip', 'tool')),
    CONSTRAINT ck_memory_history_entries_origin_source CHECK ((origin = 'snip' AND source_run_id IS NULL AND source_checkpoint_id IS NOT NULL AND source_checkpoint_id <> '' AND committed_checkpoint_id IS NOT NULL AND committed_checkpoint_id <> '' AND summary_model_config_id IS NOT NULL AND summary_model_payload_checksum IS NOT NULL) OR (origin = 'tool' AND source_run_id IS NOT NULL AND source_run_id <> '' AND source_checkpoint_id IS NULL AND committed_checkpoint_id IS NULL AND summary_model_config_id IS NULL AND summary_model_payload_checksum IS NULL AND summary_model_secret_generation_id IS NULL AND summary_model_secret_envelope_digest IS NULL)),
    CONSTRAINT ck_memory_history_entries_model_checksum CHECK (summary_model_payload_checksum IS NULL OR summary_model_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_history_entries_model_secret_group CHECK ((summary_model_secret_generation_id IS NULL AND summary_model_secret_envelope_digest IS NULL) OR (summary_model_secret_generation_id IS NOT NULL AND summary_model_secret_envelope_digest IS NOT NULL)),
    CONSTRAINT ck_memory_history_entries_model_secret_digest CHECK (summary_model_secret_envelope_digest IS NULL OR summary_model_secret_envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_history_entries_digests CHECK (source_digest ~ '^[0-9a-f]{64}$' AND content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_history_entries_preference_version CHECK (preference_version >= 1),
    CONSTRAINT ck_memory_history_entries_contract CHECK ((origin = 'snip' AND snip_prompt_version <> '') OR (origin = 'tool' AND snip_prompt_version = 'remember-tool-v1')),
    CONSTRAINT ck_memory_history_entries_text_size CHECK (tagged_text IS NULL OR char_length(tagged_text) <= 1000),
    CONSTRAINT ck_memory_history_entries_lifecycle CHECK ((status = 'pending' AND tagged_text IS NOT NULL AND dream_job_id IS NULL AND consumed_at IS NULL) OR (status = 'processing' AND tagged_text IS NOT NULL AND dream_job_id IS NOT NULL AND consumed_at IS NULL) OR (status = 'consumed' AND tagged_text IS NULL AND dream_job_id IS NOT NULL AND consumed_at IS NOT NULL))
);

CREATE INDEX ix_memory_history_entries_dream_job ON memory_history_entries (dream_job_id, sequence) WHERE dream_job_id IS NOT NULL;

CREATE INDEX ix_memory_history_entries_pending ON memory_history_entries (project_id, owner_user_id, namespace, sequence) WHERE status = 'pending';

CREATE TABLE memory_documents (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    content TEXT NOT NULL,
    content_digest CHAR(64) NOT NULL,
    version BIGINT DEFAULT 0 NOT NULL,
    dream_cursor BIGINT DEFAULT 0 NOT NULL,
    active_dream_job_id UUID,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    sections JSONB NOT NULL,
    sections_policy_section VARCHAR(32) DEFAULT 'memory_document' NOT NULL,
    sections_policy_version_id UUID NOT NULL,
    CONSTRAINT pk_memory_documents PRIMARY KEY (project_id, owner_user_id, namespace),
    CONSTRAINT fk_memory_documents_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_documents_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_documents_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_documents_sections_policy_version FOREIGN KEY(sections_policy_section, sections_policy_version_id) REFERENCES system_runtime_policy_versions (section, id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_documents_active_dream_job FOREIGN KEY(active_dream_job_id, project_id, owner_user_id, namespace) REFERENCES jobs (id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT uq_memory_documents_active_dream_job UNIQUE (active_dream_job_id),
    CONSTRAINT ck_memory_documents_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_documents_digest CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_documents_content_size CHECK (char_length(content) <= 16000),
    CONSTRAINT ck_memory_documents_sections CHECK (jsonb_typeof(sections) = 'array' AND jsonb_array_length(sections) BETWEEN 2 AND 8 AND NOT jsonb_path_exists(sections, '$[*] ? (@.type() != "string")')),
    CONSTRAINT ck_memory_documents_sections_policy_section CHECK (sections_policy_section = 'memory_document'),
    CONSTRAINT ck_memory_documents_versions CHECK (version >= 0 AND dream_cursor >= 0)
);

CREATE TABLE memory_episodes (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    origin VARCHAR(8) NOT NULL,
    tagged_text TEXT NOT NULL,
    content_digest CHAR(64) NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    consumed_dream_job_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_memory_episodes_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_episodes_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_episodes_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_episodes_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_episodes_origin CHECK (origin IN ('snip', 'tool')),
    CONSTRAINT ck_memory_episodes_text CHECK (tagged_text <> '' AND char_length(tagged_text) <= 1000),
    CONSTRAINT ck_memory_episodes_digest CHECK (content_digest ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_memory_episodes_scope_time ON memory_episodes (project_id, owner_user_id, namespace, occurred_at DESC, id DESC);

CREATE INDEX ix_memory_episodes_trgm ON memory_episodes USING gin (tagged_text gin_trgm_ops);

CREATE TABLE run_memory_context_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    document_version BIGINT NOT NULL,
    content TEXT NOT NULL,
    content_digest CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    sections JSONB NOT NULL,
    CONSTRAINT pk_run_memory_context_snapshots PRIMARY KEY (project_id, owner_user_id, run_id, namespace),
    CONSTRAINT fk_run_memory_context_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_memory_context_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_memory_context_snapshots_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_run_memory_context_snapshots_run UNIQUE (project_id, owner_user_id, run_id),
    CONSTRAINT fk_run_memory_context_snapshots_run FOREIGN KEY(project_id, owner_user_id, run_id) REFERENCES runs (project_id, owner_user_id, run_id) ON DELETE CASCADE,
    CONSTRAINT ck_run_memory_context_snapshots_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_run_memory_context_snapshots_version CHECK (document_version >= 1),
    CONSTRAINT ck_run_memory_context_snapshots_digest CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_memory_context_snapshots_content CHECK (content <> '' AND char_length(content) <= 16000),
    CONSTRAINT ck_run_memory_context_snapshots_sections CHECK (jsonb_typeof(sections) = 'array' AND jsonb_array_length(sections) BETWEEN 2 AND 8 AND NOT jsonb_path_exists(sections, '$[*] ? (@.type() != "string")'))
);

CREATE TABLE run_asset_versions (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    asset_kind VARCHAR(16) NOT NULL,
    dependency_order INTEGER NOT NULL,
    asset_scope VARCHAR(16) NOT NULL,
    asset_id UUID NOT NULL,
    version_id UUID NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    catalog_generation BIGINT NOT NULL,
    snapshot_schema_version SMALLINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    snapshot_json JSONB NOT NULL,
    CONSTRAINT pk_run_asset_versions PRIMARY KEY (project_id, owner_user_id, run_id, asset_kind, dependency_order),
    CONSTRAINT fk_run_asset_versions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_asset_versions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_asset_versions_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_asset_versions_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT ck_run_asset_versions_kind CHECK (asset_kind IN ('agent', 'skill', 'mcp')),
    CONSTRAINT ck_run_asset_versions_scope CHECK (asset_scope IN ('system', 'project')),
    CONSTRAINT ck_run_asset_versions_order CHECK (dependency_order >= 0),
    CONSTRAINT ck_run_asset_versions_generation CHECK (catalog_generation >= 0),
    CONSTRAINT ck_run_asset_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_asset_versions_snapshot_schema CHECK (snapshot_schema_version BETWEEN 2 AND 4),
    CONSTRAINT uq_run_asset_versions_dependency_order UNIQUE (project_id, owner_user_id, run_id, dependency_order),
    CONSTRAINT uq_run_asset_versions_runtime_exact UNIQUE (project_id, owner_user_id, thread_id, run_id, asset_kind, dependency_order, asset_scope, asset_id, version_id, payload_checksum, snapshot_schema_version)
);

CREATE INDEX ix_run_asset_versions_legacy_project_skill ON run_asset_versions (project_id, asset_id, version_id) WHERE asset_kind = 'skill' AND asset_scope = 'project' AND snapshot_schema_version IN (2, 3);

CREATE INDEX ix_run_asset_versions_legacy_skill_version ON run_asset_versions (asset_id, version_id) WHERE asset_kind = 'skill' AND snapshot_schema_version IN (2, 3);

CREATE TABLE run_skill_secret_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    secret_name VARCHAR(255) NOT NULL,
    secret_revision BIGINT NOT NULL,
    secret_generation_id UUID NOT NULL,
    secret_generation_digest CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_run_skill_secret_snapshots PRIMARY KEY (project_id, owner_user_id, run_id, skill_version_id, secret_name),
    CONSTRAINT fk_run_skill_secret_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_secret_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_secret_snapshots_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_secret_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT ck_run_skill_secret_snapshots_secret_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT ck_run_skill_secret_snapshots_revision CHECK (secret_revision >= 1),
    CONSTRAINT ck_run_skill_secret_snapshots_generation_digest CHECK (secret_generation_digest ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_run_skill_secret_snapshots_generation ON run_skill_secret_snapshots (secret_generation_id);

CREATE INDEX ix_run_skill_secret_snapshots_private_run ON run_skill_secret_snapshots (project_id, owner_user_id, thread_id, run_id);

CREATE TABLE project_system_agent_bindings (
    project_id UUID NOT NULL,
    system_agent_id UUID NOT NULL,
    system_asset_scope VARCHAR(16) DEFAULT 'system' NOT NULL,
    enabled BOOLEAN DEFAULT true NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, system_agent_id),
    CONSTRAINT fk_project_system_agent_bindings_system_asset FOREIGN KEY(system_agent_id, system_asset_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT ck_project_system_agent_bindings_system_scope CHECK (system_asset_scope = 'system'),
    CONSTRAINT ck_project_system_agent_bindings_version CHECK (version >= 1),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

CREATE TABLE project_system_skill_bindings (
    project_id UUID NOT NULL,
    system_skill_id UUID NOT NULL,
    system_asset_scope VARCHAR(16) DEFAULT 'system' NOT NULL,
    enabled BOOLEAN DEFAULT true NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, system_skill_id),
    CONSTRAINT fk_project_system_skill_bindings_system_asset FOREIGN KEY(system_skill_id, system_asset_scope) REFERENCES skills (id, scope) ON DELETE RESTRICT,
    CONSTRAINT ck_project_system_skill_bindings_system_scope CHECK (system_asset_scope = 'system'),
    CONSTRAINT ck_project_system_skill_bindings_version CHECK (version >= 1),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

CREATE TABLE mcp_server_versions (
    id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    version_number BIGINT NOT NULL,
    workflow_status VARCHAR(24) DEFAULT 'draft' NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    transport VARCHAR(24) NOT NULL,
    command TEXT,
    args JSONB DEFAULT '[]'::jsonb NOT NULL,
    url TEXT,
    non_secret_env JSONB DEFAULT '{}'::jsonb NOT NULL,
    non_secret_headers JSONB DEFAULT '{}'::jsonb NOT NULL,
    oauth_metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
    routing JSONB DEFAULT '{}'::jsonb NOT NULL,
    tool_overrides JSONB DEFAULT '{}'::jsonb NOT NULL,
    timeout_seconds INTEGER DEFAULT 30 NOT NULL,
    supersedes_version_id UUID,
    payload_checksum CHAR(64) NOT NULL,
    submitted_at TIMESTAMP WITH TIME ZONE,
    reviewed_at TIMESTAMP WITH TIME ZONE,
    reviewed_by_user_id VARCHAR(36),
    review_note TEXT,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_mcp_server_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_mcp_server_versions_workflow_status CHECK (workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')),
    CONSTRAINT ck_mcp_server_versions_transport CHECK (transport IN ('stdio', 'sse', 'http', 'streamable_http')),
    CONSTRAINT ck_mcp_server_versions_timeout CHECK (timeout_seconds > 0),
    CONSTRAINT ck_mcp_server_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT uq_mcp_server_versions_asset_number UNIQUE (mcp_server_id, version_number),
    CONSTRAINT uq_mcp_server_versions_asset_id UNIQUE (mcp_server_id, id),
    FOREIGN KEY(mcp_server_id) REFERENCES mcp_servers (id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(reviewed_by_user_id) REFERENCES users (id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id)
);

CREATE TABLE skill_versions (
    id UUID NOT NULL,
    skill_id UUID NOT NULL,
    version_number BIGINT NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    frontmatter JSONB DEFAULT '{}'::jsonb NOT NULL,
    compatibility VARCHAR(255),
    secret_requirements JSONB DEFAULT '[]'::jsonb NOT NULL,
    scan_decision VARCHAR(24) NOT NULL,
    scan_summary JSONB DEFAULT '{}'::jsonb NOT NULL,
    supersedes_version_id UUID,
    payload_checksum CHAR(64) NOT NULL,
    file_count INTEGER NOT NULL,
    content_size_bytes BIGINT NOT NULL,
    files_sealed BOOLEAN DEFAULT false NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    revocation_reason_code VARCHAR(32),
    PRIMARY KEY (id),
    CONSTRAINT ck_skill_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_skill_versions_scan_decision CHECK (scan_decision IN ('allow', 'warn', 'block')),
    CONSTRAINT ck_skill_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_versions_file_count CHECK (file_count BETWEEN 1 AND 16384),
    CONSTRAINT ck_skill_versions_content_size CHECK (content_size_bytes BETWEEN 0 AND 104857600),
    CONSTRAINT ck_skill_versions_files_sealed CHECK (files_sealed IN (true, false)),
    CONSTRAINT ck_skill_versions_revocation CHECK ((revoked_at IS NULL) = (revoked_by_user_id IS NULL) AND (revoked_at IS NULL) = (revocation_reason_code IS NULL)),
    CONSTRAINT ck_skill_versions_revocation_reason CHECK (revocation_reason_code IS NULL OR revocation_reason_code IN ('security', 'policy', 'integrity')),
    CONSTRAINT uq_skill_versions_asset_number UNIQUE (skill_id, version_number),
    CONSTRAINT uq_skill_versions_asset_id UNIQUE (skill_id, id),
    CONSTRAINT uq_skill_versions_runtime_exact UNIQUE (skill_id, id, payload_checksum, file_count, content_size_bytes),
    FOREIGN KEY(skill_id) REFERENCES skills (id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_version_id) REFERENCES skill_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(revoked_by_user_id) REFERENCES users (id)
);

CREATE TABLE run_skill_version_refs (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    asset_kind VARCHAR(16) NOT NULL,
    dependency_order INTEGER NOT NULL,
    asset_scope VARCHAR(16) NOT NULL,
    snapshot_schema_version SMALLINT NOT NULL,
    skill_project_id UUID,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    file_count INTEGER NOT NULL,
    content_size_bytes BIGINT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_run_skill_version_refs PRIMARY KEY (project_id, owner_user_id, run_id, asset_kind, dependency_order),
    CONSTRAINT uq_run_skill_version_refs_exact_version UNIQUE (project_id, owner_user_id, run_id, skill_id, skill_version_id),
    CONSTRAINT ck_run_skill_version_refs_kind CHECK (asset_kind = 'skill'),
    CONSTRAINT ck_run_skill_version_refs_schema CHECK (snapshot_schema_version = 4),
    CONSTRAINT ck_run_skill_version_refs_scope CHECK (asset_scope IN ('system', 'project')),
    CONSTRAINT ck_run_skill_version_refs_scope_project CHECK ((asset_scope = 'system' AND skill_project_id IS NULL) OR (asset_scope = 'project' AND skill_project_id IS NOT NULL AND skill_project_id = project_id)),
    CONSTRAINT ck_run_skill_version_refs_order CHECK (dependency_order >= 0),
    CONSTRAINT ck_run_skill_version_refs_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_skill_version_refs_file_count CHECK (file_count BETWEEN 1 AND 16384),
    CONSTRAINT ck_run_skill_version_refs_content_size CHECK (content_size_bytes BETWEEN 0 AND 104857600),
    CONSTRAINT fk_run_skill_version_refs_exact_run_asset FOREIGN KEY(project_id, owner_user_id, thread_id, run_id, asset_kind, dependency_order, asset_scope, skill_id, skill_version_id, payload_checksum, snapshot_schema_version) REFERENCES run_asset_versions (project_id, owner_user_id, thread_id, run_id, asset_kind, dependency_order, asset_scope, asset_id, version_id, payload_checksum, snapshot_schema_version) ON DELETE CASCADE,
    CONSTRAINT fk_run_skill_version_refs_skill_scope FOREIGN KEY(skill_id, asset_scope) REFERENCES skills (id, scope) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_version_refs_project_skill FOREIGN KEY(skill_project_id, skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_version_refs_exact_version FOREIGN KEY(skill_id, skill_version_id, payload_checksum, file_count, content_size_bytes) REFERENCES skill_versions (skill_id, id, payload_checksum, file_count, content_size_bytes) ON DELETE RESTRICT
);

CREATE INDEX ix_run_skill_version_refs_version ON run_skill_version_refs (skill_version_id);

CREATE INDEX ix_run_skill_version_refs_skill_scope ON run_skill_version_refs (skill_id, asset_scope);

CREATE INDEX ix_run_skill_version_refs_project_skill ON run_skill_version_refs (skill_project_id, skill_id);

CREATE TABLE run_runtime_policy_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    section VARCHAR(32) NOT NULL,
    policy_version_id UUID NOT NULL,
    schema_version SMALLINT NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, owner_user_id, run_id, section),
    CONSTRAINT ck_run_runtime_policy_snapshots_section CHECK (section = 'agent_runtime'),
    CONSTRAINT ck_run_runtime_policy_snapshots_schema CHECK (schema_version >= 1),
    CONSTRAINT ck_run_runtime_policy_snapshots_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_run_runtime_policy_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_runtime_policy_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_runtime_policy_snapshots_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_runtime_policy_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_runtime_policy_snapshots_exact_policy FOREIGN KEY(section, policy_version_id, schema_version, payload_checksum) REFERENCES system_runtime_policy_versions (section, id, schema_version, payload_checksum) ON DELETE RESTRICT,
    CONSTRAINT uq_run_runtime_policy_snapshots_exact UNIQUE (project_id, owner_user_id, run_id, section, policy_version_id, schema_version, payload_checksum)
);

CREATE INDEX ix_run_runtime_policy_snapshots_private_run ON run_runtime_policy_snapshots (project_id, owner_user_id, thread_id, run_id);

CREATE TABLE run_model_config_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    purpose VARCHAR(64) NOT NULL,
    model_config_id UUID NOT NULL,
    provider_payload JSONB NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    secret_generation_id UUID,
    secret_envelope_digest CHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, owner_user_id, run_id, purpose),
    CONSTRAINT ck_run_model_config_snapshots_purpose CHECK (purpose ~ '^[a-z][a-z0-9._-]{0,63}$'),
    CONSTRAINT ck_run_model_config_snapshots_provider_payload CHECK (jsonb_typeof(provider_payload) = 'object'),
    CONSTRAINT ck_run_model_config_snapshots_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_model_config_snapshots_secret_group CHECK ((secret_generation_id IS NULL AND secret_envelope_digest IS NULL) OR (secret_generation_id IS NOT NULL AND secret_envelope_digest IS NOT NULL)),
    CONSTRAINT ck_run_model_config_snapshots_secret_digest CHECK (secret_envelope_digest IS NULL OR secret_envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_run_model_config_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_model_config_snapshots_model FOREIGN KEY(model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT
);

CREATE INDEX ix_run_model_config_snapshots_secret_generation ON run_model_config_snapshots (secret_generation_id);

CREATE INDEX ix_run_model_config_snapshots_private_run ON run_model_config_snapshots (project_id, owner_user_id, thread_id, run_id);

CREATE INDEX ix_run_model_config_snapshots_model ON run_model_config_snapshots (model_config_id);

CREATE TABLE channel_external_principals (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    group_binding_id UUID NOT NULL,
    external_account_ref CHAR(64) NOT NULL,
    principal_user_id VARCHAR(36) NOT NULL,
    principal_type VARCHAR(16) DEFAULT 'channel_guest' NOT NULL,
    membership_id UUID NOT NULL,
    membership_role VARCHAR(16) DEFAULT 'channel_guest' NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_channel_external_principals_external_ref CHECK (external_account_ref ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_channel_external_principals_type CHECK (principal_type = 'channel_guest'),
    CONSTRAINT ck_channel_external_principals_membership_role CHECK (membership_role = 'channel_guest'),
    CONSTRAINT ck_channel_external_principals_status CHECK (status IN ('active', 'frozen')),
    CONSTRAINT ck_channel_external_principals_seen_order CHECK (first_seen_at <= last_seen_at),
    CONSTRAINT uq_channel_external_principals_group_account UNIQUE (group_binding_id, external_account_ref),
    CONSTRAINT fk_channel_external_principals_group_binding FOREIGN KEY(project_id, group_binding_id) REFERENCES project_channel_group_bindings (project_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_external_principals_guest_user FOREIGN KEY(principal_user_id, principal_type) REFERENCES users (id, principal_type) ON DELETE CASCADE,
    CONSTRAINT fk_channel_external_principals_guest_membership FOREIGN KEY(project_id, principal_user_id, membership_id, membership_role) REFERENCES project_memberships (project_id, user_id, id, role) ON DELETE CASCADE
);

CREATE INDEX ix_channel_external_principals_project_status ON channel_external_principals (project_id, status, id);

CREATE TABLE channel_credentials (
    connection_id VARCHAR(64) NOT NULL,
    encrypted_access_token TEXT,
    encrypted_refresh_token TEXT,
    token_type VARCHAR(32),
    expires_at TIMESTAMP WITH TIME ZONE,
    refresh_expires_at TIMESTAMP WITH TIME ZONE,
    encrypted_extra_json TEXT,
    version INTEGER NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (connection_id),
    FOREIGN KEY(connection_id) REFERENCES channel_connections (id) ON DELETE CASCADE
);

CREATE TABLE channel_conversations (
    id VARCHAR(64) NOT NULL,
    connection_id VARCHAR(64) NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    external_conversation_id VARCHAR(128) NOT NULL,
    external_topic_id VARCHAR(128) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    project_id UUID NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_channel_conversation_connection_external UNIQUE (connection_id, external_conversation_id, external_topic_id),
    CONSTRAINT uq_channel_conversation_delivery_scope UNIQUE (project_id, owner_user_id, connection_id, provider, external_conversation_id, external_topic_id, thread_id),
    CONSTRAINT fk_channel_conversations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_conversations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_conversations_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_conversations_private_connection FOREIGN KEY(project_id, owner_user_id, connection_id) REFERENCES channel_connections (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_conversations_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    FOREIGN KEY(connection_id) REFERENCES channel_connections (id) ON DELETE CASCADE
);

CREATE INDEX ix_channel_conversations_owner_user_id ON channel_conversations (owner_user_id);

CREATE INDEX ix_channel_conversations_provider ON channel_conversations (provider);

CREATE INDEX ix_channel_conversations_thread_id ON channel_conversations (thread_id);

CREATE INDEX ix_channel_conversations_connection_id ON channel_conversations (connection_id);

CREATE INDEX ix_channel_conversations_project_id ON channel_conversations (project_id);

CREATE TABLE channel_inbound_deliveries (
    id VARCHAR(64) NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    connection_id VARCHAR(64) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    external_conversation_id VARCHAR(128) NOT NULL,
    external_topic_id VARCHAR(128) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    provider_delivery_digest VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_channel_inbound_deliveries_scope UNIQUE (project_id, owner_user_id, connection_id, provider, external_conversation_id, external_topic_id, provider_delivery_digest),
    CONSTRAINT fk_channel_inbound_deliveries_connection FOREIGN KEY(project_id, owner_user_id, connection_id) REFERENCES channel_connections (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_inbound_deliveries_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT ck_channel_inbound_deliveries_digest CHECK (provider_delivery_digest <> '')
);

CREATE INDEX ix_channel_inbound_deliveries_run ON channel_inbound_deliveries (project_id, owner_user_id, run_id);

CREATE TABLE execution_approval_requests (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    source_run_id VARCHAR(64) NOT NULL,
    source_job_id UUID NOT NULL,
    source_job_attempt_id UUID NOT NULL,
    source_agent_path JSON NOT NULL,
    tool_call_id VARCHAR(128) NOT NULL,
    kind VARCHAR(32) NOT NULL,
    command_digest CHAR(64) NOT NULL,
    execution_domain_affinity CHAR(64) NOT NULL,
    command_private_json JSON NOT NULL,
    status VARCHAR(24) DEFAULT 'staged' NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    decision VARCHAR(16),
    decision_idempotency_key CHAR(64),
    decision_request_digest CHAR(64),
    decided_by_user_id VARCHAR(36),
    decided_at TIMESTAMP WITH TIME ZONE,
    continuation_run_id VARCHAR(64),
    continuation_job_id UUID,
    execution_job_attempt_id UUID,
    claimed_at TIMESTAMP WITH TIME ZONE,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    terminal_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    spawn_authorized_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT uq_execution_approval_requests_private_scope UNIQUE (id, project_id, owner_user_id, thread_id),
    CONSTRAINT uq_execution_approval_requests_source_tool UNIQUE (project_id, owner_user_id, source_run_id, tool_call_id),
    CONSTRAINT uq_execution_approval_requests_receipt_scope UNIQUE (id, project_id, owner_user_id, thread_id, continuation_job_id, execution_job_attempt_id),
    CONSTRAINT fk_execution_approval_requests_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_execution_approval_requests_source_run FOREIGN KEY(project_id, owner_user_id, thread_id, source_run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_execution_approval_requests_source_job FOREIGN KEY(source_job_id, project_id, owner_user_id, source_run_id) REFERENCES jobs (id, project_id, owner_user_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_source_attempt FOREIGN KEY(source_job_attempt_id, source_job_id) REFERENCES job_attempts (id, job_id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_decider FOREIGN KEY(decided_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_continuation_run FOREIGN KEY(project_id, owner_user_id, thread_id, continuation_run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_continuation_job FOREIGN KEY(continuation_job_id, project_id, owner_user_id, continuation_run_id, execution_domain_affinity) REFERENCES jobs (id, project_id, owner_user_id, run_id, execution_domain_affinity) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_requests_execution_attempt FOREIGN KEY(execution_job_attempt_id, continuation_job_id) REFERENCES job_attempts (id, job_id) ON DELETE RESTRICT,
    CONSTRAINT ck_execution_approval_requests_status CHECK (status IN ('staged', 'pending', 'approved', 'claimed', 'finished', 'launch_failed', 'unknown', 'denied', 'expired', 'cancelled')),
    CONSTRAINT ck_execution_approval_requests_kind CHECK (kind IN ('local_bash')),
    CONSTRAINT ck_execution_approval_requests_digest CHECK (command_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_execution_approval_requests_execution_domain_affinity CHECK (execution_domain_affinity ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_execution_approval_requests_command_json CHECK (json_typeof(command_private_json) = 'object' AND octet_length(command_private_json::text) <= 1048576),
    CONSTRAINT ck_execution_approval_requests_agent_path_json CHECK (json_typeof(source_agent_path) = 'array' AND json_array_length(source_agent_path) BETWEEN 1 AND 16),
    CONSTRAINT ck_execution_approval_requests_tool_call CHECK (tool_call_id <> '' AND tool_call_id = btrim(tool_call_id)),
    CONSTRAINT ck_execution_approval_requests_version CHECK (version >= 1),
    CONSTRAINT ck_execution_approval_requests_decision_shape CHECK ((decision IS NULL AND decision_idempotency_key IS NULL AND decision_request_digest IS NULL AND decided_by_user_id IS NULL AND decided_at IS NULL) OR (decision IN ('allow_once', 'deny') AND decision_idempotency_key ~ '^[0-9a-f]{64}$' AND decision_request_digest ~ '^[0-9a-f]{64}$' AND decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)),
    CONSTRAINT ck_execution_approval_requests_status_decision CHECK ((status IN ('staged', 'pending') AND decision IS NULL) OR (status = 'denied' AND decision = 'deny') OR (status IN ('approved', 'claimed', 'finished', 'launch_failed', 'unknown') AND decision = 'allow_once') OR (status = 'expired' AND (decision IS NULL OR decision = 'allow_once')) OR (status = 'cancelled' AND (decision IS NULL OR decision = 'allow_once'))),
    CONSTRAINT ck_execution_approval_requests_execution_shape CHECK ((continuation_run_id IS NULL) = (continuation_job_id IS NULL) AND (execution_job_attempt_id IS NULL) = (claimed_at IS NULL) AND (execution_job_attempt_id IS NULL OR continuation_job_id IS NOT NULL) AND (status IN ('staged', 'pending', 'denied') AND continuation_job_id IS NULL AND execution_job_attempt_id IS NULL OR status = 'approved' AND execution_job_attempt_id IS NULL OR status IN ('claimed', 'finished', 'launch_failed', 'unknown') AND continuation_job_id IS NOT NULL AND execution_job_attempt_id IS NOT NULL OR status = 'expired' AND execution_job_attempt_id IS NULL OR status = 'cancelled')),
    CONSTRAINT ck_execution_approval_requests_terminal_shape CHECK ((status IN ('staged', 'pending', 'approved', 'claimed') AND terminal_at IS NULL) OR (status IN ('finished', 'launch_failed', 'unknown', 'denied', 'expired', 'cancelled') AND terminal_at IS NOT NULL)),
    CONSTRAINT ck_execution_approval_requests_spawn_authorization CHECK ((status != 'finished' OR spawn_authorized_at IS NOT NULL) AND (spawn_authorized_at IS NULL OR (status IN ('claimed', 'finished', 'launch_failed', 'unknown', 'cancelled') AND execution_job_attempt_id IS NOT NULL AND claimed_at IS NOT NULL AND spawn_authorized_at >= claimed_at AND (terminal_at IS NULL OR spawn_authorized_at <= terminal_at)))),
    CONSTRAINT ck_execution_approval_requests_timestamps CHECK (expires_at > created_at AND updated_at >= created_at AND (decided_at IS NULL OR decided_at >= created_at) AND (claimed_at IS NULL OR claimed_at >= created_at) AND (terminal_at IS NULL OR terminal_at >= created_at))
);

CREATE UNIQUE INDEX uq_execution_approval_requests_decision_idempotency ON execution_approval_requests (project_id, owner_user_id, decision_idempotency_key) WHERE decision_idempotency_key IS NOT NULL;

CREATE UNIQUE INDEX uq_execution_approval_requests_active_thread ON execution_approval_requests (project_id, owner_user_id, thread_id) WHERE status IN ('staged', 'pending', 'approved', 'claimed');

CREATE INDEX ix_execution_approval_requests_status_expiry ON execution_approval_requests (status, expires_at, id);

CREATE INDEX ix_execution_approval_requests_private_cursor ON execution_approval_requests (project_id, owner_user_id, thread_id, created_at DESC, id DESC);

CREATE TABLE thread_event_sequences (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    high_watermark BIGINT DEFAULT 0 NOT NULL,
    PRIMARY KEY (project_id, owner_user_id, thread_id),
    CONSTRAINT ck_thread_event_sequences_high_watermark CHECK (high_watermark >= 0),
    CONSTRAINT fk_thread_event_sequences_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE
);

CREATE TABLE context_evidence_sequences (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    evidence_high_watermark BIGINT DEFAULT 0 NOT NULL,
    projection_high_watermark BIGINT DEFAULT 0 NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, owner_user_id, thread_id),
    CONSTRAINT fk_context_evidence_sequences_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT ck_context_evidence_sequences_watermarks CHECK (evidence_high_watermark >= 0 AND projection_high_watermark >= 0)
);

CREATE TABLE context_evidence (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    evidence_seq BIGINT NOT NULL,
    subject_kind VARCHAR(24) NOT NULL,
    subject_id VARCHAR(64) NOT NULL,
    context_window_generation UUID NOT NULL,
    event_type VARCHAR(40) NOT NULL,
    payload_schema_version SMALLINT DEFAULT 1 NOT NULL,
    origin_run_id VARCHAR(64),
    provider_call_id CHAR(64),
    checkpoint_id VARCHAR(128),
    idempotency_key CHAR(64) NOT NULL,
    payload_digest CHAR(64) NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_context_evidence PRIMARY KEY (project_id, owner_user_id, thread_id, evidence_seq),
    CONSTRAINT fk_context_evidence_sequence FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES context_evidence_sequences (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT uq_context_evidence_idempotency UNIQUE (project_id, owner_user_id, thread_id, idempotency_key),
    CONSTRAINT ck_context_evidence_event_type CHECK (event_type IN ('context.window.opened.v1', 'request.prepared.v1', 'request.dispatched.v1', 'provider.observed.v1', 'provider.usage_unreported.v1', 'provider.failed.v1', 'provider.ambiguous.v1', 'checkpoint.linked.v1', 'compaction.committed.v1', 'context.window.rebased.v1')),
    CONSTRAINT ck_context_evidence_subject CHECK ((subject_kind = 'lead_thread' AND subject_id = thread_id) OR (subject_kind = 'subagent_task' AND subject_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')),
    CONSTRAINT ck_context_evidence_payload_schema CHECK (payload_schema_version = 1),
    CONSTRAINT ck_context_evidence_digests CHECK (idempotency_key ~ '^[0-9a-f]{64}$' AND payload_digest ~ '^[0-9a-f]{64}$' AND (provider_call_id IS NULL OR provider_call_id ~ '^[0-9a-f]{64}$')),
    CONSTRAINT ck_context_evidence_checkpoint CHECK (checkpoint_id IS NULL OR checkpoint_id <> ''),
    CONSTRAINT ck_context_evidence_payload_object CHECK (jsonb_typeof(payload_json) = 'object')
);

CREATE INDEX ix_context_evidence_subject_seq ON context_evidence (project_id, owner_user_id, thread_id, subject_kind, subject_id, evidence_seq);

CREATE INDEX ix_context_evidence_origin_run ON context_evidence (project_id, owner_user_id, thread_id, origin_run_id, evidence_seq) WHERE origin_run_id IS NOT NULL;

CREATE INDEX ix_context_evidence_provider_call ON context_evidence (project_id, owner_user_id, thread_id, provider_call_id, evidence_seq) WHERE provider_call_id IS NOT NULL;

CREATE TABLE context_projection_heads (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    subject_kind VARCHAR(24) NOT NULL,
    subject_id VARCHAR(64) NOT NULL,
    projection_seq BIGINT NOT NULL,
    evidence_seq BIGINT NOT NULL,
    projector_revision VARCHAR(128) NOT NULL,
    projection_schema_version SMALLINT DEFAULT 2 NOT NULL,
    context_window_generation UUID NOT NULL,
    checkpoint_id VARCHAR(128),
    active_run_id VARCHAR(64),
    phase VARCHAR(16) NOT NULL,
    basis VARCHAR(24) NOT NULL,
    coverage VARCHAR(16) NOT NULL,
    freshness VARCHAR(16) NOT NULL,
    payload_digest CHAR(64) NOT NULL,
    projection_json JSONB NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_context_projection_heads PRIMARY KEY (project_id, owner_user_id, thread_id, subject_kind, subject_id),
    CONSTRAINT fk_context_projection_heads_sequence FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES context_evidence_sequences (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT ck_context_projection_heads_subject CHECK ((subject_kind = 'lead_thread' AND subject_id = thread_id) OR (subject_kind = 'subagent_task' AND subject_id ~ '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')),
    CONSTRAINT ck_context_projection_heads_versions CHECK (projection_seq >= 1 AND evidence_seq >= 0 AND projection_schema_version = 2),
    CONSTRAINT ck_context_projection_heads_projector_revision CHECK (projector_revision ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*-v[1-9][0-9]*$'),
    CONSTRAINT ck_context_projection_heads_checkpoint CHECK (checkpoint_id IS NULL OR checkpoint_id <> ''),
    CONSTRAINT ck_context_projection_heads_phase CHECK (phase IN ('idle', 'active', 'settled')),
    CONSTRAINT ck_context_projection_heads_basis CHECK (basis IN ('provider_confirmed', 'hybrid', 'estimated', 'empty')),
    CONSTRAINT ck_context_projection_heads_coverage CHECK (coverage IN ('complete', 'partial')),
    CONSTRAINT ck_context_projection_heads_freshness CHECK (freshness IN ('current', 'stale')),
    CONSTRAINT ck_context_projection_heads_digest CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_context_projection_heads_payload_object CHECK (jsonb_typeof(projection_json) = 'object'),
    CONSTRAINT uq_context_projection_heads_projection_seq UNIQUE (project_id, owner_user_id, thread_id, projection_seq)
);

CREATE INDEX ix_context_projection_heads_replay ON context_projection_heads (project_id, owner_user_id, thread_id, projection_seq);

CREATE TABLE memory_dream_runs (
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    history_from BIGINT,
    history_to BIGINT,
    history_count INTEGER NOT NULL,
    history_digest CHAR(64) NOT NULL,
    base_document_version BIGINT NOT NULL,
    base_content_digest CHAR(64) NOT NULL,
    preference_version BIGINT NOT NULL,
    policy_revision BIGINT NOT NULL,
    model_config_id UUID NOT NULL,
    model_provider_payload JSONB NOT NULL,
    model_payload_checksum CHAR(64) NOT NULL,
    model_secret_generation_id UUID,
    model_secret_envelope_digest CHAR(64),
    prompt_version VARCHAR(64) NOT NULL,
    result_version BIGINT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (job_id),
    CONSTRAINT fk_memory_dream_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_runs_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_memory_dream_runs_job_scope UNIQUE (job_id, project_id, owner_user_id, namespace),
    CONSTRAINT fk_memory_dream_runs_job FOREIGN KEY(job_id, project_id, owner_user_id, namespace) REFERENCES jobs (id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_runs_document FOREIGN KEY(project_id, owner_user_id, namespace) REFERENCES memory_documents (project_id, owner_user_id, namespace) ON DELETE CASCADE,
    CONSTRAINT fk_memory_dream_runs_model FOREIGN KEY(model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_dream_runs_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_dream_runs_model_provider_payload CHECK (jsonb_typeof(model_provider_payload) = 'object'),
    CONSTRAINT ck_memory_dream_runs_model_payload_checksum CHECK (model_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_dream_runs_model_secret_group CHECK ((model_secret_generation_id IS NULL AND model_secret_envelope_digest IS NULL) OR (model_secret_generation_id IS NOT NULL AND model_secret_envelope_digest IS NOT NULL)),
    CONSTRAINT ck_memory_dream_runs_model_secret_digest CHECK (model_secret_envelope_digest IS NULL OR model_secret_envelope_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_dream_runs_trigger CHECK (trigger IN ('auto_dream', 'manual_dream', 'budget_rewrite')),
    CONSTRAINT ck_memory_dream_runs_history CHECK ((trigger = 'budget_rewrite' AND history_count = 0 AND history_from IS NULL AND history_to IS NULL) OR (trigger IN ('auto_dream', 'manual_dream') AND history_count BETWEEN 1 AND 20 AND history_from >= 1 AND history_to >= history_from)),
    CONSTRAINT ck_memory_dream_runs_digests CHECK (history_digest ~ '^[0-9a-f]{64}$' AND base_content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_dream_runs_versions CHECK (base_document_version >= 0 AND preference_version >= 1 AND policy_revision >= 1),
    CONSTRAINT ck_memory_dream_runs_contract CHECK (prompt_version <> ''),
    CONSTRAINT ck_memory_dream_runs_result CHECK ((result_version IS NULL AND completed_at IS NULL) OR (result_version >= 1 AND completed_at IS NOT NULL))
);

CREATE TABLE files (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    kind VARCHAR(16) NOT NULL,
    logical_path VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255) DEFAULT 'application/octet-stream' NOT NULL,
    size BIGINT DEFAULT 0 NOT NULL,
    sha256 CHAR(64) NOT NULL,
    status VARCHAR(16) DEFAULT 'staging' NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    created_by_run_id VARCHAR(64),
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source_file_id UUID,
    PRIMARY KEY (id),
    CONSTRAINT fk_files_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_files_created_by_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, created_by_run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_private_source FOREIGN KEY(project_id, owner_user_id, thread_id, source_file_id) REFERENCES files (project_id, owner_user_id, thread_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_files_private_scope UNIQUE (project_id, owner_user_id, thread_id, id),
    CONSTRAINT ck_files_kind CHECK (kind IN ('upload', 'workspace', 'output')),
    CONSTRAINT ck_files_status CHECK (status IN ('staging', 'ready', 'deleted')),
    CONSTRAINT ck_files_size CHECK (size >= 0),
    CONSTRAINT ck_files_version CHECK (version >= 1),
    CONSTRAINT ck_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_files_source_not_self CHECK (source_file_id IS NULL OR source_file_id <> id),
    CONSTRAINT ck_files_source_kind CHECK (source_file_id IS NULL OR kind = 'workspace'),
    CONSTRAINT ck_files_logical_path CHECK (logical_path <> '' AND left(logical_path, 1) <> '/' AND logical_path !~ '(^|/)\.\.(/|$)' AND logical_path !~ '^[A-Za-z]:')
);

CREATE UNIQUE INDEX uq_files_active_logical_path ON files (project_id, owner_user_id, thread_id, logical_path) WHERE status != 'deleted';

CREATE TABLE scheduled_tasks (
    id VARCHAR(64) NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64),
    context_mode VARCHAR(32) NOT NULL,
    agent_asset_id UUID NOT NULL,
    agent_scope VARCHAR(16) NOT NULL,
    title VARCHAR(255) NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type VARCHAR(16) NOT NULL,
    schedule_spec JSON NOT NULL,
    timezone VARCHAR(64) NOT NULL,
    status VARCHAR(16) NOT NULL,
    overlap_policy VARCHAR(16) NOT NULL,
    next_run_at TIMESTAMP WITH TIME ZONE,
    last_run_at TIMESTAMP WITH TIME ZONE,
    last_outcome VARCHAR(24),
    last_error_code VARCHAR(64),
    run_count BIGINT NOT NULL,
    version BIGINT NOT NULL,
    frozen_at TIMESTAMP WITH TIME ZONE,
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_scheduled_tasks_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT fk_scheduled_tasks_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_agent_asset FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT ck_scheduled_tasks_context_mode CHECK (context_mode IN ('fresh_thread_per_run', 'reuse_thread')),
    CONSTRAINT ck_scheduled_tasks_schedule_type CHECK (schedule_type IN ('once', 'cron')),
    CONSTRAINT ck_scheduled_tasks_status CHECK (status IN ('enabled', 'paused', 'completed', 'failed', 'cancelled')),
    CONSTRAINT ck_scheduled_tasks_overlap_policy CHECK (overlap_policy = 'skip'),
    CONSTRAINT ck_scheduled_tasks_thread_mode CHECK ((context_mode = 'reuse_thread' AND thread_id IS NOT NULL) OR (context_mode = 'fresh_thread_per_run' AND thread_id IS NULL)),
    CONSTRAINT ck_scheduled_tasks_agent_scope CHECK (agent_scope IN ('system', 'project')),
    CONSTRAINT ck_scheduled_tasks_version CHECK (version >= 1),
    CONSTRAINT ck_scheduled_tasks_run_count CHECK (run_count >= 0),
    CONSTRAINT ck_scheduled_tasks_last_outcome CHECK (last_outcome IS NULL OR last_outcome IN ('success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected'))
);

CREATE INDEX ix_scheduled_tasks_project_id ON scheduled_tasks (project_id);

CREATE INDEX ix_scheduled_tasks_owner_user_id ON scheduled_tasks (owner_user_id);

CREATE TABLE agent_design_sessions (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id UUID NOT NULL,
    slug VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(32) DEFAULT 'interviewing' NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    messages_json JSONB DEFAULT '[]'::jsonb NOT NULL,
    progress_json JSONB DEFAULT '[]'::jsonb NOT NULL,
    active_clarification_json JSONB,
    blueprint_json JSONB,
    blueprint_checksum CHAR(64),
    error_code VARCHAR(64),
    error_message VARCHAR(255),
    created_agent_id UUID,
    create_idempotency_key_hash CHAR(64) NOT NULL,
    create_request_checksum CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    created_agent_deleted BOOLEAN DEFAULT false NOT NULL,
    generation_model_ref VARCHAR(36),
    generation_mode VARCHAR(16),
    CONSTRAINT pk_agent_design_sessions PRIMARY KEY (id),
    CONSTRAINT ck_agent_design_sessions_status CHECK (status IN ('interviewing', 'generating', 'awaiting_clarification', 'proposal_ready', 'committing', 'completed', 'failed', 'cancelled')),
    CONSTRAINT ck_agent_design_sessions_revision CHECK (revision >= 1),
    CONSTRAINT ck_agent_design_sessions_blueprint CHECK ((blueprint_json IS NULL AND blueprint_checksum IS NULL) OR (blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL)),
    CONSTRAINT ck_agent_design_sessions_completion CHECK ((status = 'completed' AND ((created_agent_deleted IS FALSE AND created_agent_id IS NOT NULL) OR (created_agent_deleted IS TRUE AND created_agent_id IS NULL))) OR (status <> 'completed' AND created_agent_deleted IS FALSE AND created_agent_id IS NULL)),
    CONSTRAINT ck_agent_design_sessions_ready_blueprint CHECK ((status IN ('proposal_ready', 'committing', 'completed') AND blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL) OR status NOT IN ('proposal_ready', 'committing', 'completed')),
    CONSTRAINT ck_agent_design_sessions_clarification CHECK ((status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)),
    CONSTRAINT ck_agent_design_sessions_error CHECK ((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)),
    CONSTRAINT ck_agent_design_sessions_generation_preference CHECK ((generation_model_ref IS NULL AND generation_mode IS NULL) OR (generation_model_ref IS NOT NULL AND generation_mode IN ('flash', 'thinking', 'pro', 'ultra'))),
    CONSTRAINT uq_agent_design_sessions_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT uq_agent_design_sessions_thread_scope UNIQUE (project_id, owner_user_id, thread_id),
    CONSTRAINT uq_agent_design_sessions_create_idempotency UNIQUE (project_id, owner_user_id, create_idempotency_key_hash),
    CONSTRAINT fk_agent_design_sessions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_created_agent_project FOREIGN KEY(project_id, created_agent_id) REFERENCES agents (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_agent_design_sessions_resume ON agent_design_sessions (project_id, owner_user_id, created_at DESC, id DESC) WHERE status NOT IN ('completed', 'cancelled');

CREATE TABLE agent_skill_refs (
    agent_id UUID NOT NULL,
    sort_order BIGINT DEFAULT 0 NOT NULL,
    skill_asset_scope VARCHAR(16) NOT NULL,
    skill_asset_id UUID NOT NULL,
    PRIMARY KEY (agent_id, skill_asset_scope, skill_asset_id),
    FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE RESTRICT,
    FOREIGN KEY(skill_asset_id, skill_asset_scope) REFERENCES skills (id, scope) ON DELETE RESTRICT,
    CONSTRAINT ck_agent_skill_refs_scope CHECK (skill_asset_scope IN ('system', 'project')),
    CONSTRAINT ck_agent_skill_refs_sort_order CHECK (sort_order >= 0)
);

CREATE TABLE agent_mcp_refs (
    agent_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    sort_order BIGINT DEFAULT 0 NOT NULL,
    PRIMARY KEY (agent_id, mcp_server_version_id),
    FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE RESTRICT,
    FOREIGN KEY(mcp_server_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_agent_mcp_refs_sort_order CHECK (sort_order >= 0)
);

CREATE TABLE project_system_mcp_bindings (
    project_id UUID NOT NULL,
    system_mcp_server_id UUID NOT NULL,
    system_asset_scope VARCHAR(16) DEFAULT 'system' NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    enabled BOOLEAN DEFAULT true NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, system_mcp_server_id),
    CONSTRAINT fk_project_system_mcp_bindings_system_asset FOREIGN KEY(system_mcp_server_id, system_asset_scope) REFERENCES mcp_servers (id, scope) ON DELETE RESTRICT,
    CONSTRAINT fk_project_system_mcp_bindings_version FOREIGN KEY(system_mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_project_system_mcp_bindings_system_scope CHECK (system_asset_scope = 'system'),
    CONSTRAINT ck_project_system_mcp_bindings_version CHECK (version >= 1),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

CREATE TABLE mcp_version_secret_slots (
    id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    name VARCHAR(63) NOT NULL,
    purpose TEXT DEFAULT '' NOT NULL,
    payload_schema JSONB NOT NULL,
    required BOOLEAN DEFAULT true NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_mcp_secret_slots_version_name UNIQUE (mcp_server_version_id, name),
    CONSTRAINT uq_mcp_secret_slots_version_id UNIQUE (mcp_server_version_id, id),
    FOREIGN KEY(mcp_server_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT
);

CREATE TABLE mcp_tool_discovery_attempts (
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    requested_by_user_id VARCHAR(36) NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    secret_digest CHAR(64) NOT NULL,
    result_status VARCHAR(16),
    public_error_code VARCHAR(64),
    requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    PRIMARY KEY (job_id),
    CONSTRAINT fk_mcp_tool_discovery_attempt_job FOREIGN KEY(job_id, project_id, requested_by_user_id) REFERENCES jobs (id, project_id, owner_user_id) ON DELETE CASCADE,
    CONSTRAINT fk_mcp_tool_discovery_attempt_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_mcp_tool_discovery_attempt_trigger CHECK (trigger IN ('auto', 'manual')),
    CONSTRAINT ck_mcp_tool_discovery_attempt_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_mcp_tool_discovery_attempt_secret_digest CHECK (secret_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_mcp_tool_discovery_attempt_result_status CHECK (result_status IS NULL OR result_status IN ('succeeded', 'failed', 'cancelled')),
    CONSTRAINT ck_mcp_tool_discovery_attempt_result CHECK ((result_status IS NULL AND public_error_code IS NULL) OR (result_status = 'succeeded' AND public_error_code IS NULL) OR (result_status = 'cancelled' AND public_error_code IS NULL) OR (result_status = 'failed' AND public_error_code IN ('mcp_discovery_unavailable', 'mcp_catalog_invalid'))),
    CONSTRAINT ck_mcp_tool_discovery_attempt_revision CHECK (revision >= 1),
    CONSTRAINT fk_mcp_tool_discovery_attempt_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_mcp_tool_discovery_attempt_requester FOREIGN KEY(requested_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_mcp_tool_discovery_attempts_version ON mcp_tool_discovery_attempts (project_id, mcp_server_id, mcp_server_version_id, requested_at DESC, job_id);

CREATE INDEX ix_mcp_tool_discovery_attempts_closure ON mcp_tool_discovery_attempts (project_id, mcp_server_id, mcp_server_version_id, payload_checksum, secret_digest, requested_at DESC);

CREATE TABLE project_mcp_tool_inventories (
    project_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    attempt_payload_checksum CHAR(64) NOT NULL,
    attempt_secret_digest CHAR(64) NOT NULL,
    attempt_status VARCHAR(16) NOT NULL,
    public_error_code VARCHAR(64),
    tools JSONB DEFAULT '[]'::jsonb NOT NULL,
    tools_payload_checksum CHAR(64),
    tools_secret_digest CHAR(64),
    last_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    last_success_at TIMESTAMP WITH TIME ZONE,
    revision BIGINT DEFAULT 1 NOT NULL,
    PRIMARY KEY (project_id, mcp_server_version_id),
    CONSTRAINT fk_project_mcp_tool_inventory_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_project_mcp_tool_inventory_attempt_status CHECK (attempt_status IN ('ready', 'failed')),
    CONSTRAINT ck_project_mcp_tool_inventory_attempt_checksum CHECK (attempt_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_attempt_secret_digest CHECK (attempt_secret_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_tools_checksum CHECK (tools_payload_checksum IS NULL OR tools_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_tools_secret_digest CHECK (tools_secret_digest IS NULL OR tools_secret_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_error CHECK ((attempt_status = 'ready' AND public_error_code IS NULL) OR (attempt_status = 'failed' AND public_error_code IN ('mcp_discovery_unavailable', 'mcp_catalog_invalid'))),
    CONSTRAINT ck_project_mcp_tool_inventory_success_shape CHECK ((tools_payload_checksum IS NULL AND tools_secret_digest IS NULL AND last_success_at IS NULL) OR (tools_payload_checksum IS NOT NULL AND tools_secret_digest IS NOT NULL AND last_success_at IS NOT NULL)),
    CONSTRAINT ck_project_mcp_tool_inventory_tools_shape CHECK (jsonb_typeof(tools) = 'array' AND jsonb_array_length(tools) <= 128),
    CONSTRAINT ck_project_mcp_tool_inventory_time_order CHECK (last_success_at IS NULL OR last_success_at <= last_attempt_at),
    CONSTRAINT ck_project_mcp_tool_inventory_revision CHECK (revision >= 1),
    CONSTRAINT fk_project_mcp_tool_inventory_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE
);

CREATE INDEX ix_project_mcp_tool_inventories_asset ON project_mcp_tool_inventories (project_id, mcp_server_id, mcp_server_version_id);

CREATE TABLE skill_design_sessions (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id UUID NOT NULL,
    slug VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    status VARCHAR(32) DEFAULT 'interviewing' NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    messages_json JSONB DEFAULT '[]'::jsonb NOT NULL,
    progress_json JSONB DEFAULT '[]'::jsonb NOT NULL,
    active_clarification_json JSONB,
    draft_checksum CHAR(64),
    validation_json JSONB,
    validated_draft_checksum CHAR(64),
    skill_creator_skill_id UUID NOT NULL,
    skill_creator_version_id UUID NOT NULL,
    skill_creator_payload_checksum CHAR(64) NOT NULL,
    error_code VARCHAR(64),
    error_message VARCHAR(255),
    created_skill_id UUID,
    created_skill_version_id UUID,
    created_skill_deleted BOOLEAN DEFAULT false NOT NULL,
    create_idempotency_key_hash CHAR(64) NOT NULL,
    create_request_checksum CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    authoring_dependencies_json JSONB,
    session_kind VARCHAR(16) DEFAULT 'create' NOT NULL,
    target_skill_id UUID,
    base_version_id UUID,
    base_version_number BIGINT,
    base_payload_checksum CHAR(64),
    target_skill_deleted BOOLEAN DEFAULT false NOT NULL,
    execution_model_ref VARCHAR(36),
    execution_mode VARCHAR(16),
    execution_thinking_enabled BOOLEAN,
    execution_reasoning_effort VARCHAR(16),
    CONSTRAINT pk_skill_design_sessions PRIMARY KEY (id),
    CONSTRAINT ck_skill_design_sessions_status CHECK (status IN ('interviewing', 'generating', 'awaiting_clarification', 'draft_ready', 'validated', 'committing', 'completed', 'failed', 'cancelled')),
    CONSTRAINT ck_skill_design_sessions_revision CHECK (revision >= 1),
    CONSTRAINT ck_skill_design_sessions_creator_checksum CHECK (skill_creator_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_design_sessions_draft_checksum CHECK (draft_checksum IS NULL OR draft_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_design_sessions_validated_checksum CHECK (validated_draft_checksum IS NULL OR validated_draft_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_design_sessions_validation_pair CHECK ((validation_json IS NULL AND validated_draft_checksum IS NULL) OR (validation_json IS NOT NULL AND validated_draft_checksum IS NOT NULL)),
    CONSTRAINT ck_skill_design_sessions_validated_state CHECK ((status IN ('validated', 'committing', 'completed') AND draft_checksum IS NOT NULL AND validation_json IS NOT NULL AND validated_draft_checksum = draft_checksum) OR status NOT IN ('validated', 'committing', 'completed')),
    CONSTRAINT ck_skill_design_sessions_draft_state CHECK ((status IN ('draft_ready', 'validated', 'committing', 'completed') AND draft_checksum IS NOT NULL) OR status NOT IN ('draft_ready', 'validated', 'committing', 'completed')),
    CONSTRAINT ck_skill_design_sessions_clarification CHECK ((status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)),
    CONSTRAINT ck_skill_design_sessions_authoring_dependencies CHECK (authoring_dependencies_json IS NULL OR (jsonb_typeof(authoring_dependencies_json) = 'object' AND authoring_dependencies_json ->> 'version' = '1' AND (authoring_dependencies_json ->> 'draft_checksum') ~ '^[0-9a-f]{64}$' AND CASE WHEN jsonb_typeof(authoring_dependencies_json -> 'requirements') = 'array' THEN jsonb_array_length(authoring_dependencies_json -> 'requirements') <= 64 ELSE FALSE END)),
    CONSTRAINT ck_skill_design_sessions_error CHECK ((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)),
    CONSTRAINT ck_skill_design_sessions_completion CHECK ((status = 'completed' AND ((created_skill_deleted IS FALSE AND created_skill_id IS NOT NULL AND created_skill_version_id IS NOT NULL) OR (created_skill_deleted IS TRUE AND created_skill_id IS NULL AND created_skill_version_id IS NULL))) OR (status <> 'completed' AND created_skill_deleted IS FALSE AND created_skill_id IS NULL AND created_skill_version_id IS NULL)),
    CONSTRAINT ck_skill_design_sessions_kind CHECK (session_kind IN ('create', 'revise')),
    CONSTRAINT ck_skill_design_sessions_base_checksum CHECK (base_payload_checksum IS NULL OR base_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_design_sessions_base_version_number CHECK (base_version_number IS NULL OR base_version_number >= 1),
    CONSTRAINT ck_skill_design_sessions_execution_preference CHECK ((execution_model_ref IS NULL AND execution_mode IS NULL AND execution_thinking_enabled IS NULL AND execution_reasoning_effort IS NULL) OR (execution_model_ref IS NOT NULL AND execution_mode IN ('flash', 'thinking', 'pro', 'ultra') AND execution_thinking_enabled IS NOT NULL AND (execution_reasoning_effort IS NULL OR execution_reasoning_effort IN ('none', 'low', 'medium', 'high')))),
    CONSTRAINT ck_skill_design_sessions_revision_target CHECK ((session_kind = 'create' AND target_skill_id IS NULL AND base_version_id IS NULL AND base_version_number IS NULL AND base_payload_checksum IS NULL AND target_skill_deleted IS FALSE) OR (session_kind = 'revise' AND ((target_skill_deleted IS FALSE AND target_skill_id IS NOT NULL AND base_version_id IS NOT NULL AND base_version_number IS NOT NULL AND base_payload_checksum IS NOT NULL) OR (target_skill_deleted IS TRUE AND target_skill_id IS NULL AND base_version_id IS NULL AND base_version_number IS NULL AND base_payload_checksum IS NULL)))),
    CONSTRAINT uq_skill_design_sessions_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT uq_skill_design_sessions_thread_scope UNIQUE (project_id, owner_user_id, thread_id),
    CONSTRAINT uq_skill_design_sessions_create_idempotency UNIQUE (project_id, owner_user_id, create_idempotency_key_hash),
    CONSTRAINT fk_skill_design_sessions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_skill_creator_version FOREIGN KEY(skill_creator_skill_id, skill_creator_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_created_skill_project FOREIGN KEY(project_id, created_skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_created_skill_version FOREIGN KEY(created_skill_id, created_skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_target_skill_project FOREIGN KEY(project_id, target_skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_base_version FOREIGN KEY(target_skill_id, base_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_skill_design_sessions_resume ON skill_design_sessions (project_id, owner_user_id, status, updated_at DESC, id DESC);

CREATE UNIQUE INDEX uq_skill_design_sessions_live_revise_target ON skill_design_sessions (project_id, owner_user_id, target_skill_id) WHERE session_kind = 'revise' AND target_skill_id IS NOT NULL AND status NOT IN ('completed', 'cancelled');

CREATE TABLE skill_version_files (
    skill_version_id UUID NOT NULL,
    path VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    content BYTEA NOT NULL,
    PRIMARY KEY (skill_version_id, path),
    FOREIGN KEY(skill_version_id) REFERENCES skill_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_skill_version_files_safe_path CHECK (path <> '' AND path !~ '(^/|(^|/)\.\.(/|$))'),
    CONSTRAINT ck_skill_version_files_size CHECK (size_bytes >= 0 AND size_bytes <= 67108864),
    CONSTRAINT ck_skill_version_files_content_size CHECK (size_bytes = octet_length(content)),
    CONSTRAINT ck_skill_version_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_skill_version_files_version_path_c ON skill_version_files (skill_version_id, path COLLATE "C");

CREATE TABLE project_skill_secret_states (
    project_id UUID NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    secret_name VARCHAR(255) NOT NULL,
    optional BOOLEAN DEFAULT false NOT NULL,
    current_generation_id UUID,
    revision BIGINT DEFAULT 0 NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_project_skill_secret_states PRIMARY KEY (project_id, skill_id, skill_version_id, secret_name),
    CONSTRAINT fk_project_skill_secret_states_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_secret_states_skill_version FOREIGN KEY(skill_id, skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_secret_states_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT ck_project_skill_secret_states_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT ck_project_skill_secret_states_revision CHECK (revision >= 0)
);

CREATE TABLE project_skill_secret_generations (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    secret_name VARCHAR(255) NOT NULL,
    revision BIGINT NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_project_skill_secret_generations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_secret_generations_skill_version FOREIGN KEY(skill_id, skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_secret_generations_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_skill_secret_generations_owner_id UNIQUE (project_id, skill_id, skill_version_id, secret_name, id),
    CONSTRAINT uq_project_skill_secret_generations_revision UNIQUE (project_id, skill_id, skill_version_id, secret_name, revision),
    CONSTRAINT ck_project_skill_secret_generations_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT ck_project_skill_secret_generations_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_skill_secret_generations_envelope CHECK (octet_length(nonce) = 12 AND octet_length(ciphertext) >= 16),
    CONSTRAINT ck_project_skill_secret_generations_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE execution_approval_result_receipts (
    id UUID NOT NULL,
    approval_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    execution_job_id UUID NOT NULL,
    execution_job_attempt_id UUID NOT NULL,
    outcome VARCHAR(24) NOT NULL,
    exit_code INTEGER,
    result_digest CHAR(64) NOT NULL,
    result_private_json JSON NOT NULL,
    public_error_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT uq_execution_approval_result_receipts_approval UNIQUE (approval_id),
    CONSTRAINT uq_execution_approval_result_receipts_attempt UNIQUE (execution_job_attempt_id),
    CONSTRAINT fk_execution_approval_result_receipts_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_result_receipts_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_result_receipts_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_result_receipts_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_execution_approval_result_receipts_approval_execution FOREIGN KEY(approval_id, project_id, owner_user_id, thread_id, execution_job_id, execution_job_attempt_id) REFERENCES execution_approval_requests (id, project_id, owner_user_id, thread_id, continuation_job_id, execution_job_attempt_id) ON DELETE CASCADE,
    CONSTRAINT fk_execution_approval_result_receipts_execution_job FOREIGN KEY(execution_job_id, project_id, owner_user_id) REFERENCES jobs (id, project_id, owner_user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_execution_approval_result_receipts_execution_attempt FOREIGN KEY(execution_job_attempt_id, execution_job_id) REFERENCES job_attempts (id, job_id) ON DELETE RESTRICT,
    CONSTRAINT ck_execution_approval_result_receipts_outcome CHECK (outcome IN ('finished', 'launch_failed')),
    CONSTRAINT ck_execution_approval_result_receipts_digest CHECK (result_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_execution_approval_result_receipts_result_json CHECK (json_typeof(result_private_json) = 'object' AND octet_length(result_private_json::text) <= 2097152),
    CONSTRAINT ck_execution_approval_result_receipts_result_shape CHECK ((outcome = 'finished' AND exit_code IS NOT NULL) OR (outcome = 'launch_failed' AND exit_code IS NULL AND public_error_code IS NOT NULL))
);

CREATE INDEX ix_execution_approval_result_receipts_private_created ON execution_approval_result_receipts (project_id, owner_user_id, thread_id, created_at DESC, id DESC);

CREATE TABLE memory_dream_prepare_runs (
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    operation_id UUID NOT NULL,
    request_id VARCHAR(512) NOT NULL,
    phase VARCHAR(24) DEFAULT 'queued' NOT NULL,
    compacted_passes INTEGER DEFAULT 0 NOT NULL,
    last_checkpoint_id VARCHAR(128),
    dream_job_id UUID,
    history_count INTEGER,
    admission_kind VARCHAR(16),
    result_disposition VARCHAR(24) DEFAULT 'queued' NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (job_id),
    CONSTRAINT fk_memory_dream_prepare_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_memory_dream_prepare_runs_job_scope UNIQUE (job_id, project_id, owner_user_id, namespace),
    CONSTRAINT uq_memory_dream_prepare_runs_operation UNIQUE (project_id, owner_user_id, operation_id),
    CONSTRAINT fk_memory_dream_prepare_runs_job FOREIGN KEY(job_id, project_id, owner_user_id, namespace) REFERENCES jobs (id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_dream FOREIGN KEY(dream_job_id, project_id, owner_user_id, namespace) REFERENCES memory_dream_runs (job_id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_dream_prepare_runs_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_dream_prepare_runs_request CHECK (request_id <> ''),
    CONSTRAINT ck_memory_dream_prepare_runs_phase CHECK (phase IN ('queued', 'draining', 'verifying', 'dream_admitted', 'succeeded', 'cancelled', 'failed')),
    CONSTRAINT ck_memory_dream_prepare_runs_disposition CHECK (result_disposition IN ('queued', 'already_running', 'nothing_pending', 'cancelled', 'failed')),
    CONSTRAINT ck_memory_dream_prepare_runs_passes CHECK (compacted_passes >= 0),
    CONSTRAINT ck_memory_dream_prepare_runs_terminal CHECK ((phase IN ('succeeded', 'cancelled', 'failed')) = (completed_at IS NOT NULL)),
    CONSTRAINT ck_memory_dream_prepare_runs_child CHECK ((dream_job_id IS NULL AND admission_kind IS NULL AND (history_count IS NULL OR (result_disposition = 'nothing_pending' AND history_count = 0))) OR (dream_job_id IS NOT NULL AND history_count BETWEEN 0 AND 20 AND admission_kind IN ('history', 'budget_rewrite'))),
    CONSTRAINT ck_memory_dream_prepare_runs_admission_kind CHECK ((admission_kind = 'budget_rewrite') = (dream_job_id IS NOT NULL AND history_count = 0))
);

CREATE INDEX ix_memory_dream_prepare_runs_scope_updated ON memory_dream_prepare_runs (project_id, owner_user_id, updated_at DESC, job_id DESC);

CREATE UNIQUE INDEX uq_memory_dream_prepare_runs_active_thread ON memory_dream_prepare_runs (project_id, owner_user_id, thread_id) WHERE completed_at IS NULL;

CREATE TABLE memory_document_versions (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    namespace VARCHAR(255) NOT NULL,
    version BIGINT NOT NULL,
    content TEXT NOT NULL,
    content_digest CHAR(64) NOT NULL,
    unified_diff TEXT NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    dream_job_id UUID,
    history_from BIGINT,
    history_to BIGINT,
    history_count INTEGER,
    prompt_version VARCHAR(64),
    needs_review BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_memory_document_versions PRIMARY KEY (project_id, owner_user_id, namespace, version),
    CONSTRAINT fk_memory_document_versions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_document_versions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_document_versions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_document_versions_document FOREIGN KEY(project_id, owner_user_id, namespace) REFERENCES memory_documents (project_id, owner_user_id, namespace) ON DELETE CASCADE,
    CONSTRAINT fk_memory_document_versions_dream_run FOREIGN KEY(dream_job_id, project_id, owner_user_id, namespace) REFERENCES memory_dream_runs (job_id, project_id, owner_user_id, namespace) ON DELETE CASCADE,
    CONSTRAINT ck_memory_document_versions_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_document_versions_version CHECK (version >= 1),
    CONSTRAINT ck_memory_document_versions_digest CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_document_versions_content_size CHECK (char_length(content) <= 16000),
    CONSTRAINT ck_memory_document_versions_trigger CHECK (trigger IN ('auto_dream', 'manual_dream', 'budget_rewrite', 'restore')),
    CONSTRAINT ck_memory_document_versions_source CHECK ((trigger = 'restore' AND dream_job_id IS NULL AND history_from IS NULL AND history_to IS NULL AND history_count IS NULL AND prompt_version IS NULL) OR (trigger = 'budget_rewrite' AND dream_job_id IS NOT NULL AND history_from IS NULL AND history_to IS NULL AND history_count = 0 AND prompt_version IS NOT NULL AND prompt_version <> '') OR (trigger IN ('auto_dream', 'manual_dream') AND dream_job_id IS NOT NULL AND history_from >= 1 AND history_to >= history_from AND history_count BETWEEN 1 AND 20 AND prompt_version IS NOT NULL AND prompt_version <> ''))
);

CREATE UNIQUE INDEX uq_memory_document_versions_dream_job ON memory_document_versions (dream_job_id) WHERE dream_job_id IS NOT NULL;

CREATE TABLE run_mcp_secret_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    mcp_server_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    slot_id UUID NOT NULL,
    secret_revision BIGINT NOT NULL,
    secret_generation_id UUID NOT NULL,
    secret_generation_digest CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_run_mcp_secret_snapshots PRIMARY KEY (project_id, owner_user_id, run_id, mcp_server_version_id, slot_id),
    CONSTRAINT fk_run_mcp_secret_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_secret_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_secret_snapshots_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_secret_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_mcp_secret_snapshots_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_secret_snapshots_slot FOREIGN KEY(mcp_server_version_id, slot_id) REFERENCES mcp_version_secret_slots (mcp_server_version_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_run_mcp_secret_snapshots_revision CHECK (secret_revision >= 1),
    CONSTRAINT ck_run_mcp_secret_snapshots_generation_digest CHECK (secret_generation_digest ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_run_mcp_secret_snapshots_generation ON run_mcp_secret_snapshots (secret_generation_id);

CREATE TABLE file_chunks (
    file_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    content BYTEA NOT NULL,
    size INTEGER NOT NULL,
    sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (file_id, chunk_index),
    CONSTRAINT ck_file_chunks_index CHECK (chunk_index >= 0),
    CONSTRAINT ck_file_chunks_size CHECK (size >= 0),
    CONSTRAINT ck_file_chunks_content_size CHECK (size = octet_length(content)),
    CONSTRAINT ck_file_chunks_bounded_size CHECK (size > 0 AND size <= 1048576),
    CONSTRAINT ck_file_chunks_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_file_chunks_file_id_files FOREIGN KEY(file_id) REFERENCES files (id) ON DELETE CASCADE
);

CREATE TABLE artifacts (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    file_id UUID NOT NULL,
    display_name VARCHAR(256) NOT NULL,
    media_type VARCHAR(255) NOT NULL,
    artifact_metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT fk_artifacts_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_artifacts_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_artifacts_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_artifacts_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_artifacts_private_file FOREIGN KEY(project_id, owner_user_id, thread_id, file_id) REFERENCES files (project_id, owner_user_id, thread_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_artifacts_private_scope UNIQUE (project_id, owner_user_id, thread_id, run_id, id)
);

CREATE INDEX ix_artifacts_private_active ON artifacts (project_id, owner_user_id, thread_id, created_at) WHERE deleted_at IS NULL;

CREATE TABLE agent_design_operations (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    session_id UUID NOT NULL,
    operation_kind VARCHAR(16) NOT NULL,
    idempotency_key_hash CHAR(64) NOT NULL,
    request_checksum CHAR(64) NOT NULL,
    status VARCHAR(16) DEFAULT 'in_progress' NOT NULL,
    result_revision BIGINT,
    public_error_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    stop_requested_at TIMESTAMP WITH TIME ZONE,
    requested_generation_profile_json JSONB,
    effective_generation_profile_json JSONB,
    CONSTRAINT pk_agent_design_operations PRIMARY KEY (id),
    CONSTRAINT ck_agent_design_operations_kind CHECK (operation_kind IN ('turn', 'commit', 'cancel')),
    CONSTRAINT ck_agent_design_operations_status CHECK (status IN ('in_progress', 'completed', 'failed', 'stopped')),
    CONSTRAINT ck_agent_design_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
    CONSTRAINT ck_agent_design_operations_completion CHECK ((status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL) OR (status = 'stopped' AND result_revision IS NOT NULL AND public_error_code IS NULL)),
    CONSTRAINT ck_agent_design_operations_generation_profile CHECK ((requested_generation_profile_json IS NULL AND effective_generation_profile_json IS NULL) OR (operation_kind = 'turn' AND requested_generation_profile_json IS NOT NULL AND effective_generation_profile_json IS NOT NULL)),
    CONSTRAINT fk_agent_design_operations_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES agent_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_agent_design_operations_private_scope UNIQUE (project_id, owner_user_id, session_id, id),
    CONSTRAINT uq_agent_design_operations_idempotency UNIQUE (project_id, owner_user_id, operation_kind, idempotency_key_hash),
    CONSTRAINT fk_agent_design_operations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_operations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_agent_design_operations_session ON agent_design_operations (project_id, owner_user_id, session_id, created_at DESC);

CREATE TABLE project_mcp_secret_states (
    project_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    slot_id UUID NOT NULL,
    current_generation_id UUID,
    revision BIGINT DEFAULT 0 NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_project_mcp_secret_states PRIMARY KEY (project_id, mcp_server_id, mcp_server_version_id, slot_id),
    CONSTRAINT fk_project_mcp_secret_states_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_states_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_states_slot FOREIGN KEY(mcp_server_version_id, slot_id) REFERENCES mcp_version_secret_slots (mcp_server_version_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_states_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT ck_project_mcp_secret_states_revision CHECK (revision >= 0)
);

CREATE TABLE project_mcp_secret_generations (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    slot_id UUID NOT NULL,
    revision BIGINT NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    envelope_digest CHAR(64) NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT fk_project_mcp_secret_generations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_generations_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_generations_slot FOREIGN KEY(mcp_server_version_id, slot_id) REFERENCES mcp_version_secret_slots (mcp_server_version_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_secret_generations_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_mcp_secret_generations_owner_id UNIQUE (project_id, mcp_server_id, mcp_server_version_id, slot_id, id),
    CONSTRAINT uq_project_mcp_secret_generations_revision UNIQUE (project_id, mcp_server_id, mcp_server_version_id, slot_id, revision),
    CONSTRAINT ck_project_mcp_secret_generations_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_mcp_secret_generations_envelope CHECK (octet_length(nonce) = 12 AND octet_length(ciphertext) >= 16),
    CONSTRAINT ck_project_mcp_secret_generations_digest CHECK (envelope_digest ~ '^[0-9a-f]{64}$')
);

CREATE TABLE skill_design_operations (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    session_id UUID NOT NULL,
    operation_kind VARCHAR(16) NOT NULL,
    idempotency_key_hash CHAR(64) NOT NULL,
    request_checksum CHAR(64) NOT NULL,
    status VARCHAR(16) DEFAULT 'in_progress' NOT NULL,
    result_revision BIGINT,
    public_error_code VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    run_id VARCHAR(64),
    terminal_kind VARCHAR(16),
    terminal_request_checksum CHAR(64),
    stop_requested_at TIMESTAMP WITH TIME ZONE,
    CONSTRAINT pk_skill_design_operations PRIMARY KEY (id),
    CONSTRAINT ck_skill_design_operations_kind CHECK (operation_kind IN ('turn', 'validate', 'commit', 'cancel')),
    CONSTRAINT ck_skill_design_operations_status CHECK (status IN ('in_progress', 'completed', 'failed', 'stopped')),
    CONSTRAINT ck_skill_design_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
    CONSTRAINT ck_skill_design_operations_terminal_kind CHECK (terminal_kind IS NULL OR terminal_kind IN ('clarification', 'candidate')),
    CONSTRAINT ck_skill_design_operations_terminal_checksum CHECK (terminal_request_checksum IS NULL OR terminal_request_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_design_operations_terminal_pair CHECK ((terminal_kind IS NULL AND terminal_request_checksum IS NULL) OR (terminal_kind IS NOT NULL AND terminal_request_checksum IS NOT NULL)),
    CONSTRAINT ck_skill_design_operations_completion CHECK ((status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL) OR (status = 'stopped' AND result_revision IS NOT NULL AND public_error_code IS NULL)),
    CONSTRAINT fk_skill_design_operations_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES skill_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_skill_design_operations_run FOREIGN KEY(project_id, owner_user_id, run_id) REFERENCES runs (project_id, owner_user_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT uq_skill_design_operations_private_scope UNIQUE (project_id, owner_user_id, session_id, id),
    CONSTRAINT uq_skill_design_operations_idempotency UNIQUE (project_id, owner_user_id, operation_kind, idempotency_key_hash),
    CONSTRAINT uq_skill_design_operations_run UNIQUE (project_id, owner_user_id, run_id),
    CONSTRAINT fk_skill_design_operations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_operations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_skill_design_operations_session ON skill_design_operations (project_id, owner_user_id, session_id, created_at DESC);

CREATE TABLE skill_design_draft_files (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    session_id UUID NOT NULL,
    path VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    content BYTEA NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_skill_design_draft_files PRIMARY KEY (project_id, owner_user_id, session_id, path),
    CONSTRAINT fk_skill_design_draft_files_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES skill_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_skill_design_draft_files_safe_path CHECK (path <> '' AND path !~ '(^/|(^|/)\.\.(/|$))'),
    CONSTRAINT ck_skill_design_draft_files_size CHECK (size_bytes >= 0 AND size_bytes <= 104857600),
    CONSTRAINT ck_skill_design_draft_files_content_size CHECK (size_bytes = octet_length(content)),
    CONSTRAINT ck_skill_design_draft_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE execution_approval_output_delivery_obligations (
    approval_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    mode VARCHAR(16) DEFAULT 'any_one' NOT NULL,
    status VARCHAR(24) DEFAULT 'deferred' NOT NULL,
    continuation_run_id VARCHAR(64),
    continuation_job_id UUID,
    intent_tool_call_id VARCHAR(128),
    intent_digest CHAR(64),
    intent_private_json JSON,
    satisfied_artifact_id UUID,
    version BIGINT DEFAULT 1 NOT NULL,
    assigned_at TIMESTAMP WITH TIME ZONE,
    intent_recorded_at TIMESTAMP WITH TIME ZONE,
    terminal_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (approval_id),
    CONSTRAINT uq_ea_output_delivery_obligations_private_scope UNIQUE (approval_id, project_id, owner_user_id, thread_id),
    CONSTRAINT fk_ea_output_delivery_obligations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_ea_output_delivery_obligations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_ea_output_delivery_obligations_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_ea_output_delivery_obligations_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_ea_output_delivery_obligations_approval FOREIGN KEY(approval_id, project_id, owner_user_id, thread_id) REFERENCES execution_approval_requests (id, project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_ea_output_delivery_obligations_continuation_run FOREIGN KEY(project_id, owner_user_id, thread_id, continuation_run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_ea_output_delivery_obligations_continuation_job FOREIGN KEY(continuation_job_id, project_id, owner_user_id, continuation_run_id) REFERENCES jobs (id, project_id, owner_user_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_ea_output_delivery_obligations_satisfied_artifact FOREIGN KEY(project_id, owner_user_id, thread_id, continuation_run_id, satisfied_artifact_id) REFERENCES artifacts (project_id, owner_user_id, thread_id, run_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_ea_output_delivery_obligations_mode CHECK (mode IN ('any_one')),
    CONSTRAINT ck_ea_output_delivery_obligations_status CHECK (status IN ('deferred', 'assigned', 'intent_recorded', 'delivered', 'cancelled', 'blocked_unknown', 'failed')),
    CONSTRAINT ck_ea_output_delivery_obligations_assignment_shape CHECK ((continuation_run_id IS NULL) = (continuation_job_id IS NULL) AND (continuation_run_id IS NULL) = (assigned_at IS NULL)),
    CONSTRAINT ck_ea_output_delivery_obligations_intent_shape CHECK ((intent_tool_call_id IS NULL AND intent_digest IS NULL AND intent_private_json IS NULL AND intent_recorded_at IS NULL) OR (intent_tool_call_id IS NOT NULL AND intent_tool_call_id <> '' AND intent_tool_call_id = btrim(intent_tool_call_id) AND intent_digest ~ '^[0-9a-f]{64}$' AND json_typeof(intent_private_json) = 'object' AND octet_length(intent_private_json::text) <= 1048576 AND intent_recorded_at IS NOT NULL)),
    CONSTRAINT ck_ea_output_delivery_obligations_lifecycle_shape CHECK ((status = 'deferred' AND continuation_run_id IS NULL AND intent_tool_call_id IS NULL AND satisfied_artifact_id IS NULL AND terminal_at IS NULL) OR (status = 'assigned' AND continuation_run_id IS NOT NULL AND intent_tool_call_id IS NULL AND satisfied_artifact_id IS NULL AND terminal_at IS NULL) OR (status = 'intent_recorded' AND continuation_run_id IS NOT NULL AND intent_tool_call_id IS NOT NULL AND satisfied_artifact_id IS NULL AND terminal_at IS NULL) OR (status = 'delivered' AND continuation_run_id IS NOT NULL AND intent_tool_call_id IS NOT NULL AND satisfied_artifact_id IS NOT NULL AND terminal_at IS NOT NULL) OR (status = 'cancelled' AND satisfied_artifact_id IS NULL AND terminal_at IS NOT NULL) OR (status IN ('blocked_unknown', 'failed') AND continuation_run_id IS NOT NULL AND satisfied_artifact_id IS NULL AND terminal_at IS NOT NULL)),
    CONSTRAINT ck_ea_output_delivery_obligations_version CHECK (version >= 1),
    CONSTRAINT ck_ea_output_delivery_obligations_timestamps CHECK (updated_at >= created_at AND (assigned_at IS NULL OR assigned_at >= created_at) AND (intent_recorded_at IS NULL OR intent_recorded_at >= assigned_at) AND (terminal_at IS NULL OR terminal_at >= COALESCE(intent_recorded_at, assigned_at, created_at)))
);

CREATE INDEX ix_ea_output_delivery_obligations_private_status ON execution_approval_output_delivery_obligations (project_id, owner_user_id, thread_id, status, updated_at);

CREATE TABLE agent_design_activities (
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    session_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    attempt INTEGER,
    kind VARCHAR(40) NOT NULL,
    payload_json JSONB DEFAULT '{}'::jsonb NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_agent_design_activities PRIMARY KEY (seq),
    CONSTRAINT ck_agent_design_activities_attempt CHECK (attempt IS NULL OR attempt IN (1, 2)),
    CONSTRAINT ck_agent_design_activities_kind CHECK (kind IN ('turn_accepted', 'attempt_started', 'reasoning', 'candidate_generated', 'validation_started', 'validation_passed', 'validation_failed', 'repair_started', 'turn_terminal', 'commit_accepted', 'commit_validation_started', 'commit_validation_passed', 'commit_persistence_started', 'commit_persistence_completed', 'commit_terminal')),
    CONSTRAINT fk_agent_design_activities_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES agent_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_design_activities_operation FOREIGN KEY(project_id, owner_user_id, session_id, operation_id) REFERENCES agent_design_operations (project_id, owner_user_id, session_id, id) ON DELETE CASCADE
);

CREATE INDEX ix_agent_design_activities_session_seq ON agent_design_activities (project_id, owner_user_id, session_id, seq);

CREATE UNIQUE INDEX uq_agent_design_activities_terminal ON agent_design_activities (operation_id) WHERE kind IN ('turn_terminal', 'commit_terminal');

CREATE TABLE skill_design_activities (
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    session_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    run_id VARCHAR(64),
    attempt BIGINT,
    source_event_id VARCHAR(255),
    kind VARCHAR(40) NOT NULL,
    payload_json JSONB DEFAULT '{}'::jsonb NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_skill_design_activities PRIMARY KEY (seq),
    CONSTRAINT ck_skill_design_activities_attempt CHECK (attempt IS NULL OR attempt >= 1),
    CONSTRAINT ck_skill_design_activities_kind CHECK (kind IN ('request_accepted', 'attempt_started', 'reasoning', 'tool_started', 'tool_completed', 'tool_failed', 'candidate_generated', 'validation_started', 'validation_passed', 'validation_failed', 'repair_started', 'run_terminal', 'commit_accepted', 'commit_validation_started', 'commit_validation_passed', 'commit_persistence_started', 'commit_persistence_completed', 'commit_terminal')),
    CONSTRAINT fk_skill_design_activities_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES skill_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_skill_design_activities_operation FOREIGN KEY(project_id, owner_user_id, session_id, operation_id) REFERENCES skill_design_operations (project_id, owner_user_id, session_id, id) ON DELETE CASCADE
);

CREATE INDEX ix_skill_design_activities_session_seq ON skill_design_activities (project_id, owner_user_id, session_id, seq);

CREATE UNIQUE INDEX uq_skill_design_activities_source_event ON skill_design_activities (operation_id, source_event_id) WHERE source_event_id IS NOT NULL;

CREATE UNIQUE INDEX uq_skill_design_activities_terminal ON skill_design_activities (operation_id) WHERE kind IN ('run_terminal', 'commit_terminal');

CREATE TABLE skill_design_operation_baseline_files (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    session_id UUID NOT NULL,
    operation_id UUID NOT NULL,
    path VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    content BYTEA NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_skill_design_operation_baseline_files PRIMARY KEY (project_id, owner_user_id, session_id, operation_id, path),
    CONSTRAINT fk_skill_design_operation_baseline_files_operation FOREIGN KEY(project_id, owner_user_id, session_id, operation_id) REFERENCES skill_design_operations (project_id, owner_user_id, session_id, id) ON DELETE CASCADE,
    CONSTRAINT ck_skill_design_operation_baseline_files_safe_path CHECK (path <> '' AND path !~ '(^/|(^|/)\.\.(/|$))'),
    CONSTRAINT ck_skill_design_operation_baseline_files_size CHECK (size_bytes >= 0 AND size_bytes <= 2097152),
    CONSTRAINT ck_skill_design_operation_baseline_files_content_size CHECK (size_bytes = octet_length(content)),
    CONSTRAINT ck_skill_design_operation_baseline_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE TABLE execution_approval_output_delivery_candidates (
    approval_id UUID NOT NULL,
    file_id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    logical_path VARCHAR(1024) NOT NULL,
    file_version BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (approval_id, file_id),
    CONSTRAINT uq_ea_output_delivery_candidates_path UNIQUE (approval_id, logical_path),
    CONSTRAINT fk_ea_output_delivery_candidates_obligation FOREIGN KEY(approval_id, project_id, owner_user_id, thread_id) REFERENCES execution_approval_output_delivery_obligations (approval_id, project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_ea_output_delivery_candidates_private_file FOREIGN KEY(project_id, owner_user_id, thread_id, file_id) REFERENCES files (project_id, owner_user_id, thread_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_ea_output_delivery_candidates_path CHECK (logical_path LIKE 'outputs/%%' AND logical_path <> 'outputs/' AND logical_path !~ '(^|/)\.\.(/|$)' AND logical_path !~ '^[A-Za-z]:'),
    CONSTRAINT ck_ea_output_delivery_candidates_version CHECK (file_version >= 1),
    CONSTRAINT ck_ea_output_delivery_candidates_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_ea_output_delivery_candidates_private ON execution_approval_output_delivery_candidates (project_id, owner_user_id, thread_id, approval_id);

ALTER TABLE runs ADD CONSTRAINT fk_runs_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE;

ALTER TABLE memory_dream_runs ADD CONSTRAINT fk_memory_dream_runs_result_version FOREIGN KEY(project_id, owner_user_id, namespace, result_version) REFERENCES memory_document_versions (project_id, owner_user_id, namespace, version) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE system_model_configs ADD CONSTRAINT fk_system_model_configs_current_secret_generation FOREIGN KEY(id, current_secret_generation_id) REFERENCES system_model_secret_generations (model_config_id, id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_task FOREIGN KEY(project_id, owner_user_id, task_id) REFERENCES scheduled_tasks (project_id, owner_user_id, id) ON DELETE CASCADE;

ALTER TABLE project_mcp_secret_states ADD CONSTRAINT fk_project_mcp_secret_states_current_generation FOREIGN KEY(project_id, mcp_server_id, mcp_server_version_id, slot_id, current_generation_id) REFERENCES project_mcp_secret_generations (project_id, mcp_server_id, mcp_server_version_id, slot_id, id) ON DELETE RESTRICT;

ALTER TABLE system_runtime_policies ADD CONSTRAINT fk_system_runtime_policies_current_version FOREIGN KEY(section, current_version_id) REFERENCES system_runtime_policy_versions (section, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_job FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_job FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT;

ALTER TABLE mcp_servers ADD CONSTRAINT fk_mcp_servers_current_published_version FOREIGN KEY(id, current_published_version_id) REFERENCES mcp_server_versions (mcp_server_id, id);

ALTER TABLE dead_jobs ADD CONSTRAINT fk_dead_jobs_job FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_lease_worker FOREIGN KEY(lease_owner_id) REFERENCES worker_nodes (id) ON DELETE SET NULL;

ALTER TABLE dead_jobs ADD CONSTRAINT fk_dead_jobs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE project_skill_secret_states ADD CONSTRAINT fk_project_skill_secret_states_current_generation FOREIGN KEY(project_id, skill_id, skill_version_id, secret_name, current_generation_id) REFERENCES project_skill_secret_generations (project_id, skill_id, skill_version_id, secret_name, id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE system_model_catalog_state ADD CONSTRAINT fk_system_model_catalog_state_default_model FOREIGN KEY(default_model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_private_run FOREIGN KEY(project_id, owner_user_id, run_id, origin_trace_id) REFERENCES runs (project_id, owner_user_id, run_id, origin_trace_id) ON DELETE RESTRICT;

ALTER TABLE project_channel_secret_states ADD CONSTRAINT fk_project_channel_secret_states_current_generation FOREIGN KEY(project_id, channel_instance_id, current_generation_id) REFERENCES project_channel_secret_generations (project_id, channel_instance_id, id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_automation_occurrence FOREIGN KEY(project_id, owner_user_id, automation_occurrence_id) REFERENCES scheduled_task_runs (project_id, owner_user_id, id) ON DELETE RESTRICT;

ALTER TABLE skills ADD CONSTRAINT fk_skills_current_version FOREIGN KEY(id, current_version_id) REFERENCES skill_versions (skill_id, id);

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_predecessor_dead_job FOREIGN KEY(predecessor_dead_job_id) REFERENCES dead_jobs (job_id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION enforce_scheduled_task_agent_project()
RETURNS trigger AS $$
BEGIN
    IF TG_TABLE_NAME = 'scheduled_tasks' THEN
        IF NEW.agent_scope = 'project' THEN
            PERFORM 1
            FROM agents
            WHERE id = NEW.agent_asset_id
              AND scope = 'project'
              AND project_id = NEW.project_id
            FOR SHARE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'project Agent must belong to the scheduled task project'
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
        END IF;
        RETURN NEW;
    END IF;

    IF TG_TABLE_NAME = 'agents'
       AND NEW.project_id IS DISTINCT FROM OLD.project_id
       AND EXISTS (
           SELECT 1
           FROM scheduled_tasks task
           WHERE task.agent_asset_id = OLD.id
             AND task.agent_scope = 'project'
             AND task.project_id IS DISTINCT FROM NEW.project_id
       ) THEN
        RAISE EXCEPTION 'cannot move a project Agent referenced by scheduled tasks'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_scheduled_tasks_agent_project BEFORE INSERT OR UPDATE OF project_id, agent_asset_id, agent_scope ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project();

CREATE TRIGGER trg_agents_scheduled_task_project BEFORE UPDATE OF project_id ON agents FOR EACH ROW EXECUTE FUNCTION enforce_scheduled_task_agent_project();

CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
DECLARE
    asset_scope text;
BEGIN
    IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
       AND TG_TABLE_NAME = 'mcp_server_versions' THEN
        SELECT scope INTO asset_scope FROM mcp_servers
        WHERE id = NEW.mcp_server_id;
        IF asset_scope = 'system' THEN
            RETURN NEW;
        END IF;
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code', 'files_sealed'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code', 'files_sealed'
    ]::text[]) THEN
        RAISE EXCEPTION 'shared asset version payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_skill_version_files_seal_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.files_sealed IS NOT DISTINCT FROM OLD.files_sealed
       OR (OLD.files_sealed IS FALSE AND NEW.files_sealed IS TRUE) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid Skill version file seal transition'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION verify_skill_version_file_facts()
RETURNS trigger AS $$
DECLARE
    current_version skill_versions%ROWTYPE;
    actual_file_count bigint;
    actual_content_size bigint;
BEGIN
    SELECT * INTO current_version
    FROM skill_versions
    WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF current_version.files_sealed IS NOT TRUE THEN
        RAISE EXCEPTION 'Skill version files must be sealed before commit'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT count(*), coalesce(sum(size_bytes), 0)
    INTO actual_file_count, actual_content_size
    FROM skill_version_files
    WHERE skill_version_id = current_version.id;
    IF actual_file_count IS DISTINCT FROM current_version.file_count
       OR actual_content_size IS DISTINCT FROM current_version.content_size_bytes THEN
        RAISE EXCEPTION 'Skill version file facts do not match persisted files'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION bump_asset_catalog_generation()
RETURNS trigger AS $$
BEGIN
    INSERT INTO asset_catalog_state (id, generation, updated_at)
    VALUES (1, 1, now())
    ON CONFLICT (id) DO UPDATE
      SET generation = asset_catalog_state.generation + 1,
          updated_at = now();
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION ensure_system_binding_eligible_version()
RETURNS trigger AS $$
DECLARE
    version_revoked_at timestamp with time zone;
    current_id uuid;
    asset_status text;
    version_status text;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'project_system_agent_bindings' THEN
            SELECT definition_id, status INTO current_id, asset_status
            FROM agents
            WHERE id = NEW.system_agent_id AND scope = 'system'
            FOR UPDATE;
        WHEN 'project_system_skill_bindings' THEN
            SELECT current_version_id, status INTO current_id, asset_status
            FROM skills
            WHERE id = NEW.system_skill_id AND scope = 'system'
            FOR UPDATE;
            SELECT revoked_at INTO version_revoked_at
            FROM skill_versions
            WHERE id = current_id AND skill_id = NEW.system_skill_id
            FOR UPDATE;
            IF TG_OP = 'UPDATE'
               AND OLD.enabled IS TRUE
               AND NEW.enabled IS FALSE
               AND OLD.system_skill_id = NEW.system_skill_id THEN
                RETURN NEW;
            END IF;
        WHEN 'project_system_mcp_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM mcp_server_versions
            WHERE id = NEW.mcp_server_version_id
              AND mcp_server_id = NEW.system_mcp_server_id
            FOR UPDATE;
        ELSE
            RAISE EXCEPTION 'unsupported system binding table';
    END CASE;
    IF TG_TABLE_NAME = 'project_system_mcp_bindings'
       AND version_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'system MCP binding requires a published version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME IN ('project_system_agent_bindings', 'project_system_skill_bindings')
       AND (current_id IS NULL OR asset_status IS DISTINCT FROM 'active'
       OR (TG_TABLE_NAME = 'project_system_skill_bindings'
           AND version_revoked_at IS NOT NULL)) THEN
        RAISE EXCEPTION 'system binding requires an eligible definition or Current Version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_system_skill_version_revocation()
RETURNS trigger AS $$
DECLARE
    asset_scope text;
    asset_project_id uuid;
    current_id uuid;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.revoked_at IS NOT NULL
           OR NEW.revoked_by_user_id IS NOT NULL
           OR NEW.revocation_reason_code IS NOT NULL THEN
            RAISE EXCEPTION 'skill version must be created unrevoked'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.revoked_at IS NOT DISTINCT FROM OLD.revoked_at
       AND NEW.revoked_by_user_id IS NOT DISTINCT FROM OLD.revoked_by_user_id
       AND NEW.revocation_reason_code IS NOT DISTINCT FROM OLD.revocation_reason_code THEN
        RETURN NEW;
    END IF;

    IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
       AND OLD.revoked_at IS NOT NULL
       AND NEW.revoked_at IS NULL
       AND NEW.revoked_by_user_id IS NULL
       AND NEW.revocation_reason_code IS NULL THEN
        SELECT scope, project_id, current_version_id
        INTO asset_scope, asset_project_id, current_id
        FROM skills
        WHERE id = NEW.skill_id
        FOR UPDATE;
        IF asset_scope = 'system'
           AND asset_project_id IS NULL
           AND current_id = NEW.id
           AND NEW.version_number = 1 THEN
            RETURN NEW;
        END IF;
    END IF;
    IF OLD.revoked_at IS NOT NULL
       OR OLD.revoked_by_user_id IS NOT NULL
       OR OLD.revocation_reason_code IS NOT NULL
       OR NEW.revoked_at IS NULL
       OR NEW.revoked_by_user_id IS NULL
       OR NEW.revocation_reason_code IS NULL
       OR NEW.revocation_reason_code NOT IN ('security', 'policy', 'integrity') THEN
        RAISE EXCEPTION 'system skill version revocation is irreversible'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT scope, project_id, current_version_id
    INTO asset_scope, asset_project_id, current_id
    FROM skills
    WHERE id = NEW.skill_id
    FOR UPDATE;
    IF asset_scope IS DISTINCT FROM 'system'
       OR asset_project_id IS NOT NULL
       OR current_id IS DISTINCT FROM NEW.id
       OR NEW.version_number != 1 THEN
        RAISE EXCEPTION 'only a System Skill Current v1 can be revoked'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION prevent_bound_mcp_published_version_downgrade()
RETURNS trigger AS $$
DECLARE
    is_bound boolean;
BEGIN
    IF OLD.workflow_status = 'published'
       AND NEW.workflow_status IS DISTINCT FROM 'published' THEN
        SELECT EXISTS (
            SELECT 1 FROM project_system_mcp_bindings
            WHERE mcp_server_version_id = OLD.id
        ) INTO is_bound;
        IF is_bound THEN
            RAISE EXCEPTION 'bound published version cannot change workflow status'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION prevent_asset_version_child_mutation()
RETURNS trigger AS $$
DECLARE
    parent_version_id uuid;
    parent_status text;
    parent_scope text;
    parent_project_id uuid;
    parent_asset_id uuid;
    parent_files_sealed boolean;
    purge_allowed boolean := false;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'skill_version_files' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
            SELECT asset.scope, asset.project_id, asset.id, version.files_sealed
            INTO parent_scope, parent_project_id, parent_asset_id,
                 parent_files_sealed
            FROM skill_versions version
            JOIN skills asset ON asset.id = version.skill_id
            WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM skill_versions version
                    JOIN skills asset ON asset.id = version.skill_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.skill_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                      AND NOT EXISTS (
                          SELECT 1
                          FROM run_skill_version_refs pinned
                          WHERE pinned.skill_version_id = version.id
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM run_asset_versions legacy
                          WHERE legacy.asset_kind = 'skill'
                            AND legacy.asset_scope = 'project'
                            AND legacy.asset_id = asset.id
                            AND legacy.version_id = version.id
                            AND legacy.snapshot_schema_version IN (2, 3)
                      )
                ) INTO purge_allowed;
            END IF;
        WHEN 'mcp_version_secret_slots' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;
            SELECT asset.scope, asset.project_id, asset.id,
                   version.workflow_status
            INTO parent_scope, parent_project_id, parent_asset_id, parent_status
            FROM mcp_server_versions version
            JOIN mcp_servers asset ON asset.id = version.mcp_server_id
            WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM mcp_server_versions version
                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.mcp_server_version_id
                      AND asset.scope = 'project'
                      AND (
                          (
                              project.status = 'pending_deletion'
                              AND project.deletion_effective_at IS NOT NULL
                              AND project.deletion_effective_at <= now()
                          )
                          OR (
                              project.status = 'active'
                              AND project.is_suspended IS FALSE
                              AND asset.status = 'archived'
                              AND asset.current_published_version_id IS NULL
                              AND current_setting(
                                  'deerflow.mcp_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) INTO purge_allowed;
            END IF;
        ELSE
            RAISE EXCEPTION 'unsupported version child table';
    END CASE;
    IF TG_TABLE_NAME = 'skill_version_files' THEN
        IF TG_OP = 'INSERT'
           AND parent_files_sealed IS FALSE
           AND current_setting('deerflow.asset_version_assembly', true)
               = parent_version_id::text THEN
            RETURN NEW;
        END IF;
        IF TG_OP = 'DELETE' AND purge_allowed THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Skill version files are immutable outside initial assembly'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_TABLE_NAME = 'mcp_version_secret_slots' THEN
        IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
           AND parent_scope = 'system' THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
        IF current_setting('deerflow.asset_version_assembly', true)
           = parent_version_id::text THEN
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END IF;
        IF TG_OP = 'DELETE' AND purge_allowed THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'MCP version child rows are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' AND purge_allowed THEN
        RETURN OLD;
    END IF;
    IF parent_status IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'published version child rows are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_shared_asset_version_state_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.workflow_status = OLD.workflow_status
       OR (OLD.workflow_status = 'draft'
           AND NEW.workflow_status IN ('pending_approval', 'published'))
       OR (OLD.workflow_status = 'pending_approval'
           AND NEW.workflow_status IN ('published', 'rejected')) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid shared asset version workflow transition'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_agent_definition_mutation()
RETURNS trigger AS $$
DECLARE
    target_agent_id uuid;
    target_scope text;
    target_project_id uuid;
    project_status text;
    project_deletion_effective_at timestamptz;
    referenced_scope text;
    referenced_project_id uuid;
    referenced_status text;
BEGIN
    IF TG_TABLE_NAME = 'agents' THEN
        IF OLD.scope = 'system'
           AND current_setting('deerflow.system_asset_upgrade', true)
               IS NOT DISTINCT FROM 'on' THEN
            IF NEW.definition_id IS DISTINCT FROM OLD.definition_id
               OR NEW.revision != OLD.revision + 1 THEN
                RAISE EXCEPTION 'System Agent definition identity is immutable and revision must advance once'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.scope IS DISTINCT FROM 'project'
           OR current_setting(
               'deerflow.agent_definition_mutation_id', true
           ) IS DISTINCT FROM OLD.id::text
           OR NEW.definition_id IS NOT DISTINCT FROM OLD.definition_id
           OR NEW.revision != OLD.revision + 1 THEN
            RAISE EXCEPTION 'Project Agent definition mutation requires its transaction fence and one revision advance'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;

    target_agent_id := CASE WHEN TG_OP = 'DELETE'
        THEN OLD.agent_id ELSE NEW.agent_id END;
    SELECT agent.scope, agent.project_id, project.status,
           project.deletion_effective_at
    INTO target_scope, target_project_id, project_status,
         project_deletion_effective_at
    FROM agents agent
    LEFT JOIN projects project ON project.id = agent.project_id
    WHERE agent.id = target_agent_id
    FOR UPDATE OF agent;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Agent definition reference requires an Agent'
            USING ERRCODE = 'foreign_key_violation';
    END IF;

    IF NOT (
        (target_scope = 'system'
         AND current_setting('deerflow.system_asset_upgrade', true)
             IS NOT DISTINCT FROM 'on')
        OR
        (target_scope = 'project'
         AND current_setting(
             'deerflow.agent_definition_mutation_id', true
         ) IS NOT DISTINCT FROM target_agent_id::text)
        OR
        (target_scope = 'project'
         AND project_status = 'pending_deletion'
         AND project_deletion_effective_at IS NOT NULL
         AND project_deletion_effective_at <= now())
        OR
        (target_scope = 'project'
         AND current_setting(
             'deerflow.agent_hard_delete_asset_id', true
         ) IS NOT DISTINCT FROM target_agent_id::text)
    ) THEN
        RAISE EXCEPTION 'Agent definition reference mutation requires its transaction fence'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF NEW.agent_id IS DISTINCT FROM target_agent_id THEN
        RAISE EXCEPTION 'Agent definition reference cannot move between Agents'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF TG_TABLE_NAME = 'agent_skill_refs' THEN
        SELECT skill.scope, skill.project_id, skill.status
        INTO referenced_scope, referenced_project_id, referenced_status
        FROM skills skill
        WHERE skill.id = NEW.skill_asset_id
          AND skill.scope = NEW.skill_asset_scope
        FOR SHARE;
    ELSIF TG_TABLE_NAME = 'agent_mcp_refs' THEN
        SELECT server.scope, server.project_id, server.status
        INTO referenced_scope, referenced_project_id, referenced_status
        FROM mcp_server_versions version
        JOIN mcp_servers server ON server.id = version.mcp_server_id
        WHERE version.id = NEW.mcp_server_version_id
        FOR SHARE OF version, server;
    ELSE
        RAISE EXCEPTION 'unsupported Agent definition reference table';
    END IF;
    IF NOT FOUND
       OR referenced_status = 'archived'
       OR (target_scope = 'system' AND referenced_scope != 'system')
       OR (
           target_scope = 'project'
           AND referenced_scope = 'project'
           AND referenced_project_id IS DISTINCT FROM target_project_id
       ) THEN
        RAISE EXCEPTION 'Agent definition reference crosses its governed scope'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_skill_archive_transition()
RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'archived' AND NEW.status IS DISTINCT FROM 'archived' THEN
        RAISE EXCEPTION 'archived Skill status is terminal'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_agents_definition_mutation BEFORE UPDATE OF definition_id, description, agents_instructions, soul, identity, user_context, model_ref, model_settings, tool_groups, payload_schema_version, payload_checksum ON agents FOR EACH ROW EXECUTE FUNCTION enforce_agent_definition_mutation();

CREATE TRIGGER trg_agent_skill_refs_definition_mutation BEFORE INSERT OR UPDATE OR DELETE ON agent_skill_refs FOR EACH ROW EXECUTE FUNCTION enforce_agent_definition_mutation();

CREATE TRIGGER trg_agent_mcp_refs_definition_mutation BEFORE INSERT OR UPDATE OR DELETE ON agent_mcp_refs FOR EACH ROW EXECUTE FUNCTION enforce_agent_definition_mutation();

CREATE TRIGGER trg_skills_archive_terminal BEFORE UPDATE OF status ON skills FOR EACH ROW EXECUTE FUNCTION enforce_skill_archive_transition();

CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_skill_versions_files_seal_transition BEFORE UPDATE OF files_sealed ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_skill_version_files_seal_transition();

CREATE CONSTRAINT TRIGGER trg_skill_versions_facts_complete AFTER INSERT OR UPDATE ON skill_versions DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION verify_skill_version_file_facts();

CREATE TRIGGER trg_skill_versions_revocation BEFORE INSERT OR UPDATE OF revoked_at, revoked_by_user_id, revocation_reason_code ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_system_skill_version_revocation();

CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_mcp_secret_slots_immutable BEFORE UPDATE ON mcp_version_secret_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_bindings_current BEFORE INSERT OR UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version();

CREATE TRIGGER trg_skill_bindings_current BEFORE INSERT OR UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version();

CREATE TRIGGER trg_mcp_bindings_published BEFORE INSERT OR UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version();

CREATE TRIGGER trg_mcp_server_versions_bound_published BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_mcp_published_version_downgrade();

CREATE TRIGGER trg_skill_version_files_child_immutable BEFORE INSERT OR DELETE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation();

CREATE TRIGGER trg_mcp_secret_slots_child_immutable BEFORE INSERT OR DELETE ON mcp_version_secret_slots FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation();

CREATE TRIGGER trg_mcp_server_versions_state_transition BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, definition_id, revision ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_version_id, revision ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skill_version_revocations_generation AFTER UPDATE OF revoked_at ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_server_versions_generation AFTER UPDATE OF workflow_status ON mcp_server_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_agent_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_agent_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skill_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_skill_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_mcp_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE OR REPLACE FUNCTION enforce_run_asset_closure_seal_transition()
RETURNS trigger AS $$
BEGIN
    IF NEW.asset_closure_sealed IS NOT DISTINCT FROM OLD.asset_closure_sealed
       OR (OLD.asset_closure_sealed IS FALSE
           AND NEW.asset_closure_sealed IS TRUE) THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'invalid Run asset closure seal transition'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gate_run_closure_child_mutation()
RETURNS trigger AS $$
DECLARE
    exact_project_id uuid;
    exact_owner_user_id text;
    exact_thread_id text;
    exact_run_id text;
    closure_sealed boolean;
    run_found boolean := false;
    claimable_job_exists boolean := false;
    retention_authorized boolean := false;
    ref_parent_exists boolean := false;
BEGIN
    IF TG_OP = 'DELETE' THEN
        exact_project_id := OLD.project_id;
        exact_owner_user_id := OLD.owner_user_id;
        exact_thread_id := OLD.thread_id;
        exact_run_id := OLD.run_id;
    ELSE
        exact_project_id := NEW.project_id;
        exact_owner_user_id := NEW.owner_user_id;
        exact_thread_id := NEW.thread_id;
        exact_run_id := NEW.run_id;
    END IF;

    SELECT asset_closure_sealed
    INTO closure_sealed
    FROM runs
    WHERE project_id = exact_project_id
      AND owner_user_id = exact_owner_user_id
      AND thread_id = exact_thread_id
      AND run_id = exact_run_id
    FOR UPDATE;
    run_found := FOUND;

    IF TG_OP = 'DELETE' AND NOT run_found THEN
        RETURN OLD;
    END IF;
    IF NOT run_found THEN
        RAISE EXCEPTION 'Run closure child requires an exact Run'
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'Run closure child rows are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        -- RetentionPurgeAuthority installs exact Run coordinates in a
        -- transaction-local temp table only after locked eligibility and
        -- quiescence verification.  This is deliberately not a blanket GUC.
        IF to_regclass('pg_temp.retention_purge_run_authority') IS NOT NULL THEN
            EXECUTE
                'SELECT EXISTS (
                     SELECT 1
                     FROM pg_temp.retention_purge_run_authority authority
                     WHERE authority.project_id = $1
                       AND authority.thread_id = $2
                       AND authority.run_id = $3
                       AND authority.purge_id IS NOT NULL
                       AND (
                           (authority.resource_kind = ''project''
                            AND authority.owner_user_id IS NULL)
                           OR
                           (authority.resource_kind IN
                                (''former_owner'', ''account'', ''run'')
                            AND authority.owner_user_id = $4)
                       )
                 )'
            INTO retention_authorized
            USING exact_project_id, exact_thread_id, exact_run_id,
                  exact_owner_user_id;
        END IF;
        IF retention_authorized AND TG_TABLE_NAME = 'run_skill_version_refs' THEN
            SELECT EXISTS (
                SELECT 1
                FROM run_asset_versions parent
                WHERE parent.project_id = OLD.project_id
                  AND parent.owner_user_id = OLD.owner_user_id
                  AND parent.thread_id = OLD.thread_id
                  AND parent.run_id = OLD.run_id
                  AND parent.asset_kind = OLD.asset_kind
                  AND parent.dependency_order = OLD.dependency_order
            ) INTO ref_parent_exists;
            IF ref_parent_exists THEN
                RAISE EXCEPTION 'Run Skill ref cannot be deleted independently'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END IF;
        IF retention_authorized THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Run closure child deletion requires scoped retention authority'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF closure_sealed IS NOT FALSE
       OR current_setting('deerflow.run_asset_closure_assembly', true)
          IS DISTINCT FROM exact_run_id THEN
        RAISE EXCEPTION 'Run closure is not open for exact assembly'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM jobs
        WHERE project_id = exact_project_id
          AND owner_user_id = exact_owner_user_id
          AND run_id = exact_run_id
          AND (
              (status IN ('queued', 'retry_wait')
               AND available_at <= clock_timestamp())
              OR
              (status IN ('leased', 'running')
               AND lease_expires_at <= clock_timestamp())
          )
    ) INTO claimable_job_exists;
    IF claimable_job_exists THEN
        RAISE EXCEPTION 'claimable Job forbids Run closure assembly'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION verify_run_asset_closure()
RETURNS trigger AS $$
DECLARE
    current_run runs%ROWTYPE;
    asset run_asset_versions%ROWTYPE;
    asset_count bigint;
    minimum_dependency_order integer;
    max_dependency_order integer;
    ref_count bigint;
    ref_file_count integer;
    ref_content_size bigint;
    invalid_secret_identity boolean;
BEGIN
    SELECT * INTO current_run
    FROM runs
    WHERE run_id = NEW.run_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    IF current_run.asset_closure_sealed IS NOT TRUE THEN
        RAISE EXCEPTION 'Run asset closure must be sealed before commit'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT count(*), min(dependency_order), max(dependency_order)
    INTO asset_count, minimum_dependency_order, max_dependency_order
    FROM run_asset_versions
    WHERE project_id = current_run.project_id
      AND owner_user_id = current_run.owner_user_id
      AND thread_id = current_run.thread_id
      AND run_id = current_run.run_id;

    IF asset_count = 0 THEN
        SELECT EXISTS (
            SELECT 1 FROM run_skill_secret_snapshots secret
            WHERE secret.project_id = current_run.project_id
              AND secret.owner_user_id = current_run.owner_user_id
              AND secret.thread_id = current_run.thread_id
              AND secret.run_id = current_run.run_id
            UNION ALL
            SELECT 1 FROM run_mcp_secret_snapshots secret
            WHERE secret.project_id = current_run.project_id
              AND secret.owner_user_id = current_run.owner_user_id
              AND secret.thread_id = current_run.thread_id
              AND secret.run_id = current_run.run_id
        ) INTO invalid_secret_identity;
        IF invalid_secret_identity
           OR current_run.status NOT IN
                ('success', 'error', 'timeout', 'interrupted', 'deleted') THEN
            RAISE EXCEPTION 'only a terminal privacy-purged Run may have an empty closure'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NULL;
    END IF;

    IF minimum_dependency_order != 0
       OR max_dependency_order != asset_count - 1 THEN
        RAISE EXCEPTION 'Run asset dependency order must be globally continuous'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM run_asset_versions first_asset
        WHERE first_asset.project_id = current_run.project_id
          AND first_asset.owner_user_id = current_run.owner_user_id
          AND first_asset.thread_id = current_run.thread_id
          AND first_asset.run_id = current_run.run_id
          AND first_asset.dependency_order = 0
          AND first_asset.asset_kind = 'agent'
    ) THEN
        RAISE EXCEPTION 'Run asset closure must begin with an Agent'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM run_skill_secret_snapshots secret
        WHERE secret.project_id = current_run.project_id
          AND secret.owner_user_id = current_run.owner_user_id
          AND secret.thread_id = current_run.thread_id
          AND secret.run_id = current_run.run_id
          AND NOT EXISTS (
              SELECT 1
              FROM run_asset_versions parent
              WHERE parent.project_id = secret.project_id
                AND parent.owner_user_id = secret.owner_user_id
                AND parent.thread_id = secret.thread_id
                AND parent.run_id = secret.run_id
                AND parent.asset_kind = 'skill'
                AND parent.asset_id = secret.skill_id
                AND parent.version_id = secret.skill_version_id
          )
    ) INTO invalid_secret_identity;
    IF invalid_secret_identity THEN
        RAISE EXCEPTION 'Run Skill secret snapshot lacks its exact Skill parent'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    SELECT EXISTS (
        SELECT 1
        FROM run_mcp_secret_snapshots secret
        WHERE secret.project_id = current_run.project_id
          AND secret.owner_user_id = current_run.owner_user_id
          AND secret.thread_id = current_run.thread_id
          AND secret.run_id = current_run.run_id
          AND NOT EXISTS (
              SELECT 1
              FROM run_asset_versions parent
              WHERE parent.project_id = secret.project_id
                AND parent.owner_user_id = secret.owner_user_id
                AND parent.thread_id = secret.thread_id
                AND parent.run_id = secret.run_id
                AND parent.asset_kind = 'mcp'
                AND parent.asset_id = secret.mcp_server_id
                AND parent.version_id = secret.mcp_server_version_id
          )
    ) INTO invalid_secret_identity;
    IF invalid_secret_identity THEN
        RAISE EXCEPTION 'Run MCP secret snapshot lacks its exact MCP parent'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    FOR asset IN
        SELECT *
        FROM run_asset_versions
        WHERE project_id = current_run.project_id
          AND owner_user_id = current_run.owner_user_id
          AND thread_id = current_run.thread_id
          AND run_id = current_run.run_id
        ORDER BY dependency_order
    LOOP
        IF jsonb_typeof(asset.snapshot_json) IS DISTINCT FROM 'object'
           OR jsonb_typeof(asset.snapshot_json->'schema_version') IS DISTINCT FROM 'number'
           OR jsonb_typeof(asset.snapshot_json->'kind') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'scope') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'asset_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'version_id') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'checksum') IS DISTINCT FROM 'string'
           OR jsonb_typeof(asset.snapshot_json->'catalog_generation') IS DISTINCT FROM 'number'
           OR jsonb_typeof(asset.snapshot_json->'dependency_version_ids') IS DISTINCT FROM 'array'
           OR asset.snapshot_json->>'schema_version' IS DISTINCT FROM asset.snapshot_schema_version::text
           OR asset.snapshot_json->>'kind' IS DISTINCT FROM asset.asset_kind
           OR asset.snapshot_json->>'scope' IS DISTINCT FROM asset.asset_scope
           OR asset.snapshot_json->>'asset_id' IS DISTINCT FROM asset.asset_id::text
           OR asset.snapshot_json->>'version_id' IS DISTINCT FROM asset.version_id::text
           OR asset.snapshot_json->>'checksum' IS DISTINCT FROM asset.payload_checksum
           OR asset.snapshot_json->>'catalog_generation' IS DISTINCT FROM asset.catalog_generation::text THEN
            RAISE EXCEPTION 'Run asset typed identity disagrees with snapshot JSON'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        IF asset.snapshot_schema_version = 4 AND asset.asset_kind != 'skill' THEN
            RAISE EXCEPTION 'Run asset schema v4 is reserved for Skill references'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;

        SELECT count(*), max(file_count), max(content_size_bytes)
        INTO ref_count, ref_file_count, ref_content_size
        FROM run_skill_version_refs ref
        WHERE ref.project_id = asset.project_id
          AND ref.owner_user_id = asset.owner_user_id
          AND ref.thread_id = asset.thread_id
          AND ref.run_id = asset.run_id
          AND ref.asset_kind = asset.asset_kind
          AND ref.dependency_order = asset.dependency_order;

        IF asset.asset_kind = 'skill' AND asset.snapshot_schema_version = 4 THEN
            IF ref_count != 1
               OR octet_length(asset.snapshot_json::text) > 262144
               OR asset.snapshot_json - 'schema_version' - 'kind' - 'scope'
                    - 'asset_id' - 'version_id' - 'checksum'
                    - 'catalog_generation' - 'dependency_version_ids'
                    - 'skill' != '{}'::jsonb
               OR jsonb_typeof(asset.snapshot_json->'skill') IS DISTINCT FROM 'object'
               OR (asset.snapshot_json->'skill') - 'source' - 'file_count'
                    - 'content_size_bytes' != '{}'::jsonb
               OR jsonb_typeof(asset.snapshot_json->'skill'->'source') IS DISTINCT FROM 'string'
               OR jsonb_typeof(asset.snapshot_json->'skill'->'file_count') IS DISTINCT FROM 'number'
               OR jsonb_typeof(asset.snapshot_json->'skill'->'content_size_bytes') IS DISTINCT FROM 'number'
               OR asset.snapshot_json->'skill'->>'source' IS DISTINCT FROM 'skill_version_ref'
               OR asset.snapshot_json->'skill'->>'file_count' IS DISTINCT FROM ref_file_count::text
               OR asset.snapshot_json->'skill'->>'content_size_bytes' IS DISTINCT FROM ref_content_size::text
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(asset.snapshot_json->'dependency_version_ids') value
                    WHERE jsonb_typeof(value) IS DISTINCT FROM 'string'
                       OR value #>> '{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
               ) THEN
                RAISE EXCEPTION 'Run Skill v4 manifest and exact ref are incomplete'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        ELSIF ref_count != 0 THEN
            RAISE EXCEPTION 'only a Skill v4 parent may own an exact Skill ref'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END LOOP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_runs_asset_closure_seal_transition BEFORE UPDATE OF asset_closure_sealed ON runs FOR EACH ROW EXECUTE FUNCTION enforce_run_asset_closure_seal_transition();

CREATE TRIGGER trg_run_asset_versions_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_asset_versions FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation();

CREATE TRIGGER trg_run_skill_version_refs_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_skill_version_refs FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation();

CREATE TRIGGER trg_run_skill_secret_snapshots_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_skill_secret_snapshots FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation();

CREATE TRIGGER trg_run_mcp_secret_snapshots_closure_mutation BEFORE INSERT OR UPDATE OR DELETE ON run_mcp_secret_snapshots FOR EACH ROW EXECUTE FUNCTION gate_run_closure_child_mutation();

CREATE CONSTRAINT TRIGGER trg_runs_asset_closure_complete AFTER INSERT OR UPDATE ON runs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION verify_run_asset_closure();



CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);



CREATE OR REPLACE FUNCTION reject_schema_v1_append_only_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Schema V1 append-only rows cannot be updated or deleted'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_project_usage_ledger_append_only BEFORE UPDATE OR DELETE ON project_usage_ledger FOR EACH ROW EXECUTE FUNCTION reject_schema_v1_append_only_mutation();

CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_schema_v1_append_only_mutation();

CREATE TRIGGER trg_dead_jobs_append_only BEFORE UPDATE OR DELETE ON dead_jobs FOR EACH ROW EXECUTE FUNCTION reject_schema_v1_append_only_mutation();

CREATE TRIGGER trg_system_runtime_policy_versions_append_only BEFORE UPDATE OR DELETE ON system_runtime_policy_versions FOR EACH ROW EXECUTE FUNCTION reject_schema_v1_append_only_mutation();


CREATE OR REPLACE FUNCTION enforce_context_evidence_append_only()
RETURNS trigger AS $$
DECLARE
    retention_authorized boolean := false;
BEGIN
    IF TG_OP = 'DELETE'
       AND to_regclass('pg_temp.context_evidence_retention_authority') IS NOT NULL THEN
        EXECUTE
            'SELECT EXISTS ('
            'SELECT 1 FROM pg_temp.context_evidence_retention_authority '
            'WHERE project_id = $1 AND owner_user_id = $2 AND thread_id = $3)'
            INTO retention_authorized
            USING OLD.project_id, OLD.owner_user_id, OLD.thread_id;
        IF retention_authorized THEN
            RETURN OLD;
        END IF;
    END IF;

    RAISE EXCEPTION 'Context Evidence is append-only outside exact retention purge'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_context_evidence_append_only BEFORE UPDATE OR DELETE ON context_evidence FOR EACH ROW EXECUTE FUNCTION enforce_context_evidence_append_only();



CREATE OR REPLACE FUNCTION enforce_stream_terminal_invariant()
RETURNS trigger AS $$
BEGIN
    -- Serialize every Thread's cross-partition invariant checks. The event
    -- store already holds this advisory lock, so normal writes are reentrant.
    PERFORM pg_advisory_xact_lock(hashtext(NEW.thread_id)::bigint);
    PERFORM 1
      FROM run_event_partition_state
     WHERE singleton
       AND (retained_from IS NULL OR NEW.created_at >= retained_from);
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run event timestamp precedes the retention watermark'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    INSERT INTO run_event_invariants (
        id, created_at, project_id, owner_user_id, thread_id, run_id, seq,
        is_stream_terminal
    ) VALUES (
        NEW.id, NEW.created_at, NEW.project_id, NEW.owner_user_id,
        NEW.thread_id, NEW.run_id, NEW.seq,
        NEW.category = 'stream' AND NEW.event_type = 'stream.end'
    );
    IF NEW.category = 'stream' THEN
        IF NEW.event_type <> 'stream.end' AND EXISTS (
            SELECT 1 FROM run_event_invariants
             WHERE project_id = NEW.project_id
               AND owner_user_id = NEW.owner_user_id
               AND thread_id = NEW.thread_id
               AND run_id = NEW.run_id
               AND is_stream_terminal
        ) THEN
            RAISE EXCEPTION 'stream event cannot follow terminal event'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;



CREATE OR REPLACE FUNCTION cleanup_run_event_invariant()
RETURNS trigger AS $$
BEGIN
    DELETE FROM run_event_invariants WHERE id = OLD.id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;



CREATE OR REPLACE FUNCTION enforce_run_event_identity_immutable()
RETURNS trigger AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.owner_user_id IS DISTINCT FROM OLD.owner_user_id
       OR NEW.thread_id IS DISTINCT FROM OLD.thread_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.seq IS DISTINCT FROM OLD.seq
       OR NEW.category IS DISTINCT FROM OLD.category
       OR NEW.event_type IS DISTINCT FROM OLD.event_type THEN
        RAISE EXCEPTION 'run event identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;



CREATE OR REPLACE FUNCTION ensure_run_events_month_partition(target_at TIMESTAMP WITH TIME ZONE)
RETURNS text AS $$
DECLARE
    month_start TIMESTAMP WITH TIME ZONE;
    month_end TIMESTAMP WITH TIME ZONE;
    partition_name text;
    retention_watermark TIMESTAMP WITH TIME ZONE;
    parent_table_comment text;
    parent_column record;
BEGIN
    IF target_at IS NULL THEN
        RAISE EXCEPTION 'run event partition timestamp is required'
            USING ERRCODE = 'not_null_violation';
    END IF;
    SELECT retained_from
      INTO retention_watermark
      FROM run_event_partition_state
     WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run event partition state is missing'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF retention_watermark IS NOT NULL AND target_at < retention_watermark THEN
        RAISE EXCEPTION 'run event timestamp precedes the retention watermark'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    month_start := date_trunc('month', target_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    month_end := month_start + INTERVAL '1 month';
    partition_name := 'run_events_p' || to_char(month_start AT TIME ZONE 'UTC', 'YYYYMM');
    IF to_regclass(partition_name) IS NOT NULL THEN
        RETURN partition_name;
    END IF;
    LOCK TABLE run_events IN ACCESS EXCLUSIVE MODE;
    SELECT retained_from
      INTO retention_watermark
      FROM run_event_partition_state
     WHERE singleton
       FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run event partition state is missing'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF retention_watermark IS NOT NULL AND target_at < retention_watermark THEN
        RAISE EXCEPTION 'run event timestamp precedes the retention watermark'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF to_regclass(partition_name) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF run_events FOR VALUES FROM (%L) TO (%L)',
            partition_name,
            month_start,
            month_end
        );
        parent_table_comment := obj_description('run_events'::regclass, 'pg_class');
        IF parent_table_comment IS NULL OR btrim(parent_table_comment) = '' THEN
            RAISE EXCEPTION 'run_events table comment is missing'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        EXECUTE format(
            'COMMENT ON TABLE %I IS %L',
            partition_name,
            parent_table_comment
        );
        FOR parent_column IN
            SELECT attribute.attname,
                   col_description(attribute.attrelid, attribute.attnum) AS description
              FROM pg_attribute attribute
             WHERE attribute.attrelid = 'run_events'::regclass
               AND attribute.attnum > 0
               AND NOT attribute.attisdropped
             ORDER BY attribute.attnum
        LOOP
            IF parent_column.description IS NULL
               OR btrim(parent_column.description) = '' THEN
                RAISE EXCEPTION 'run_events column comment is missing'
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
            EXECUTE format(
                'COMMENT ON COLUMN %I.%I IS %L',
                partition_name,
                parent_column.attname,
                parent_column.description
            );
        END LOOP;
    END IF;
    RETURN partition_name;
END;
$$ LANGUAGE plpgsql;



CREATE OR REPLACE FUNCTION drop_run_event_partitions_before(cutoff_at TIMESTAMP WITH TIME ZONE)
RETURNS integer AS $$
DECLARE
    keep_from TIMESTAMP WITH TIME ZONE;
    retention_watermark TIMESTAMP WITH TIME ZONE;
    month_key text;
    month_start TIMESTAMP WITH TIME ZONE;
    month_end TIMESTAMP WITH TIME ZONE;
    partition_name text;
    dropped integer := 0;
BEGIN
    IF cutoff_at IS NULL THEN
        RAISE EXCEPTION 'run event retention cutoff is required'
            USING ERRCODE = 'not_null_violation';
    END IF;
    IF NOT isfinite(cutoff_at) OR cutoff_at > clock_timestamp() THEN
        RAISE EXCEPTION 'run event retention cutoff cannot be in the future'
            USING ERRCODE = 'invalid_parameter_value';
    END IF;
    keep_from := date_trunc('month', cutoff_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    LOCK TABLE run_events IN ACCESS EXCLUSIVE MODE;
    SELECT retained_from
      INTO retention_watermark
      FROM run_event_partition_state
     WHERE singleton
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'run event partition state is missing'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF retention_watermark IS NULL OR keep_from > retention_watermark THEN
        retention_watermark := keep_from;
    END IF;
    UPDATE run_event_partition_state
       SET retained_from = retention_watermark,
           updated_at = now()
     WHERE singleton;
    FOR partition_name IN
        SELECT child.relname
          FROM pg_inherits inheritance
          JOIN pg_class parent ON parent.oid = inheritance.inhparent
          JOIN pg_class child ON child.oid = inheritance.inhrelid
          JOIN pg_namespace namespace ON namespace.oid = child.relnamespace
         WHERE parent.oid = 'run_events'::regclass
           AND namespace.nspname = current_schema()
           AND child.relname ~ '^run_events_p[0-9]{6}$'
         ORDER BY child.relname
    LOOP
        month_key := substring(partition_name FROM '^run_events_p([0-9]{6})$');
        month_start := to_date(month_key, 'YYYYMM')::timestamp AT TIME ZONE 'UTC';
        month_end := month_start + INTERVAL '1 month';
        IF month_end <= retention_watermark THEN
            EXECUTE format('DROP TABLE %I', partition_name);
            DELETE FROM run_event_invariants
             WHERE created_at >= month_start AND created_at < month_end;
            dropped := dropped + 1;
        END IF;
    END LOOP;
    PERFORM ensure_run_events_month_partition(now());
    PERFORM ensure_run_events_month_partition(now() + INTERVAL '1 month');
    RETURN dropped;
END;
$$ LANGUAGE plpgsql;



CREATE TRIGGER trg_run_events_stream_terminal BEFORE INSERT ON run_events FOR EACH ROW EXECUTE FUNCTION enforce_stream_terminal_invariant();

CREATE TRIGGER trg_run_events_identity_immutable BEFORE UPDATE OF id, created_at, project_id, owner_user_id, thread_id, run_id, seq, category, event_type ON run_events FOR EACH ROW EXECUTE FUNCTION enforce_run_event_identity_immutable();

CREATE TRIGGER trg_run_events_invariant_cleanup AFTER DELETE ON run_events FOR EACH ROW EXECUTE FUNCTION cleanup_run_event_invariant();



CREATE OR REPLACE FUNCTION set_schema_v1_updated_at()
RETURNS trigger AS $$
BEGIN
    IF NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at THEN
        NEW.updated_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION set_threads_meta_updated_at()
RETURNS trigger AS $$
BEGIN
    IF NEW.memory_sealed_at IS DISTINCT FROM OLD.memory_sealed_at
       AND NEW.updated_at IS NOT DISTINCT FROM OLD.updated_at
       AND (to_jsonb(NEW) - 'memory_sealed_at' - 'updated_at')
           IS NOT DISTINCT FROM
           (to_jsonb(OLD) - 'memory_sealed_at' - 'updated_at') THEN
        NEW.updated_at := OLD.updated_at;
    ELSE
        NEW.updated_at := clock_timestamp();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;



CREATE TRIGGER trg_asset_catalog_state_updated_at BEFORE UPDATE ON asset_catalog_state FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_invitation_rate_limits_updated_at BEFORE UPDATE ON project_invitation_rate_limits FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_run_event_partition_state_updated_at BEFORE UPDATE ON run_event_partition_state FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_runs_updated_at BEFORE UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_scheduled_task_runs_updated_at BEFORE UPDATE ON scheduled_task_runs FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_system_model_catalog_state_updated_at BEFORE UPDATE ON system_model_catalog_state FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_system_model_configs_updated_at BEFORE UPDATE ON system_model_configs FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_system_runtime_policies_updated_at BEFORE UPDATE ON system_runtime_policies FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_system_runtime_policy_catalog_state_updated_at BEFORE UPDATE ON system_runtime_policy_catalog_state FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_agents_updated_at BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_mcp_servers_updated_at BEFORE UPDATE ON mcp_servers FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_channel_instances_updated_at BEFORE UPDATE ON project_channel_instances FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_memberships_updated_at BEFORE UPDATE ON project_memberships FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_quotas_updated_at BEFORE UPDATE ON project_quotas FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_usage_counters_updated_at BEFORE UPDATE ON project_usage_counters FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_skills_updated_at BEFORE UPDATE ON skills FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_channel_connections_updated_at BEFORE UPDATE ON channel_connections FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_memory_documents_updated_at BEFORE UPDATE ON memory_documents FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_channel_group_bindings_updated_at BEFORE UPDATE ON project_channel_group_bindings FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_channel_instance_leases_updated_at BEFORE UPDATE ON project_channel_instance_leases FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_channel_secret_states_updated_at BEFORE UPDATE ON project_channel_secret_states FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_default_agents_updated_at BEFORE UPDATE ON project_default_agents FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_system_agent_bindings_updated_at BEFORE UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_system_skill_bindings_updated_at BEFORE UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_threads_meta_updated_at BEFORE UPDATE ON threads_meta FOR EACH ROW EXECUTE FUNCTION set_threads_meta_updated_at();

CREATE TRIGGER trg_agent_design_sessions_updated_at BEFORE UPDATE ON agent_design_sessions FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_channel_conversations_updated_at BEFORE UPDATE ON channel_conversations FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_channel_credentials_updated_at BEFORE UPDATE ON channel_credentials FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_channel_external_principals_updated_at BEFORE UPDATE ON channel_external_principals FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_execution_approval_requests_updated_at BEFORE UPDATE ON execution_approval_requests FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_files_updated_at BEFORE UPDATE ON files FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_skill_secret_states_updated_at BEFORE UPDATE ON project_skill_secret_states FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_system_mcp_bindings_updated_at BEFORE UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_scheduled_tasks_updated_at BEFORE UPDATE ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_skill_design_sessions_updated_at BEFORE UPDATE ON skill_design_sessions FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_agent_design_operations_updated_at BEFORE UPDATE ON agent_design_operations FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_memory_dream_prepare_runs_updated_at BEFORE UPDATE ON memory_dream_prepare_runs FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_project_mcp_secret_states_updated_at BEFORE UPDATE ON project_mcp_secret_states FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_skill_design_draft_files_updated_at BEFORE UPDATE ON skill_design_draft_files FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_skill_design_operations_updated_at BEFORE UPDATE ON skill_design_operations FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_execution_approval_output_delivery_obligations_updated_at BEFORE UPDATE ON execution_approval_output_delivery_obligations FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_context_evidence_sequences_updated_at BEFORE UPDATE ON context_evidence_sequences FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_context_projection_heads_updated_at BEFORE UPDATE ON context_projection_heads FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE OR REPLACE FUNCTION reject_direct_run_model_snapshot_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'run model snapshots cannot be updated or directly deleted'
            USING ERRCODE = '55000';
    END IF;

    PERFORM 1
      FROM runs
     WHERE project_id = OLD.project_id
       AND owner_user_id = OLD.owner_user_id
       AND thread_id = OLD.thread_id
       AND run_id = OLD.run_id;

    IF FOUND THEN
        RAISE EXCEPTION 'run model snapshots cannot be updated or directly deleted'
            USING ERRCODE = '55000';
    END IF;

    -- The parent row is already absent only while PostgreSQL is executing the
    -- FK-owned ON DELETE CASCADE from an unreferenced Run retention delete.
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_run_model_config_snapshots_immutable BEFORE UPDATE OR DELETE ON run_model_config_snapshots FOR EACH ROW EXECUTE FUNCTION reject_direct_run_model_snapshot_mutation();



CREATE OR REPLACE FUNCTION reject_direct_run_runtime_policy_snapshot_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'run runtime policy snapshots cannot be updated or directly deleted'
            USING ERRCODE = '55000';
    END IF;

    PERFORM 1
      FROM runs
     WHERE project_id = OLD.project_id
       AND owner_user_id = OLD.owner_user_id
       AND thread_id = OLD.thread_id
       AND run_id = OLD.run_id;

    IF FOUND THEN
        RAISE EXCEPTION 'run runtime policy snapshots cannot be updated or directly deleted'
            USING ERRCODE = '55000';
    END IF;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_run_runtime_policy_snapshots_immutable BEFORE UPDATE OR DELETE ON run_runtime_policy_snapshots FOR EACH ROW EXECUTE FUNCTION reject_direct_run_runtime_policy_snapshot_mutation();



CREATE OR REPLACE FUNCTION prevent_memory_document_sections_mutation()
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_memory_documents_sections_immutable BEFORE UPDATE ON memory_documents FOR EACH ROW EXECUTE FUNCTION prevent_memory_document_sections_mutation();



CREATE OR REPLACE FUNCTION prevent_run_memory_snapshot_sections_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.sections IS DISTINCT FROM OLD.sections THEN
        RAISE EXCEPTION 'Run Memory snapshot sections are immutable'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_run_memory_context_snapshots_sections_immutable BEFORE UPDATE ON run_memory_context_snapshots FOR EACH ROW EXECUTE FUNCTION prevent_run_memory_snapshot_sections_mutation();



-- Host-owned retrieval model registry: one OpenAI-compatible endpoint per
-- Provider row plus its encrypted API Key, and one typed embedding or rerank
-- model per model row. Knowledge Bases below bind the model rows by UUID.
CREATE TABLE model_providers (
    id UUID NOT NULL,
    name VARCHAR(120) NOT NULL,
    base_url VARCHAR(1024) NOT NULL,
    request_timeout_seconds INTEGER DEFAULT 30 NOT NULL,
    api_key_nonce BYTEA NOT NULL,
    api_key_ciphertext BYTEA NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_model_providers_name CHECK (btrim(name) <> ''),
    CONSTRAINT ck_model_providers_base_url CHECK (btrim(base_url) <> ''),
    CONSTRAINT ck_model_providers_timeout CHECK (request_timeout_seconds BETWEEN 1 AND 300),
    CONSTRAINT ck_model_providers_secret CHECK (
        octet_length(api_key_nonce) = 12 AND octet_length(api_key_ciphertext) >= 16
    )
);

CREATE UNIQUE INDEX uq_model_providers_name ON model_providers (lower(name)) WHERE deleted_at IS NULL;

-- system_model_configs is created earlier in the snapshot, so its required
-- provider binding is added after the model_providers table exists.
ALTER TABLE system_model_configs ADD CONSTRAINT fk_system_model_configs_provider FOREIGN KEY(provider_id) REFERENCES model_providers (id) ON DELETE RESTRICT;

CREATE TABLE model_provider_models (
    id UUID NOT NULL,
    provider_id UUID NOT NULL,
    model_type VARCHAR(16) NOT NULL,
    model_name VARCHAR(255) NOT NULL,
    embedding_dimension INTEGER,
    max_batch INTEGER NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    deleted_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_model_provider_models_type CHECK (model_type IN ('embedding', 'rerank')),
    CONSTRAINT ck_model_provider_models_model_name CHECK (btrim(model_name) <> ''),
    CONSTRAINT ck_model_provider_models_dimension CHECK (
        (model_type = 'embedding') = (embedding_dimension IS NOT NULL)
        AND (embedding_dimension IS NULL OR embedding_dimension BETWEEN 1 AND 16000)
    ),
    CONSTRAINT ck_model_provider_models_max_batch CHECK (
        (model_type = 'embedding' AND max_batch BETWEEN 1 AND 2048)
        OR (model_type = 'rerank' AND max_batch BETWEEN 1 AND 256)
    ),
    CONSTRAINT ck_model_provider_models_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_model_provider_models_deleted_state CHECK (deleted_at IS NULL OR status = 'disabled'),
    FOREIGN KEY(provider_id) REFERENCES model_providers (id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_model_provider_models_identity ON model_provider_models (provider_id, model_type, model_name) WHERE deleted_at IS NULL;

CREATE TRIGGER trg_model_providers_updated_at BEFORE UPDATE ON model_providers FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

CREATE TRIGGER trg_model_provider_models_updated_at BEFORE UPDATE ON model_provider_models FOR EACH ROW EXECUTE FUNCTION set_schema_v1_updated_at();

-- Knowledge Package tables (ActWeave Knowledge). pgvector is an administrator
-- preparation step performed by setup/reset with maintenance authority; this
-- snapshot only refuses to install without the extension type present.
DO $$
BEGIN
    IF to_regtype('public.vector') IS NULL THEN
        RAISE EXCEPTION 'SCHEMA_RECREATE_REQUIRED: public.vector extension type is missing';
    END IF;
END
$$;

CREATE TABLE knowledge_bases (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    name VARCHAR(120) NOT NULL,
    description VARCHAR(500) DEFAULT '' NOT NULL,
    embedding_model_id UUID,
    reranker_model_id UUID,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    retrieval_mode VARCHAR(16) DEFAULT 'semantic' NOT NULL,
    default_top_k INTEGER DEFAULT 4 NOT NULL,
    default_score_threshold DOUBLE PRECISION DEFAULT 0.2 NOT NULL,
    summary_index_enabled BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    default_relative_cutoff DOUBLE PRECISION,
    CONSTRAINT pk_knowledge_bases PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_bases_project_id_id UNIQUE (project_id, id),
    CONSTRAINT ck_knowledge_bases_name CHECK (btrim(name) <> ''),
    CONSTRAINT ck_knowledge_bases_status CHECK (status IN ('active', 'disabled', 'deleting')),
    CONSTRAINT ck_knowledge_bases_retrieval_mode CHECK (retrieval_mode IN ('semantic', 'hybrid')),
    CONSTRAINT ck_knowledge_bases_default_top_k CHECK (default_top_k BETWEEN 1 AND 20),
    CONSTRAINT ck_knowledge_bases_default_score_threshold CHECK (
        default_score_threshold >= 0 AND default_score_threshold <= 1
    ),
    CONSTRAINT ck_knowledge_bases_default_relative_cutoff CHECK (
        default_relative_cutoff IS NULL OR (default_relative_cutoff > 0 AND default_relative_cutoff <= 1)
    ),
    CONSTRAINT fk_knowledge_bases_project FOREIGN KEY (project_id)
        REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_bases_embedding_model FOREIGN KEY (embedding_model_id)
        REFERENCES model_provider_models (id) ON DELETE RESTRICT,
    CONSTRAINT fk_knowledge_bases_reranker_model FOREIGN KEY (reranker_model_id)
        REFERENCES model_provider_models (id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_knowledge_bases_project_name
    ON knowledge_bases (project_id, lower(name));

CREATE INDEX ix_knowledge_bases_project_status
    ON knowledge_bases (project_id, status, updated_at DESC, id);

CREATE INDEX ix_knowledge_bases_embedding_model
    ON knowledge_bases (embedding_model_id);

CREATE INDEX ix_knowledge_bases_reranker_model
    ON knowledge_bases (reranker_model_id);

CREATE TABLE knowledge_documents (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    name VARCHAR(255) NOT NULL,
    original_name VARCHAR(255) NOT NULL,
    storage_key VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255),
    size_bytes BIGINT NOT NULL,
    status VARCHAR(16) DEFAULT 'uploading' NOT NULL,
    enabled BOOLEAN DEFAULT true NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    published_version INTEGER,
    chunk_size INTEGER DEFAULT 1000 NOT NULL,
    chunk_overlap INTEGER DEFAULT 100 NOT NULL,
    chunk_separator VARCHAR(64) DEFAULT '\n\n' NOT NULL,
    remove_extra_spaces BOOLEAN DEFAULT false NOT NULL,
    remove_urls_emails BOOLEAN DEFAULT false NOT NULL,
    chunking_mode VARCHAR(16) DEFAULT 'general' NOT NULL,
    child_chunk_size INTEGER DEFAULT 500 NOT NULL,
    child_chunk_separator VARCHAR(64) DEFAULT '\n' NOT NULL,
    segment_count INTEGER DEFAULT 0 NOT NULL,
    word_count BIGINT DEFAULT 0 NOT NULL,
    hit_count BIGINT DEFAULT 0 NOT NULL,
    doc_metadata JSONB DEFAULT '{}'::jsonb NOT NULL,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source_sha256 VARCHAR(64),
    published_extraction_id UUID,
    parsing_profile JSONB,
    parse_warnings JSONB DEFAULT '[]'::jsonb NOT NULL,
    capability_revision VARCHAR(64),
    upload_state VARCHAR(16) DEFAULT 'pending' NOT NULL,
    quota_state VARCHAR(16) DEFAULT 'unreserved' NOT NULL,
    CONSTRAINT pk_knowledge_documents PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_documents_project_base_id UNIQUE (project_id, knowledge_base_id, id),
    CONSTRAINT ck_knowledge_documents_name CHECK (btrim(name) <> '' AND btrim(original_name) <> ''),
    CONSTRAINT ck_knowledge_documents_storage_key CHECK (btrim(storage_key) <> ''),
    CONSTRAINT ck_knowledge_documents_size CHECK (size_bytes BETWEEN 0 AND 52428800),
    CONSTRAINT ck_knowledge_documents_status CHECK (
        status IN ('uploading', 'queued', 'processing', 'ready', 'failed', 'deleting')
    ),
    CONSTRAINT ck_knowledge_documents_version CHECK (version >= 1),
    CONSTRAINT ck_knowledge_documents_published_version CHECK (
        published_version IS NULL OR (published_version >= 1 AND published_version <= version)
    ),
    CONSTRAINT ck_knowledge_documents_chunk_size CHECK (chunk_size BETWEEN 200 AND 4000),
    CONSTRAINT ck_knowledge_documents_chunk_overlap CHECK (
        chunk_overlap BETWEEN 0 AND 500 AND chunk_overlap < chunk_size
    ),
    CONSTRAINT ck_knowledge_documents_chunk_separator CHECK (
        char_length(chunk_separator) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_knowledge_documents_chunking_mode CHECK (
        chunking_mode IN ('general', 'parent_child')
    ),
    CONSTRAINT ck_knowledge_documents_child_chunk_size CHECK (
        child_chunk_size BETWEEN 100 AND 2000
    ),
    CONSTRAINT ck_knowledge_documents_child_chunk_ratio CHECK (
        chunking_mode = 'general' OR child_chunk_size < chunk_size
    ),
    CONSTRAINT ck_knowledge_documents_child_chunk_separator CHECK (
        char_length(child_chunk_separator) BETWEEN 1 AND 64
    ),
    CONSTRAINT ck_knowledge_documents_segment_count CHECK (segment_count >= 0),
    CONSTRAINT ck_knowledge_documents_word_count CHECK (word_count >= 0),
    CONSTRAINT ck_knowledge_documents_hit_count CHECK (hit_count >= 0),
    CONSTRAINT ck_knowledge_documents_doc_metadata CHECK (jsonb_typeof(doc_metadata) = 'object'),
    CONSTRAINT ck_knowledge_documents_error CHECK (
        (status = 'failed' AND error_message IS NOT NULL)
        OR (status <> 'failed' AND error_message IS NULL)
    ),
    CONSTRAINT fk_knowledge_documents_base FOREIGN KEY (project_id, knowledge_base_id)
        REFERENCES knowledge_bases (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_knowledge_documents_parse_warnings CHECK (jsonb_typeof(parse_warnings) = 'array'),
    CONSTRAINT ck_knowledge_documents_parsing_profile CHECK (parsing_profile IS NULL OR jsonb_typeof(parsing_profile) = 'object'),
    CONSTRAINT ck_knowledge_documents_quota_released CHECK (quota_state <> 'released' OR upload_state = 'deleted'),
    CONSTRAINT ck_knowledge_documents_quota_state CHECK (quota_state IN ('unreserved', 'reserved', 'committed', 'released')),
    CONSTRAINT ck_knowledge_documents_source_sha256 CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_knowledge_documents_upload_state CHECK (upload_state IN ('pending', 'stored', 'delete_pending', 'deleted')),
    CONSTRAINT uq_knowledge_documents_published_extraction UNIQUE (project_id, knowledge_base_id, id, published_extraction_id)
);

CREATE UNIQUE INDEX uq_knowledge_documents_storage_key
    ON knowledge_documents (storage_key);

CREATE INDEX ix_knowledge_documents_base_status
    ON knowledge_documents (project_id, knowledge_base_id, status, updated_at DESC, id);

-- Accelerates the search-path metadata equality filter (doc_metadata @> {...}).
CREATE INDEX ix_knowledge_documents_doc_metadata
    ON knowledge_documents USING gin (doc_metadata);

CREATE TABLE knowledge_extractions (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    source_sha256 VARCHAR(64) NOT NULL,
    parser_fingerprint VARCHAR(64) NOT NULL,
    normalization_version VARCHAR(64) NOT NULL,
    state VARCHAR(16) DEFAULT 'staging' NOT NULL,
    manifest_storage_key VARCHAR(1024),
    manifest_sha256 VARCHAR(64),
    manifest_size_bytes BIGINT DEFAULT 0 NOT NULL,
    manifest_upload_state VARCHAR(16) DEFAULT 'pending' NOT NULL,
    manifest_quota_state VARCHAR(16) DEFAULT 'unreserved' NOT NULL,
    created_task_id UUID NOT NULL,
    created_attempt SMALLINT NOT NULL,
    created_claim_token UUID NOT NULL,
    target_document_version INTEGER NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE,
    unpublished_expires_at TIMESTAMP WITH TIME ZONE,
    delete_error TEXT,
    CONSTRAINT pk_knowledge_extractions PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_extractions_project_id UNIQUE (project_id, id),
    CONSTRAINT uq_knowledge_extractions_scope UNIQUE (project_id, knowledge_base_id, knowledge_document_id, id),
    CONSTRAINT uq_knowledge_extractions_creation_attempt UNIQUE (knowledge_document_id, created_task_id, created_attempt, created_claim_token),
    CONSTRAINT fk_knowledge_extractions_document FOREIGN KEY(project_id, knowledge_base_id, knowledge_document_id) REFERENCES knowledge_documents (project_id, knowledge_base_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_knowledge_extractions_source_sha256 CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_knowledge_extractions_parser_fingerprint CHECK (parser_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_knowledge_extractions_normalization_version CHECK (btrim(normalization_version) <> ''),
    CONSTRAINT ck_knowledge_extractions_state CHECK (state IN ('staging', 'ready', 'deleting')),
    CONSTRAINT ck_knowledge_extractions_manifest_sha256 CHECK (manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_knowledge_extractions_manifest_size CHECK (manifest_size_bytes BETWEEN 0 AND 52428800),
    CONSTRAINT ck_knowledge_extractions_manifest_registration CHECK ((manifest_storage_key IS NULL AND manifest_sha256 IS NULL AND manifest_size_bytes = 0 AND manifest_quota_state = 'unreserved') OR (manifest_storage_key IS NOT NULL AND btrim(manifest_storage_key) <> '' AND manifest_sha256 IS NOT NULL)),
    CONSTRAINT ck_knowledge_extractions_upload_state CHECK (manifest_upload_state IN ('pending', 'stored', 'delete_pending', 'deleted')),
    CONSTRAINT ck_knowledge_extractions_quota_state CHECK (manifest_quota_state IN ('unreserved', 'reserved', 'committed', 'released')),
    CONSTRAINT ck_knowledge_extractions_stored_manifest CHECK (manifest_upload_state NOT IN ('stored', 'delete_pending') OR manifest_storage_key IS NOT NULL),
    CONSTRAINT ck_knowledge_extractions_quota_released CHECK (manifest_quota_state <> 'released' OR manifest_upload_state = 'deleted'),
    CONSTRAINT ck_knowledge_extractions_ready CHECK (state <> 'ready' OR (manifest_upload_state = 'stored' AND manifest_quota_state = 'committed' AND completed_at IS NOT NULL)),
    CONSTRAINT ck_knowledge_extractions_created_attempt CHECK (created_attempt BETWEEN 1 AND 3),
    CONSTRAINT ck_knowledge_extractions_target_version CHECK (target_document_version >= 1)
);

CREATE INDEX ix_knowledge_extractions_document ON knowledge_extractions (project_id, knowledge_base_id, knowledge_document_id, state);
CREATE INDEX ix_knowledge_extractions_unpublished_expires ON knowledge_extractions (unpublished_expires_at, id);
CREATE UNIQUE INDEX uq_knowledge_extractions_manifest_key ON knowledge_extractions (manifest_storage_key);

CREATE TABLE knowledge_attachments (
    id UUID NOT NULL,
    extraction_id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    media_type VARCHAR(32) NOT NULL,
    size_bytes BIGINT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    storage_key VARCHAR(1024) NOT NULL,
    state VARCHAR(16) DEFAULT 'staging' NOT NULL,
    upload_state VARCHAR(16) DEFAULT 'pending' NOT NULL,
    quota_state VARCHAR(16) DEFAULT 'unreserved' NOT NULL,
    delete_error TEXT,
    CONSTRAINT pk_knowledge_attachments PRIMARY KEY (id),
    CONSTRAINT fk_knowledge_attachments_extraction FOREIGN KEY(project_id, knowledge_base_id, knowledge_document_id, extraction_id) REFERENCES knowledge_extractions (project_id, knowledge_base_id, knowledge_document_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_knowledge_attachments_hash UNIQUE (extraction_id, sha256),
    CONSTRAINT uq_knowledge_attachments_scope UNIQUE (project_id, knowledge_base_id, knowledge_document_id, extraction_id, id),
    CONSTRAINT ck_knowledge_attachments_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_knowledge_attachments_media_type CHECK (media_type IN ('image/png', 'image/jpeg', 'image/webp')),
    CONSTRAINT ck_knowledge_attachments_size CHECK (size_bytes BETWEEN 0 AND 5242880),
    CONSTRAINT ck_knowledge_attachments_pixels CHECK (width > 0 AND height > 0 AND width::bigint * height::bigint <= 20000000),
    CONSTRAINT ck_knowledge_attachments_storage_key CHECK (btrim(storage_key) <> ''),
    CONSTRAINT ck_knowledge_attachments_state CHECK (state IN ('staging', 'ready', 'deleting')),
    CONSTRAINT ck_knowledge_attachments_upload_state CHECK (upload_state IN ('pending', 'stored', 'delete_pending', 'deleted')),
    CONSTRAINT ck_knowledge_attachments_quota_state CHECK (quota_state IN ('unreserved', 'reserved', 'committed', 'released')),
    CONSTRAINT ck_knowledge_attachments_quota_released CHECK (quota_state <> 'released' OR upload_state = 'deleted'),
    CONSTRAINT ck_knowledge_attachments_ready CHECK (state <> 'ready' OR (upload_state = 'stored' AND quota_state = 'committed'))
);

CREATE UNIQUE INDEX uq_knowledge_attachments_storage_key ON knowledge_attachments (storage_key);

-- Forward reference completes the document/extraction ownership cycle.
ALTER TABLE knowledge_documents ADD CONSTRAINT fk_knowledge_documents_published_extraction FOREIGN KEY(project_id, knowledge_base_id, id, published_extraction_id) REFERENCES knowledge_extractions (project_id, knowledge_base_id, knowledge_document_id, id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE knowledge_metadata_fields (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    name VARCHAR(64) NOT NULL,
    field_type VARCHAR(16) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_metadata_fields PRIMARY KEY (id),
    CONSTRAINT ck_knowledge_metadata_fields_name CHECK (btrim(name) <> ''),
    CONSTRAINT ck_knowledge_metadata_fields_type CHECK (field_type IN ('string', 'number', 'time')),
    CONSTRAINT fk_knowledge_metadata_fields_base FOREIGN KEY (project_id, knowledge_base_id)
        REFERENCES knowledge_bases (project_id, id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX uq_knowledge_metadata_fields_base_name
    ON knowledge_metadata_fields (knowledge_base_id, lower(name));

CREATE TABLE knowledge_segments (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    word_count INTEGER DEFAULT 0 NOT NULL,
    enabled BOOLEAN DEFAULT true NOT NULL,
    hit_count INTEGER DEFAULT 0 NOT NULL,
    source_position JSONB DEFAULT '{}'::jsonb NOT NULL,
    embedding public.vector,
    lexical_tsv TSVECTOR DEFAULT to_tsvector('simple', '') NOT NULL,
    lexical_version INTEGER DEFAULT 0 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    index_text TEXT DEFAULT '' NOT NULL,
    token_count INTEGER DEFAULT 0 NOT NULL,
    source_spans JSONB DEFAULT '[]'::jsonb NOT NULL,
    extraction_id UUID,
    CONSTRAINT pk_knowledge_segments PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_segments_document_version_position UNIQUE (
        knowledge_document_id,
        document_version,
        position
    ),
    CONSTRAINT ck_knowledge_segments_version CHECK (document_version >= 1),
    CONSTRAINT ck_knowledge_segments_position CHECK (position >= 1),
    CONSTRAINT ck_knowledge_segments_content CHECK (content <> ''),
    CONSTRAINT ck_knowledge_segments_word_count CHECK (word_count >= 0),
    CONSTRAINT ck_knowledge_segments_hit_count CHECK (hit_count >= 0),
    CONSTRAINT ck_knowledge_segments_source_position CHECK (jsonb_typeof(source_position) = 'object'),
    CONSTRAINT ck_knowledge_segments_lexical_version CHECK (lexical_version >= 0),
    CONSTRAINT ck_knowledge_segments_embedding CHECK (
        embedding IS NULL OR public.vector_dims(embedding) BETWEEN 1 AND 16000
    ),
    CONSTRAINT fk_knowledge_segments_document FOREIGN KEY (
        project_id,
        knowledge_base_id,
        knowledge_document_id
    ) REFERENCES knowledge_documents (
        project_id,
        knowledge_base_id,
        id
    ) ON DELETE CASCADE,
    CONSTRAINT ck_knowledge_segments_source_spans CHECK (jsonb_typeof(source_spans) = 'array'),
    CONSTRAINT ck_knowledge_segments_token_count CHECK (token_count >= 0),
    CONSTRAINT fk_knowledge_segments_published_extraction FOREIGN KEY(project_id, knowledge_base_id, knowledge_document_id, extraction_id) REFERENCES knowledge_documents (project_id, knowledge_base_id, id, published_extraction_id) DEFERRABLE INITIALLY DEFERRED,
    CONSTRAINT uq_knowledge_segments_extraction_scope UNIQUE (project_id, knowledge_base_id, knowledge_document_id, extraction_id, id)
);

CREATE INDEX ix_knowledge_segments_document
    ON knowledge_segments (
        project_id,
        knowledge_base_id,
        knowledge_document_id,
        document_version,
        position
    );

-- Lexical route (design §8): GIN over the lexical_v1 derived tokens.
CREATE INDEX ix_knowledge_segments_lexical
    ON knowledge_segments USING gin (lexical_tsv);

-- Semantic route: the embedding column carries any dimension, so pgvector
-- HNSW is built per common dimension as a partial expression index. Recall
-- orders each base's lateral branch by ``embedding::vector(D) <=> query``
-- under ``vector_dims(embedding) = D``; a dimension outside this list still
-- works as a sorted scan. HNSW on ``vector`` supports at most 2000 dims.
CREATE INDEX ix_knowledge_segments_embedding_hnsw_384
    ON knowledge_segments USING hnsw ((embedding::public.vector(384)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 384;
CREATE INDEX ix_knowledge_segments_embedding_hnsw_512
    ON knowledge_segments USING hnsw ((embedding::public.vector(512)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 512;
CREATE INDEX ix_knowledge_segments_embedding_hnsw_768
    ON knowledge_segments USING hnsw ((embedding::public.vector(768)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 768;
CREATE INDEX ix_knowledge_segments_embedding_hnsw_1024
    ON knowledge_segments USING hnsw ((embedding::public.vector(1024)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 1024;
CREATE INDEX ix_knowledge_segments_embedding_hnsw_1536
    ON knowledge_segments USING hnsw ((embedding::public.vector(1536)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 1536;

CREATE TABLE knowledge_segment_attachments (
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    extraction_id UUID NOT NULL,
    segment_id UUID NOT NULL,
    attachment_id UUID NOT NULL,
    position INTEGER NOT NULL,
    alt_text TEXT DEFAULT '' NOT NULL,
    CONSTRAINT pk_knowledge_segment_attachments PRIMARY KEY (segment_id, position),
    CONSTRAINT fk_knowledge_segment_attachments_segment FOREIGN KEY(project_id, knowledge_base_id, knowledge_document_id, extraction_id, segment_id) REFERENCES knowledge_segments (project_id, knowledge_base_id, knowledge_document_id, extraction_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_knowledge_segment_attachments_attachment FOREIGN KEY(project_id, knowledge_base_id, knowledge_document_id, extraction_id, attachment_id) REFERENCES knowledge_attachments (project_id, knowledge_base_id, knowledge_document_id, extraction_id, id) ON DELETE RESTRICT,
    CONSTRAINT ck_knowledge_segment_attachments_position CHECK (position >= 1)
);

CREATE INDEX ix_knowledge_segment_attachments_attachment ON knowledge_segment_attachments (attachment_id);

CREATE TABLE knowledge_segment_children (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    knowledge_segment_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    position INTEGER NOT NULL,
    content TEXT NOT NULL,
    word_count INTEGER DEFAULT 0 NOT NULL,
    embedding public.vector NOT NULL,
    lexical_tsv TSVECTOR DEFAULT to_tsvector('simple', '') NOT NULL,
    lexical_version INTEGER DEFAULT 0 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    index_text TEXT DEFAULT '' NOT NULL,
    token_count INTEGER DEFAULT 0 NOT NULL,
    source_spans JSONB DEFAULT '[]'::jsonb NOT NULL,
    CONSTRAINT pk_knowledge_segment_children PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_segment_children_segment_position UNIQUE (
        knowledge_segment_id,
        position
    ),
    CONSTRAINT ck_knowledge_segment_children_version CHECK (document_version >= 1),
    CONSTRAINT ck_knowledge_segment_children_position CHECK (position >= 1),
    CONSTRAINT ck_knowledge_segment_children_content CHECK (content <> ''),
    CONSTRAINT ck_knowledge_segment_children_word_count CHECK (word_count >= 0),
    CONSTRAINT ck_knowledge_segment_children_lexical_version CHECK (lexical_version >= 0),
    CONSTRAINT ck_knowledge_segment_children_embedding CHECK (
        public.vector_dims(embedding) BETWEEN 1 AND 16000
    ),
    CONSTRAINT fk_knowledge_segment_children_segment FOREIGN KEY (knowledge_segment_id)
        REFERENCES knowledge_segments (id) ON DELETE CASCADE,
    CONSTRAINT ck_knowledge_segment_children_source_spans CHECK (jsonb_typeof(source_spans) = 'array'),
    CONSTRAINT ck_knowledge_segment_children_token_count CHECK (token_count >= 0)
);

CREATE INDEX ix_knowledge_segment_children_document
    ON knowledge_segment_children (
        project_id,
        knowledge_base_id,
        knowledge_document_id,
        document_version,
        position
    );

CREATE INDEX ix_knowledge_segment_children_lexical
    ON knowledge_segment_children USING gin (lexical_tsv);

-- Per-dimension HNSW (see knowledge_segments): parent_child recall orders
-- each base's nearest children through these before rolling up to parents.
CREATE INDEX ix_knowledge_segment_children_embedding_hnsw_384
    ON knowledge_segment_children USING hnsw ((embedding::public.vector(384)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 384;
CREATE INDEX ix_knowledge_segment_children_embedding_hnsw_512
    ON knowledge_segment_children USING hnsw ((embedding::public.vector(512)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 512;
CREATE INDEX ix_knowledge_segment_children_embedding_hnsw_768
    ON knowledge_segment_children USING hnsw ((embedding::public.vector(768)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 768;
CREATE INDEX ix_knowledge_segment_children_embedding_hnsw_1024
    ON knowledge_segment_children USING hnsw ((embedding::public.vector(1024)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 1024;
CREATE INDEX ix_knowledge_segment_children_embedding_hnsw_1536
    ON knowledge_segment_children USING hnsw ((embedding::public.vector(1536)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 1536;

-- Segment summaries (M11 design §5): at most one complete, system-generated
-- summary per segment, embedded into the base's vector space as a recall
-- aid. Rows have no enabled switch of their own — visibility follows the
-- owning segment/document — and segment deletion cascades.
CREATE TABLE knowledge_segment_summaries (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    knowledge_base_id UUID NOT NULL,
    knowledge_document_id UUID NOT NULL,
    knowledge_segment_id UUID NOT NULL,
    document_version INTEGER NOT NULL,
    content TEXT NOT NULL,
    source_content_digest VARCHAR(64) NOT NULL,
    embedding public.vector NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_segment_summaries PRIMARY KEY (id),
    CONSTRAINT uq_knowledge_segment_summaries_segment UNIQUE (knowledge_segment_id),
    CONSTRAINT ck_knowledge_segment_summaries_version CHECK (document_version >= 1),
    CONSTRAINT ck_knowledge_segment_summaries_content CHECK (length(content) > 0),
    CONSTRAINT ck_knowledge_segment_summaries_embedding CHECK (
        public.vector_dims(embedding) BETWEEN 1 AND 16000
    ),
    CONSTRAINT fk_knowledge_segment_summaries_segment FOREIGN KEY (knowledge_segment_id)
        REFERENCES knowledge_segments (id) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_segment_summaries_scope
    ON knowledge_segment_summaries (project_id, knowledge_base_id);

CREATE INDEX ix_knowledge_segment_summaries_document
    ON knowledge_segment_summaries (knowledge_document_id);

-- Per-dimension HNSW (see knowledge_segments) for the summary recall route.
CREATE INDEX ix_knowledge_segment_summaries_embedding_hnsw_384
    ON knowledge_segment_summaries USING hnsw ((embedding::public.vector(384)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 384;
CREATE INDEX ix_knowledge_segment_summaries_embedding_hnsw_512
    ON knowledge_segment_summaries USING hnsw ((embedding::public.vector(512)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 512;
CREATE INDEX ix_knowledge_segment_summaries_embedding_hnsw_768
    ON knowledge_segment_summaries USING hnsw ((embedding::public.vector(768)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 768;
CREATE INDEX ix_knowledge_segment_summaries_embedding_hnsw_1024
    ON knowledge_segment_summaries USING hnsw ((embedding::public.vector(1024)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 1024;
CREATE INDEX ix_knowledge_segment_summaries_embedding_hnsw_1536
    ON knowledge_segment_summaries USING hnsw ((embedding::public.vector(1536)) public.vector_cosine_ops)
    WHERE public.vector_dims(embedding) = 1536;

CREATE TABLE knowledge_queries (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    knowledge_base_ids JSONB DEFAULT '[]'::jsonb NOT NULL,
    query VARCHAR(2000) NOT NULL,
    source VARCHAR(16) NOT NULL,
    result_count INTEGER DEFAULT 0 NOT NULL,
    top_score DOUBLE PRECISION,
    top_score_kind VARCHAR(16),
    strategy_version VARCHAR(32),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_knowledge_queries PRIMARY KEY (id),
    CONSTRAINT ck_knowledge_queries_query CHECK (btrim(query) <> ''),
    CONSTRAINT ck_knowledge_queries_source CHECK (source IN ('agent', 'retrieval_test')),
    CONSTRAINT ck_knowledge_queries_base_ids CHECK (jsonb_typeof(knowledge_base_ids) = 'array'),
    CONSTRAINT ck_knowledge_queries_result_count CHECK (result_count >= 0),
    CONSTRAINT ck_knowledge_queries_top_score CHECK (
        top_score IS NULL OR (top_score >= -1 AND top_score <= 1)
    ),
    CONSTRAINT ck_knowledge_queries_top_score_kind CHECK (
        top_score_kind IS NULL OR top_score_kind IN ('cosine', 'rerank', 'rank_fusion')
    ),
    CONSTRAINT ck_knowledge_queries_strategy_version CHECK (
        strategy_version IS NULL OR btrim(strategy_version) <> ''
    ),
    CONSTRAINT fk_knowledge_queries_project FOREIGN KEY (project_id)
        REFERENCES projects (id) ON DELETE CASCADE
);

CREATE INDEX ix_knowledge_queries_owner_created
    ON knowledge_queries (project_id, owner_user_id, created_at DESC, id);

CREATE TABLE knowledge_tasks (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    resource_id UUID NOT NULL,
    kind VARCHAR(32) NOT NULL,
    target_version INTEGER,
    storage_key VARCHAR(1024),
    reparse_settings JSONB,
    status VARCHAR(16) DEFAULT 'queued' NOT NULL,
    stage VARCHAR(32) DEFAULT 'queued' NOT NULL,
    completed_units INTEGER DEFAULT 0 NOT NULL,
    total_units INTEGER,
    progress_updated_at TIMESTAMP WITH TIME ZONE,
    attempt_count SMALLINT DEFAULT 0 NOT NULL,
    max_attempts SMALLINT DEFAULT 3 NOT NULL,
    available_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    claim_token UUID,
    lease_until TIMESTAMP WITH TIME ZONE,
    error_message TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    extraction_id UUID,
    CONSTRAINT pk_knowledge_tasks PRIMARY KEY (id),
    CONSTRAINT ck_knowledge_tasks_kind CHECK (
        kind IN ('ingest_document', 'reembed_document', 'summarize_document', 'relex_document', 'delete_document', 'delete_document_object', 'delete_knowledge_base', 'delete_extraction')
    ),
    CONSTRAINT ck_knowledge_tasks_target_version CHECK (
        (kind IN ('ingest_document', 'reembed_document', 'summarize_document', 'relex_document') AND target_version IS NOT NULL AND target_version >= 1)
        OR (kind NOT IN ('ingest_document', 'reembed_document', 'summarize_document', 'relex_document') AND target_version IS NULL)
    ),
    CONSTRAINT ck_knowledge_tasks_storage_key CHECK (
        (kind = 'delete_document_object' AND storage_key IS NOT NULL AND btrim(storage_key) <> '')
        OR (kind <> 'delete_document_object' AND storage_key IS NULL)
    ),
    CONSTRAINT ck_knowledge_tasks_reparse_settings CHECK (
        reparse_settings IS NULL
        OR (kind = 'ingest_document' AND jsonb_typeof(reparse_settings) = 'object')
    ),
    CONSTRAINT ck_knowledge_tasks_status CHECK (
        status IN ('queued', 'running', 'retry_wait', 'succeeded', 'failed')
    ),
    CONSTRAINT ck_knowledge_tasks_stage CHECK (
        stage IN ('queued', 'reading_source', 'extracting_splitting', 'loading_segments', 'summarizing', 'embedding', 'publishing', 'done')
    ),
    CONSTRAINT ck_knowledge_tasks_progress_units CHECK (
        completed_units >= 0
        AND (total_units IS NULL OR (total_units >= 0 AND completed_units <= total_units))
    ),
    CONSTRAINT ck_knowledge_tasks_attempts CHECK (
        attempt_count BETWEEN 0 AND max_attempts AND max_attempts = 3
    ),
    CONSTRAINT ck_knowledge_tasks_claim CHECK (
        (status = 'running' AND claim_token IS NOT NULL AND lease_until IS NOT NULL)
        OR (status <> 'running' AND claim_token IS NULL AND lease_until IS NULL)
    ),
    CONSTRAINT ck_knowledge_tasks_finished CHECK (
        (status IN ('succeeded', 'failed') AND finished_at IS NOT NULL)
        OR (status NOT IN ('succeeded', 'failed') AND finished_at IS NULL)
    ),
    CONSTRAINT fk_knowledge_tasks_project FOREIGN KEY (project_id)
        REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT ck_knowledge_tasks_extraction_pin CHECK (extraction_id IS NULL OR (kind IN ('ingest_document', 'reembed_document', 'summarize_document') AND status IN ('queued', 'running', 'retry_wait'))),
    CONSTRAINT fk_knowledge_tasks_extraction FOREIGN KEY(project_id, extraction_id) REFERENCES knowledge_extractions (project_id, id) ON DELETE RESTRICT
);


CREATE UNIQUE INDEX uq_knowledge_tasks_open_extraction_delete ON knowledge_tasks (resource_id) WHERE kind = 'delete_extraction' AND status IN ('queued', 'running', 'retry_wait');
CREATE INDEX ix_knowledge_tasks_claim
    ON knowledge_tasks (available_at, created_at, id)
    WHERE status IN ('queued', 'retry_wait');

CREATE INDEX ix_knowledge_tasks_expired
    ON knowledge_tasks (lease_until, id)
    WHERE status = 'running';

-- One open indexing operation per document/version regardless of kind:
-- ingest, reembed, summarize and relex share the guard, so none can slip
-- past the slot another holds.
CREATE UNIQUE INDEX uq_knowledge_tasks_open_indexing
    ON knowledge_tasks (resource_id, target_version)
    WHERE kind IN ('ingest_document', 'reembed_document', 'summarize_document', 'relex_document') AND status IN ('queued', 'running', 'retry_wait');

CREATE UNIQUE INDEX uq_knowledge_tasks_open_document_delete
    ON knowledge_tasks (resource_id)
    WHERE kind = 'delete_document' AND status IN ('queued', 'running', 'retry_wait');

CREATE UNIQUE INDEX uq_knowledge_tasks_open_document_object_delete
    ON knowledge_tasks (storage_key)
    WHERE kind = 'delete_document_object' AND status IN ('queued', 'running', 'retry_wait');

CREATE UNIQUE INDEX uq_knowledge_tasks_open_base_delete
    ON knowledge_tasks (resource_id)
    WHERE kind = 'delete_knowledge_base' AND status IN ('queued', 'running', 'retry_wait');

-- Host-owned singleton (id = 1) with the PostgreSQL-administered Knowledge
-- configuration: module switch, worker limits, quotas, MinIO storage target
-- and the System Model reference for segment-summary generation. The MinIO
-- secret key is stored as an AES-GCM envelope (nonce + ciphertext).
CREATE TABLE knowledge_system_settings (
    id SMALLINT DEFAULT 1 NOT NULL,
    revision INTEGER DEFAULT 1 NOT NULL,
    enabled BOOLEAN DEFAULT false NOT NULL,
    worker_concurrency SMALLINT DEFAULT 2 NOT NULL,
    task_timeout_seconds INTEGER DEFAULT 900 NOT NULL,
    upload_max_bytes BIGINT DEFAULT 52428800 NOT NULL,
    max_knowledge_bases_per_project INTEGER DEFAULT 20 NOT NULL,
    max_documents_per_knowledge_base INTEGER DEFAULT 500 NOT NULL,
    max_segments_per_document INTEGER DEFAULT 5000 NOT NULL,
    minio_endpoint VARCHAR(512),
    minio_bucket VARCHAR(255),
    minio_access_key VARCHAR(512),
    minio_secure BOOLEAN DEFAULT false NOT NULL,
    minio_secret_nonce BYTEA,
    minio_secret_ciphertext BYTEA,
    summary_model_name VARCHAR(36),
    query_cache_enabled BOOLEAN DEFAULT true NOT NULL,
    query_cache_max_entries INTEGER DEFAULT 512 NOT NULL,
    query_cache_ttl_seconds INTEGER DEFAULT 300 NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    etl_type VARCHAR(32) DEFAULT 'builtin' NOT NULL,
    extraction_cache_enabled BOOLEAN DEFAULT true NOT NULL,
    CONSTRAINT pk_knowledge_system_settings PRIMARY KEY (id),
    CONSTRAINT ck_knowledge_system_settings_singleton CHECK (id = 1),
    CONSTRAINT ck_knowledge_system_settings_worker_concurrency CHECK (
        worker_concurrency BETWEEN 1 AND 16
    ),
    CONSTRAINT ck_knowledge_system_settings_task_timeout CHECK (
        task_timeout_seconds BETWEEN 30 AND 7200
    ),
    CONSTRAINT ck_knowledge_system_settings_upload_max_bytes CHECK (
        upload_max_bytes BETWEEN 1 AND 52428800
    ),
    CONSTRAINT ck_knowledge_system_settings_max_bases CHECK (
        max_knowledge_bases_per_project >= 1
    ),
    CONSTRAINT ck_knowledge_system_settings_max_documents CHECK (
        max_documents_per_knowledge_base >= 1
    ),
    CONSTRAINT ck_knowledge_system_settings_max_segments CHECK (
        max_segments_per_document BETWEEN 1 AND 5000
    ),
    CONSTRAINT ck_knowledge_system_settings_cache_entries CHECK (
        query_cache_max_entries BETWEEN 16 AND 65536
    ),
    CONSTRAINT ck_knowledge_system_settings_cache_ttl CHECK (
        query_cache_ttl_seconds BETWEEN 5 AND 86400
    ),
    CONSTRAINT ck_knowledge_system_settings_secret_pair CHECK (
        ((minio_secret_nonce IS NULL) = (minio_secret_ciphertext IS NULL))
        AND (
            minio_secret_nonce IS NULL
            OR (octet_length(minio_secret_nonce) = 12 AND octet_length(minio_secret_ciphertext) >= 16)
        )
    ),
    CONSTRAINT ck_knowledge_system_settings_enabled_requires_minio CHECK (
        NOT enabled
        OR (
            minio_endpoint IS NOT NULL
            AND minio_bucket IS NOT NULL
            AND minio_access_key IS NOT NULL
            AND minio_secret_nonce IS NOT NULL
            AND minio_secret_ciphertext IS NOT NULL
        )
    ),
    CONSTRAINT ck_knowledge_system_settings_etl_type CHECK (etl_type IN ('builtin', 'unstructured_local'))
);



INSERT INTO run_event_partition_state (singleton) VALUES (true);

INSERT INTO system_model_catalog_state (id, revision) VALUES (1, 1);

INSERT INTO system_runtime_policy_catalog_state (id, revision) VALUES (1, 1);

INSERT INTO alembic_version (version_num) VALUES ('schema_v1');



-- INCLUDE GENERATED SCHEMA COMMENTS FROM schema_comments.sql



SELECT ensure_run_events_month_partition(now());

SELECT ensure_run_events_month_partition(now() + INTERVAL '1 month');



COMMIT;
