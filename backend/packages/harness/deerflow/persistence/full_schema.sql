BEGIN;

-- ActWeave complete PostgreSQL application schema snapshot.
-- Applied only by `make setup-db` to an empty database.
-- This file is not an incremental migration and must remain transaction-safe.

-- Trusted extension for trigram search over the Memory episode archive; a
-- regular database owner role can create it without superuser rights.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);


CREATE TABLE asset_catalog_state (
    id SMALLINT DEFAULT 1 NOT NULL,
    generation BIGINT DEFAULT 1 NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_asset_catalog_state_generation CHECK (generation >= 1),
    CONSTRAINT ck_asset_catalog_state_singleton CHECK (id = 1)
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
    CONSTRAINT ck_system_asset_upgrade_audit_kind CHECK (asset_kind IN ('agent', 'skill')),
    CONSTRAINT ck_system_asset_upgrade_audit_before_checksum CHECK (before_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_asset_upgrade_audit_after_checksum CHECK (after_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_asset_upgrade_audit_package_digest CHECK (package_digest ~ '^[0-9a-f]{64}$')
);

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
    CONSTRAINT ck_dead_jobs_retry_safety CHECK (retry_safety IN ('safe', 'unknown', 'unsafe')),
    CONSTRAINT ck_dead_jobs_attempt_count CHECK (attempt_count >= 1)
);

CREATE INDEX ix_dead_jobs_project_dead ON dead_jobs (project_id, dead_at DESC, job_id);

CREATE TABLE jobs (
    id UUID NOT NULL,
    job_type VARCHAR(32) NOT NULL,
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36),
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
    CONSTRAINT ck_jobs_authority_shape CHECK ((job_type = 'private_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NOT NULL) OR (job_type = 'automation_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL AND origin_trace_id IS NOT NULL) OR (job_type = 'retention_purge' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_dream_prepare' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)),
    CONSTRAINT ck_jobs_memory_namespace CHECK ((job_type IN ('memory_dream', 'memory_dream_prepare', 'memory_seal')) = (namespace IS NOT NULL)),
    CONSTRAINT ck_jobs_type CHECK (job_type IN ('private_run', 'automation_run', 'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_dream_prepare', 'memory_seal')),
    CONSTRAINT ck_jobs_retry_safety CHECK (retry_safety IN ('safe', 'unknown', 'unsafe')),
    CONSTRAINT ck_jobs_execution_domain_affinity CHECK (execution_domain_affinity IS NULL OR (job_type = 'private_run' AND execution_domain_affinity ~ '^[0-9a-f]{64}$')),
    CONSTRAINT ck_jobs_status CHECK (status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')),
    CONSTRAINT ck_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts >= 1),
    CONSTRAINT uq_jobs_type_idempotency UNIQUE (job_type, idempotency_key),
    CONSTRAINT uq_jobs_id_project_owner UNIQUE (id, project_id, owner_user_id),
    CONSTRAINT uq_jobs_id_project_owner_run UNIQUE (id, project_id, owner_user_id, run_id),
    CONSTRAINT uq_jobs_id_project_owner_run_execution_domain UNIQUE (id, project_id, owner_user_id, run_id, execution_domain_affinity),
    CONSTRAINT uq_jobs_id_project_owner_namespace UNIQUE (id, project_id, owner_user_id, namespace),
    CONSTRAINT uq_jobs_predecessor_dead_job UNIQUE (predecessor_dead_job_id)
);

CREATE INDEX ix_jobs_active_lease ON jobs (lease_expires_at, id) WHERE status IN ('leased', 'running');

CREATE INDEX ix_jobs_claim ON jobs (status, available_at, priority DESC, created_at);

CREATE INDEX ix_jobs_execution_domain_claim ON jobs (execution_domain_affinity, status, available_at, priority DESC, created_at) WHERE execution_domain_affinity IS NOT NULL;

CREATE INDEX ix_jobs_private_scope ON jobs (project_id, owner_user_id, created_at);

CREATE UNIQUE INDEX uq_jobs_active_memory_seal ON jobs (project_id, owner_user_id, namespace) WHERE job_type = 'memory_seal' AND status IN ('queued', 'leased', 'running', 'retry_wait');

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
    PRIMARY KEY (run_id),
    CONSTRAINT ck_runs_finalization_status CHECK (finalization_status IN ('pending', 'finalizing', 'complete', 'failed')),
    CONSTRAINT uq_runs_job_scope UNIQUE (project_id, owner_user_id, run_id),
    CONSTRAINT uq_runs_job_trace_scope UNIQUE (project_id, owner_user_id, run_id, origin_trace_id),
    CONSTRAINT uq_runs_private_scope UNIQUE (project_id, owner_user_id, thread_id, run_id)
);

CREATE INDEX ix_runs_owner_user_id ON runs (owner_user_id);

CREATE INDEX ix_runs_project_id ON runs (project_id);

CREATE INDEX ix_runs_thread_id ON runs (thread_id);

CREATE INDEX ix_runs_thread_status ON runs (thread_id, status);

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
    CONSTRAINT ck_scheduled_task_runs_status CHECK (status IN ('queued', 'launching', 'running', 'success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')),
    CONSTRAINT ck_scheduled_task_runs_trigger CHECK (trigger IN ('scheduled', 'manual')),
    CONSTRAINT ck_scheduled_task_runs_attempt_count CHECK (launch_attempt_count >= 0 AND (resolved_membership_version IS NULL OR resolved_membership_version >= 1)),
    CONSTRAINT ck_scheduled_task_runs_run_requires_thread CHECK (run_id IS NULL OR thread_id IS NOT NULL),
    CONSTRAINT ck_scheduled_task_runs_task_version CHECK (task_version >= 1),
    CONSTRAINT uq_scheduled_task_runs_job_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT uq_scheduled_task_runs_occurrence UNIQUE (project_id, owner_user_id, task_id, occurrence_key)
);

CREATE INDEX ix_scheduled_task_runs_active_occurrence ON scheduled_task_runs (project_id, owner_user_id, status, scheduled_for, id) WHERE status IN ('queued', 'launching', 'running');

CREATE INDEX ix_scheduled_task_runs_history ON scheduled_task_runs (project_id, owner_user_id, task_id, created_at DESC, id DESC);

CREATE INDEX ix_scheduled_task_runs_owner_user_id ON scheduled_task_runs (owner_user_id);

CREATE INDEX ix_scheduled_task_runs_project_id ON scheduled_task_runs (project_id);

CREATE UNIQUE INDEX uq_scheduled_task_runs_manual_idempotency ON scheduled_task_runs (project_id, owner_user_id, task_id, manual_idempotency_hash) WHERE manual_idempotency_hash IS NOT NULL;

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
    PRIMARY KEY (id),
    CONSTRAINT ck_users_system_role CHECK (system_role IN ('system_admin', 'user')),
    CONSTRAINT ck_users_principal_type CHECK (principal_type IN ('human', 'channel_guest')),
    CONSTRAINT ck_users_oauth_identity_shape CHECK ((oauth_provider IS NULL AND oauth_id IS NULL) OR (oauth_provider IS NOT NULL AND oauth_id IS NOT NULL)),
    CONSTRAINT ck_users_username_format CHECK (username IS NULL OR username ~ '^[a-z][a-z0-9_]{2,31}$'),
    CONSTRAINT ck_users_channel_guest_identity CHECK ((principal_type = 'human' AND email IS NOT NULL) OR (principal_type = 'channel_guest' AND email IS NULL AND username IS NULL AND password_hash IS NULL AND oauth_provider IS NULL AND oauth_id IS NULL AND system_role = 'user' AND needs_setup IS FALSE AND token_version = 0)),
    CONSTRAINT ck_users_preferences_version CHECK (preferences_version >= 1),
    CONSTRAINT uq_users_id_principal_type UNIQUE (id, principal_type)
);

CREATE UNIQUE INDEX idx_users_oauth_identity ON users (oauth_provider, oauth_id) WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL;

CREATE UNIQUE INDEX ix_users_email ON users (lower(email)) WHERE email IS NOT NULL;

CREATE UNIQUE INDEX ix_users_username ON users (username) WHERE username IS NOT NULL;

CREATE TABLE auth_sessions (
    session_id_hash CHAR(64) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (session_id_hash),
    CONSTRAINT ck_auth_sessions_expiry CHECK (expires_at > created_at),
    CONSTRAINT ck_auth_sessions_hash CHECK (session_id_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_auth_sessions_last_seen CHECK (last_seen_at >= created_at AND last_seen_at <= expires_at),
    CONSTRAINT ck_auth_sessions_revoked_at CHECK (revoked_at IS NULL OR revoked_at >= created_at),
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX ix_auth_sessions_expires_at ON auth_sessions (expires_at, session_id_hash);

CREATE INDEX ix_auth_sessions_revoked_at ON auth_sessions (revoked_at, session_id_hash) WHERE revoked_at IS NOT NULL;

CREATE INDEX ix_auth_sessions_user_active ON auth_sessions (user_id, expires_at) WHERE revoked_at IS NULL;

CREATE TABLE worker_nodes (
    id UUID NOT NULL,
    version VARCHAR(64) NOT NULL,
    capabilities_json JSON DEFAULT '[]' NOT NULL,
    max_concurrent_jobs INTEGER NOT NULL,
    draining BOOLEAN DEFAULT false NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_worker_nodes_capacity CHECK (max_concurrent_jobs >= 1)
);

CREATE INDEX ix_worker_nodes_fresh ON worker_nodes (draining, heartbeat_at);

CREATE TABLE job_attempts (
    id UUID NOT NULL,
    job_id UUID NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_id UUID NOT NULL,
    lease_token_hash CHAR(64) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    heartbeat_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    finished_at TIMESTAMP WITH TIME ZONE,
    outcome VARCHAR(16),
    public_error_code VARCHAR(64),
    checkpoint_cursor VARCHAR(128),
    stream_cursor BIGINT,
    PRIMARY KEY (id),
    CONSTRAINT ck_job_attempts_outcome CHECK (outcome IS NULL OR outcome IN ('succeeded', 'retry', 'cancelled', 'failed', 'lease_lost', 'dead')),
    CONSTRAINT ck_job_attempts_number CHECK (attempt_number >= 1),
    CONSTRAINT ck_job_attempts_stream_cursor CHECK (stream_cursor IS NULL OR stream_cursor >= 0),
    FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE CASCADE,
    FOREIGN KEY(worker_id) REFERENCES worker_nodes (id) ON DELETE RESTRICT,
    CONSTRAINT uq_job_attempts_number UNIQUE (job_id, attempt_number),
    CONSTRAINT uq_job_attempts_id_job UNIQUE (id, job_id)
);

CREATE INDEX ix_job_attempts_job_started ON job_attempts (job_id, started_at DESC);

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
    CONSTRAINT ck_projects_slug_format CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    CONSTRAINT ck_projects_status CHECK (status IN ('active', 'pending_deletion')),
    CONSTRAINT ck_projects_slug_length CHECK (char_length(slug) BETWEEN 3 AND 63),
    CONSTRAINT ck_projects_membership_version CHECK (membership_version >= 1),
    CONSTRAINT ck_projects_slug_lowercase CHECK (slug = lower(slug)),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    CONSTRAINT fk_projects_deletion_requested_by_user_id_users FOREIGN KEY(deletion_requested_by_user_id) REFERENCES users (id),
    CONSTRAINT uq_projects_slug UNIQUE (slug)
);

CREATE TABLE agents (
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
    CONSTRAINT ck_agents_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_agents_status CHECK (status IN ('active', 'archived', 'suspended')),
    CONSTRAINT ck_agents_revision CHECK (revision >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    CONSTRAINT uq_agents_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_agents_project_id_id UNIQUE (project_id, id),
    CONSTRAINT uq_agents_source_key UNIQUE (source_key)
);

CREATE UNIQUE INDEX uq_agents_project_slug ON agents (project_id, lower(slug)) WHERE scope = 'project' AND status != 'archived';

CREATE UNIQUE INDEX uq_agents_system_slug ON agents (lower(slug)) WHERE scope = 'system';

CREATE TABLE project_default_agents (
    project_id UUID NOT NULL,
    agent_asset_id UUID,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id),
    CONSTRAINT ck_project_default_agents_revision CHECK (revision >= 1),
    CONSTRAINT fk_project_default_agents_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_default_agents_project_agent FOREIGN KEY(project_id, agent_asset_id) REFERENCES agents (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_default_agents_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_default_agents_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

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
    CONSTRAINT ck_audit_logs_outcome CHECK (outcome IN ('success', 'rejected', 'failed')),
    CONSTRAINT ck_audit_logs_actor CHECK ((actor_user_id IS NOT NULL AND actor_process IS NULL) OR (actor_user_id IS NULL AND actor_process IS NOT NULL)),
    FOREIGN KEY(actor_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(attempt_id) REFERENCES job_attempts (id) ON DELETE RESTRICT,
    FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT,
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
);

CREATE INDEX ix_audit_logs_platform_cursor ON audit_logs (occurred_at DESC, id DESC);

CREATE INDEX ix_audit_logs_project_cursor ON audit_logs (project_id, occurred_at DESC, id DESC);

CREATE TABLE credentials (
    id UUID NOT NULL,
    scope VARCHAR(16) NOT NULL,
    project_id UUID,
    name VARCHAR(63) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    credential_type VARCHAR(32) NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    is_delete BOOLEAN DEFAULT false NOT NULL,
    current_version_id UUID,
    version BIGINT DEFAULT 1 NOT NULL,
    source_key VARCHAR(255),
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_credentials_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_credentials_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_credentials_version CHECK (version >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    FOREIGN KEY(revoked_by_user_id) REFERENCES users (id),
    CONSTRAINT uq_credentials_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_credentials_source_key UNIQUE (source_key)
);

CREATE UNIQUE INDEX uq_credentials_project_name ON credentials (project_id, lower(name)) WHERE scope = 'project' AND is_delete = false;

CREATE UNIQUE INDEX uq_credentials_system_name ON credentials (lower(name)) WHERE scope = 'system' AND is_delete = false;

CREATE INDEX ix_credentials_scope_project_is_delete ON credentials (scope, project_id, is_delete);

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
    CONSTRAINT fk_project_channel_instances_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_instances_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_instances_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_channel_instances_project_id UNIQUE (project_id, id),
    CONSTRAINT uq_project_channel_instances_project_provider UNIQUE (project_id, id, provider)
);

CREATE UNIQUE INDEX uq_project_channel_instances_live_provider ON project_channel_instances (project_id, provider) WHERE deleted_at IS NULL;

CREATE UNIQUE INDEX uq_project_channel_instances_live_identity ON project_channel_instances (provider, provider_identity_digest) WHERE deleted_at IS NULL;

CREATE INDEX ix_project_channel_instances_runtime ON project_channel_instances (desired_status, observed_status, id) WHERE deleted_at IS NULL;

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
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    CONSTRAINT uq_mcp_servers_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_mcp_servers_source_key UNIQUE (source_key)
);

CREATE UNIQUE INDEX uq_mcp_servers_project_slug ON mcp_servers (project_id, lower(slug)) WHERE scope = 'project';

CREATE UNIQUE INDEX uq_mcp_servers_system_slug ON mcp_servers (lower(slug)) WHERE scope = 'system';

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
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(redeemed_by_user_id) REFERENCES users (id),
    CONSTRAINT uq_project_invitations_token_hash UNIQUE (token_hash)
);

CREATE UNIQUE INDEX uq_project_invitations_pending_email ON project_invitations (project_id, invited_email) WHERE status = 'pending';

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
    FOREIGN KEY(project_invitation_id) REFERENCES project_invitations (id) ON DELETE CASCADE,
    FOREIGN KEY(recipient_user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT uq_user_notifications_project_invitation_id UNIQUE (project_invitation_id)
);

CREATE INDEX ix_user_notifications_recipient_cursor ON user_notifications (recipient_user_id, created_at DESC, id DESC);

CREATE INDEX ix_user_notifications_recipient_unread ON user_notifications (recipient_user_id, created_at) WHERE read_at IS NULL;

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
    CONSTRAINT ck_project_memberships_end_reason CHECK (end_reason IS NULL OR end_reason IN ('left', 'removed')),
    CONSTRAINT ck_project_memberships_activation_generation CHECK (activation_generation >= 1),
    CONSTRAINT ck_project_memberships_role CHECK (role IN ('admin', 'editor', 'runner', 'viewer', 'channel_guest')),
    CONSTRAINT ck_project_memberships_status CHECK (status IN ('active', 'left', 'removed')),
    CONSTRAINT ck_project_memberships_version CHECK (version >= 1),
    CONSTRAINT fk_project_memberships_ended_by_user_id_users FOREIGN KEY(ended_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT uq_project_memberships_project_user UNIQUE (project_id, user_id),
    CONSTRAINT uq_project_memberships_guest_identity UNIQUE (project_id, user_id, id, role)
);

CREATE INDEX ix_project_memberships_user_id ON project_memberships (user_id);

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
    CONSTRAINT fk_project_channel_group_bindings_instance FOREIGN KEY(project_id, channel_instance_id, provider) REFERENCES project_channel_instances (project_id, id, provider) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_group_bindings_agent FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_group_bindings_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_group_bindings_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_channel_group_bindings_project_id UNIQUE (project_id, id)
);

CREATE UNIQUE INDEX uq_project_channel_group_bindings_live_group ON project_channel_group_bindings (channel_instance_id, external_group_ref) WHERE deleted_at IS NULL;

CREATE INDEX ix_project_channel_group_bindings_project_status ON project_channel_group_bindings (project_id, status, id) WHERE deleted_at IS NULL;

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
    CONSTRAINT fk_channel_external_principals_group_binding FOREIGN KEY(project_id, group_binding_id) REFERENCES project_channel_group_bindings (project_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_external_principals_guest_user FOREIGN KEY(principal_user_id, principal_type) REFERENCES users (id, principal_type) ON DELETE CASCADE,
    CONSTRAINT fk_channel_external_principals_guest_membership FOREIGN KEY(project_id, principal_user_id, membership_id, membership_role) REFERENCES project_memberships (project_id, user_id, id, role) ON DELETE CASCADE,
    CONSTRAINT uq_channel_external_principals_group_account UNIQUE (group_binding_id, external_account_ref)
);

CREATE INDEX ix_channel_external_principals_project_status ON channel_external_principals (project_id, status, id);

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
    CONSTRAINT ck_project_usage_ledger_dimension CHECK (dimension IN ('members', 'storage_bytes', 'concurrent_runs', 'mcp_calls_daily')),
    CONSTRAINT ck_project_usage_ledger_delta CHECK (delta <> 0),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_usage_ledger_idempotency UNIQUE (project_id, dimension, idempotency_key)
);

CREATE INDEX ix_project_usage_ledger_project_cursor ON project_usage_ledger (project_id, occurred_at DESC, id DESC);

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
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    CONSTRAINT uq_skills_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_skills_source_key UNIQUE (source_key)
);

CREATE UNIQUE INDEX uq_skills_project_display_name ON skills (project_id, lower(display_name)) WHERE scope = 'project';

CREATE UNIQUE INDEX uq_skills_project_slug ON skills (project_id, lower(slug)) WHERE scope = 'project';

CREATE UNIQUE INDEX uq_skills_system_slug ON skills (lower(slug)) WHERE scope = 'system';

CREATE TABLE agent_versions (
    id UUID NOT NULL,
    agent_id UUID NOT NULL,
    version_number BIGINT NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    soul TEXT NOT NULL,
    model_ref VARCHAR(255) NOT NULL,
    model_settings JSONB DEFAULT '{}'::jsonb NOT NULL,
    tool_groups JSONB DEFAULT '[]'::jsonb NOT NULL,
    supersedes_version_id UUID,
    payload_checksum CHAR(64) NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    agents_instructions TEXT DEFAULT '' NOT NULL,
    identity TEXT DEFAULT '' NOT NULL,
    user_context TEXT DEFAULT '' NOT NULL,
    payload_schema_version INTEGER DEFAULT 1 NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_agent_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_agent_versions_payload_schema_version CHECK (payload_schema_version IN (1, 2, 3, 4)),
    CONSTRAINT ck_agent_versions_model_settings CHECK (
        jsonb_typeof(model_settings) = 'object'
        AND (
            payload_schema_version IN (3, 4)
            OR model_settings = '{}'::jsonb
        )
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
    CONSTRAINT ck_agent_versions_number CHECK (version_number >= 1),
    FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(supersedes_version_id) REFERENCES agent_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_agent_versions_asset_id UNIQUE (agent_id, id),
    CONSTRAINT uq_agent_versions_asset_number UNIQUE (agent_id, version_number)
);

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
    created_agent_version_id UUID,
    create_idempotency_key_hash CHAR(64) NOT NULL,
    create_request_checksum CHAR(64) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    created_agent_deleted BOOLEAN DEFAULT false NOT NULL,
    generation_model_ref VARCHAR(36),
    generation_mode VARCHAR(16),
    CONSTRAINT pk_agent_design_sessions PRIMARY KEY (id),
    CONSTRAINT ck_agent_design_sessions_blueprint CHECK ((blueprint_json IS NULL AND blueprint_checksum IS NULL) OR (blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL)),
    CONSTRAINT ck_agent_design_sessions_completion CHECK ((status = 'completed' AND ((created_agent_deleted IS FALSE AND created_agent_id IS NOT NULL AND created_agent_version_id IS NOT NULL) OR (created_agent_deleted IS TRUE AND created_agent_id IS NULL AND created_agent_version_id IS NULL))) OR (status <> 'completed' AND created_agent_deleted IS FALSE AND created_agent_id IS NULL AND created_agent_version_id IS NULL)),
    CONSTRAINT ck_agent_design_sessions_ready_blueprint CHECK ((status IN ('proposal_ready', 'committing', 'completed') AND blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL) OR status NOT IN ('proposal_ready', 'committing', 'completed')),
    CONSTRAINT ck_agent_design_sessions_clarification CHECK ((status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)),
    CONSTRAINT ck_agent_design_sessions_error CHECK ((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)),
    CONSTRAINT ck_agent_design_sessions_generation_preference CHECK ((generation_model_ref IS NULL AND generation_mode IS NULL) OR (generation_model_ref IS NOT NULL AND generation_mode IN ('flash', 'thinking', 'pro', 'ultra'))),
    CONSTRAINT ck_agent_design_sessions_revision CHECK (revision >= 1),
    CONSTRAINT ck_agent_design_sessions_status CHECK (status IN ('interviewing', 'generating', 'awaiting_clarification', 'proposal_ready', 'committing', 'completed', 'failed', 'cancelled')),
    CONSTRAINT fk_agent_design_sessions_created_agent_project FOREIGN KEY(project_id, created_agent_id) REFERENCES agents (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_created_agent_version FOREIGN KEY(created_agent_id, created_agent_version_id) REFERENCES agent_versions (agent_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_sessions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_agent_design_sessions_create_idempotency UNIQUE (project_id, owner_user_id, create_idempotency_key_hash),
    CONSTRAINT uq_agent_design_sessions_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT uq_agent_design_sessions_thread_scope UNIQUE (project_id, owner_user_id, thread_id)
);

CREATE INDEX ix_agent_design_sessions_resume ON agent_design_sessions (project_id, owner_user_id, created_at DESC, id DESC) WHERE status NOT IN ('completed', 'cancelled');

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
    CONSTRAINT ck_agent_design_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
    CONSTRAINT ck_agent_design_operations_status CHECK (status IN ('in_progress', 'completed', 'failed', 'stopped')),
    CONSTRAINT ck_agent_design_operations_completion CHECK ((status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL) OR (status = 'stopped' AND result_revision IS NOT NULL AND public_error_code IS NULL)),
    CONSTRAINT ck_agent_design_operations_generation_profile CHECK ((requested_generation_profile_json IS NULL AND effective_generation_profile_json IS NULL) OR (operation_kind = 'turn' AND requested_generation_profile_json IS NOT NULL AND effective_generation_profile_json IS NOT NULL)),
    CONSTRAINT fk_agent_design_operations_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES agent_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_design_operations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_operations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_agent_design_operations_idempotency UNIQUE (project_id, owner_user_id, operation_kind, idempotency_key_hash),
    CONSTRAINT uq_agent_design_operations_private_scope UNIQUE (project_id, owner_user_id, session_id, id)
);

CREATE INDEX ix_agent_design_operations_session ON agent_design_operations (project_id, owner_user_id, session_id, created_at DESC);

CREATE TABLE agent_design_activities (
    seq BIGINT GENERATED ALWAYS AS IDENTITY NOT NULL,
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
    CONSTRAINT ck_channel_connections_status CHECK (status IN ('connected', 'frozen', 'revoked')),
    CONSTRAINT fk_channel_connections_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_connections_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_connections_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_connections_project_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_channel_connections_private_scope UNIQUE (project_id, owner_user_id, id)
);

CREATE INDEX idx_channel_connections_event_lookup ON channel_connections (channel_instance_id, provider, workspace_id, bot_user_id);

CREATE INDEX ix_channel_connections_owner_user_id ON channel_connections (owner_user_id);

CREATE INDEX ix_channel_connections_project_id ON channel_connections (project_id);

CREATE INDEX ix_channel_connections_provider ON channel_connections (provider);

CREATE INDEX ix_channel_connections_channel_instance_id ON channel_connections (channel_instance_id);

CREATE UNIQUE INDEX uq_channel_connection_owner_legacy_identity ON channel_connections (project_id, owner_user_id, provider, external_account_id, workspace_id) WHERE channel_instance_id IS NULL;

CREATE UNIQUE INDEX uq_channel_connection_owner_instance_identity ON channel_connections (project_id, owner_user_id, channel_instance_id, external_account_id, workspace_id) WHERE channel_instance_id IS NOT NULL;

CREATE UNIQUE INDEX uq_channel_connection_active_legacy_identity ON channel_connections (provider, external_account_id, workspace_id) WHERE status = 'connected' AND channel_instance_id IS NULL;

CREATE UNIQUE INDEX uq_channel_connection_active_instance_identity ON channel_connections (channel_instance_id, external_account_id, workspace_id) WHERE status = 'connected' AND channel_instance_id IS NOT NULL;

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
    CONSTRAINT fk_channel_oauth_states_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_oauth_states_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_oauth_states_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_oauth_states_project_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE RESTRICT
);

CREATE INDEX ix_channel_oauth_states_owner_user_id ON channel_oauth_states (owner_user_id);

CREATE INDEX ix_channel_oauth_states_project_id ON channel_oauth_states (project_id);

CREATE INDEX ix_channel_oauth_states_provider ON channel_oauth_states (provider);

CREATE INDEX ix_channel_oauth_states_channel_instance_id ON channel_oauth_states (channel_instance_id);

CREATE TABLE credential_versions (
    id UUID NOT NULL,
    credential_id UUID NOT NULL,
    version_number BIGINT NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    payload_schema_version INTEGER DEFAULT 1 NOT NULL,
    payload_schema JSONB NOT NULL,
    supersedes_version_id UUID,
    retired_at TIMESTAMP WITH TIME ZONE,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_credential_versions_status CHECK (status IN ('active', 'retired', 'revoked')),
    CONSTRAINT ck_credential_versions_payload_schema_version CHECK (payload_schema_version >= 1),
    CONSTRAINT ck_credential_versions_number CHECK (version_number >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(credential_id) REFERENCES credentials (id) ON DELETE RESTRICT,
    FOREIGN KEY(revoked_by_user_id) REFERENCES users (id),
    FOREIGN KEY(supersedes_version_id) REFERENCES credential_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_credential_versions_asset_id UNIQUE (credential_id, id),
    CONSTRAINT uq_credential_versions_asset_number UNIQUE (credential_id, version_number)
);

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
    CONSTRAINT fk_feedback_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_feedback_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_feedback_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_feedback_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_feedback_private_run_owner UNIQUE (project_id, owner_user_id, thread_id, run_id)
);

CREATE INDEX ix_feedback_owner_user_id ON feedback (owner_user_id);

CREATE INDEX ix_feedback_project_id ON feedback (project_id);

CREATE INDEX ix_feedback_run_id ON feedback (run_id);

CREATE INDEX ix_feedback_thread_id ON feedback (thread_id);

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
    CONSTRAINT ck_mcp_server_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_mcp_server_versions_transport CHECK (transport IN ('stdio', 'sse', 'http', 'streamable_http')),
    CONSTRAINT ck_mcp_server_versions_workflow_status CHECK (workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')),
    CONSTRAINT ck_mcp_server_versions_timeout CHECK (timeout_seconds > 0),
    CONSTRAINT ck_mcp_server_versions_number CHECK (version_number >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(mcp_server_id) REFERENCES mcp_servers (id) ON DELETE RESTRICT,
    FOREIGN KEY(reviewed_by_user_id) REFERENCES users (id),
    FOREIGN KEY(supersedes_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_mcp_server_versions_asset_id UNIQUE (mcp_server_id, id),
    CONSTRAINT uq_mcp_server_versions_asset_number UNIQUE (mcp_server_id, version_number)
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
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    snapshot_json JSONB NOT NULL,
    CONSTRAINT pk_run_asset_versions PRIMARY KEY (project_id, owner_user_id, run_id, asset_kind, dependency_order),
    CONSTRAINT ck_run_asset_versions_kind CHECK (asset_kind IN ('agent', 'skill', 'mcp')),
    CONSTRAINT ck_run_asset_versions_scope CHECK (asset_scope IN ('system', 'project')),
    CONSTRAINT ck_run_asset_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_asset_versions_generation CHECK (catalog_generation >= 0),
    CONSTRAINT ck_run_asset_versions_order CHECK (dependency_order >= 0),
    CONSTRAINT fk_run_asset_versions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_asset_versions_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_asset_versions_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_asset_versions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
);

CREATE TABLE run_event_partition_state (
    singleton BOOLEAN DEFAULT true NOT NULL,
    retained_from TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (singleton),
    CONSTRAINT ck_run_event_partition_state_singleton CHECK (singleton)
);

INSERT INTO run_event_partition_state (singleton) VALUES (true);

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
    CONSTRAINT fk_run_event_invariants_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT uq_run_events_private_seq UNIQUE (project_id, owner_user_id, thread_id, run_id, seq),
    CONSTRAINT uq_events_thread_seq UNIQUE (thread_id, seq)
);

CREATE INDEX ix_run_event_invariants_created_at ON run_event_invariants (created_at);

CREATE UNIQUE INDEX uq_run_events_stream_terminal ON run_event_invariants (project_id, owner_user_id, thread_id, run_id) WHERE is_stream_terminal;

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
    CONSTRAINT fk_run_events_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_events_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_events_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_events_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
) PARTITION BY RANGE (created_at);

CREATE INDEX ix_events_run ON run_events (thread_id, run_id, seq);

CREATE INDEX ix_events_thread_cat_seq ON run_events (thread_id, category, seq);

CREATE INDEX ix_run_events_owner_user_id ON run_events (owner_user_id);

CREATE INDEX ix_run_events_project_id ON run_events (project_id);

CREATE INDEX ix_run_events_stream_terminal ON run_events (project_id, owner_user_id, thread_id, run_id) WHERE category = 'stream' AND event_type = 'stream.end';

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
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    revocation_reason_code VARCHAR(32),
    PRIMARY KEY (id),
    CONSTRAINT ck_skill_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_versions_scan_decision CHECK (scan_decision IN ('allow', 'warn', 'block')),
    CONSTRAINT ck_skill_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_skill_versions_revocation CHECK ((revoked_at IS NULL) = (revoked_by_user_id IS NULL) AND (revoked_at IS NULL) = (revocation_reason_code IS NULL)),
    CONSTRAINT ck_skill_versions_revocation_reason CHECK (revocation_reason_code IS NULL OR revocation_reason_code IN ('security', 'policy', 'integrity')),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    CONSTRAINT fk_skill_versions_revoked_by_user_id FOREIGN KEY(revoked_by_user_id) REFERENCES users (id),
    FOREIGN KEY(skill_id) REFERENCES skills (id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_version_id) REFERENCES skill_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_skill_versions_asset_id UNIQUE (skill_id, id),
    CONSTRAINT uq_skill_versions_asset_number UNIQUE (skill_id, version_number)
);

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
    CONSTRAINT ck_threads_meta_agent_scope CHECK (agent_scope IN ('system', 'project')),
    CONSTRAINT ck_threads_meta_kind CHECK (thread_kind IN ('chat', 'skill_builder')),
    CONSTRAINT ck_threads_meta_checkpoint_delete_status CHECK (checkpoint_delete_status IN ('not_requested', 'pending', 'complete', 'retry_required')),
    CONSTRAINT ck_threads_meta_version CHECK (version >= 1),
    CONSTRAINT fk_threads_meta_agent_asset FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT fk_threads_meta_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_threads_meta_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_threads_meta_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_threads_meta_private_scope UNIQUE (project_id, owner_user_id, thread_id)
);

CREATE INDEX ix_threads_meta_assistant_id ON threads_meta (assistant_id);

CREATE INDEX ix_threads_meta_owner_user_id ON threads_meta (owner_user_id);

CREATE INDEX ix_threads_meta_project_id ON threads_meta (project_id);

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

CREATE INDEX ix_execution_approval_requests_private_cursor ON execution_approval_requests (project_id, owner_user_id, thread_id, created_at DESC, id DESC);

CREATE INDEX ix_execution_approval_requests_status_expiry ON execution_approval_requests (status, expires_at, id);

CREATE UNIQUE INDEX uq_execution_approval_requests_active_thread ON execution_approval_requests (project_id, owner_user_id, thread_id) WHERE status IN ('staged', 'pending', 'approved', 'claimed');

CREATE UNIQUE INDEX uq_execution_approval_requests_decision_idempotency ON execution_approval_requests (project_id, owner_user_id, decision_idempotency_key) WHERE decision_idempotency_key IS NOT NULL;

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

CREATE TABLE agent_version_mcp_refs (
    agent_version_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    sort_order BIGINT DEFAULT 0 NOT NULL,
    PRIMARY KEY (agent_version_id, mcp_server_version_id),
    CONSTRAINT ck_agent_version_mcp_refs_sort_order CHECK (sort_order >= 0),
    FOREIGN KEY(agent_version_id) REFERENCES agent_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(mcp_server_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT
);

CREATE TABLE agent_version_skill_refs (
    agent_version_id UUID NOT NULL,
    sort_order BIGINT DEFAULT 0 NOT NULL,
    skill_asset_scope VARCHAR(16) NOT NULL,
    skill_asset_id UUID NOT NULL,
    PRIMARY KEY (agent_version_id, skill_asset_scope, skill_asset_id),
    CONSTRAINT ck_agent_version_skill_refs_scope CHECK (skill_asset_scope IN ('system', 'project')),
    CONSTRAINT ck_agent_version_skill_refs_sort_order CHECK (sort_order >= 0),
    FOREIGN KEY(agent_version_id) REFERENCES agent_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_version_skill_refs_skill_asset FOREIGN KEY(skill_asset_id, skill_asset_scope) REFERENCES skills (id, scope) ON DELETE RESTRICT
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
    FOREIGN KEY(connection_id) REFERENCES channel_connections (id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_conversations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_conversations_private_connection FOREIGN KEY(project_id, owner_user_id, connection_id) REFERENCES channel_connections (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_conversations_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_conversations_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_channel_conversations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_channel_conversation_connection_external UNIQUE (connection_id, external_conversation_id, external_topic_id),
    CONSTRAINT uq_channel_conversation_delivery_scope UNIQUE (project_id, owner_user_id, connection_id, provider, external_conversation_id, external_topic_id, thread_id)
);

CREATE INDEX ix_channel_conversations_connection_id ON channel_conversations (connection_id);

CREATE INDEX ix_channel_conversations_owner_user_id ON channel_conversations (owner_user_id);

CREATE INDEX ix_channel_conversations_project_id ON channel_conversations (project_id);

CREATE INDEX ix_channel_conversations_provider ON channel_conversations (provider);

CREATE INDEX ix_channel_conversations_thread_id ON channel_conversations (thread_id);

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
    CONSTRAINT ck_channel_inbound_deliveries_digest CHECK (provider_delivery_digest <> ''),
    CONSTRAINT fk_channel_inbound_deliveries_connection FOREIGN KEY(project_id, owner_user_id, connection_id) REFERENCES channel_connections (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_channel_inbound_deliveries_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT uq_channel_inbound_deliveries_scope UNIQUE (project_id, owner_user_id, connection_id, provider, external_conversation_id, external_topic_id, provider_delivery_digest)
);

CREATE INDEX ix_channel_inbound_deliveries_run ON channel_inbound_deliveries (project_id, owner_user_id, run_id);

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

CREATE TABLE credential_envelopes (
    id UUID NOT NULL,
    credential_version_id UUID NOT NULL,
    envelope_generation BIGINT NOT NULL,
    key_id VARCHAR(255) NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    is_active BOOLEAN DEFAULT false NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    rotated_from_envelope_id UUID,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    activated_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (id),
    CONSTRAINT ck_credential_envelopes_generation CHECK (envelope_generation >= 1),
    CONSTRAINT ck_credential_envelopes_ciphertext_size CHECK (octet_length(ciphertext) >= 16),
    CONSTRAINT ck_credential_envelopes_nonce_size CHECK (octet_length(nonce) = 12),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(credential_version_id) REFERENCES credential_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(rotated_from_envelope_id) REFERENCES credential_envelopes (id) ON DELETE RESTRICT,
    CONSTRAINT uq_credential_envelopes_version_generation UNIQUE (credential_version_id, envelope_generation)
);

CREATE UNIQUE INDEX uq_credential_envelopes_active_version ON credential_envelopes (credential_version_id) WHERE is_active;

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
    CONSTRAINT ck_files_kind CHECK (kind IN ('upload', 'workspace', 'output')),
    CONSTRAINT ck_files_logical_path CHECK (logical_path <> '' AND left(logical_path, 1) <> '/' AND logical_path !~ '(^|/)\.\.(/|$)' AND logical_path !~ '^[A-Za-z]:'),
    CONSTRAINT ck_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_files_source_kind CHECK (source_file_id IS NULL OR kind = 'workspace'),
    CONSTRAINT ck_files_status CHECK (status IN ('staging', 'ready', 'deleted')),
    CONSTRAINT ck_files_size CHECK (size >= 0),
    CONSTRAINT ck_files_source_not_self CHECK (source_file_id IS NULL OR source_file_id <> id),
    CONSTRAINT ck_files_version CHECK (version >= 1),
    CONSTRAINT fk_files_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_created_by_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, created_by_run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_private_source FOREIGN KEY(project_id, owner_user_id, thread_id, source_file_id) REFERENCES files (project_id, owner_user_id, thread_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE,
    CONSTRAINT fk_files_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_files_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_files_private_scope UNIQUE (project_id, owner_user_id, thread_id, id)
);

CREATE UNIQUE INDEX uq_files_active_logical_path ON files (project_id, owner_user_id, thread_id, logical_path) WHERE status != 'deleted';

CREATE TABLE mcp_version_credential_slots (
    id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    name VARCHAR(63) NOT NULL,
    purpose TEXT DEFAULT '' NOT NULL,
    payload_schema JSONB NOT NULL,
    required BOOLEAN DEFAULT true NOT NULL,
    PRIMARY KEY (id),
    FOREIGN KEY(mcp_server_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_mcp_credential_slots_version_id UNIQUE (mcp_server_version_id, id),
    CONSTRAINT uq_mcp_credential_slots_version_name UNIQUE (mcp_server_version_id, name)
);

CREATE TABLE mcp_tool_discovery_attempts (
    job_id UUID NOT NULL,
    project_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    requested_by_user_id VARCHAR(36) NOT NULL,
    trigger VARCHAR(16) NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    grant_digest CHAR(64) NOT NULL,
    result_status VARCHAR(16),
    public_error_code VARCHAR(64),
    requested_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    PRIMARY KEY (job_id),
    CONSTRAINT ck_mcp_tool_discovery_attempt_trigger CHECK (trigger IN ('auto', 'manual')),
    CONSTRAINT ck_mcp_tool_discovery_attempt_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_mcp_tool_discovery_attempt_grant_digest CHECK (grant_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_mcp_tool_discovery_attempt_result_status CHECK (result_status IS NULL OR result_status IN ('succeeded', 'failed', 'cancelled')),
    CONSTRAINT ck_mcp_tool_discovery_attempt_result CHECK ((result_status IS NULL AND public_error_code IS NULL) OR (result_status = 'succeeded' AND public_error_code IS NULL) OR (result_status = 'cancelled' AND public_error_code IS NULL) OR (result_status = 'failed' AND public_error_code IN ('mcp_discovery_unavailable', 'mcp_catalog_invalid'))),
    CONSTRAINT ck_mcp_tool_discovery_attempt_revision CHECK (revision >= 1),
    CONSTRAINT fk_mcp_tool_discovery_attempt_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_mcp_tool_discovery_attempt_requester FOREIGN KEY(requested_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_mcp_tool_discovery_attempt_job FOREIGN KEY(job_id, project_id, requested_by_user_id) REFERENCES jobs (id, project_id, owner_user_id) ON DELETE CASCADE,
    CONSTRAINT fk_mcp_tool_discovery_attempt_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE CASCADE
);

CREATE INDEX ix_mcp_tool_discovery_attempts_version ON mcp_tool_discovery_attempts (project_id, mcp_server_id, mcp_server_version_id, requested_at DESC, job_id);

CREATE INDEX ix_mcp_tool_discovery_attempts_closure ON mcp_tool_discovery_attempts (project_id, mcp_server_id, mcp_server_version_id, payload_checksum, grant_digest, requested_at DESC);

CREATE TABLE project_mcp_tool_inventories (
    project_id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    mcp_server_id UUID NOT NULL,
    attempt_payload_checksum CHAR(64) NOT NULL,
    attempt_grant_digest CHAR(64) NOT NULL,
    attempt_status VARCHAR(16) NOT NULL,
    public_error_code VARCHAR(64),
    tools JSONB DEFAULT '[]'::jsonb NOT NULL,
    tools_payload_checksum CHAR(64),
    tools_grant_digest CHAR(64),
    last_attempt_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    last_success_at TIMESTAMP WITH TIME ZONE,
    revision BIGINT DEFAULT 1 NOT NULL,
    PRIMARY KEY (project_id, mcp_server_version_id),
    CONSTRAINT ck_project_mcp_tool_inventory_attempt_status CHECK (attempt_status IN ('ready', 'failed')),
    CONSTRAINT ck_project_mcp_tool_inventory_attempt_checksum CHECK (attempt_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_attempt_grant_digest CHECK (attempt_grant_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_tools_checksum CHECK (tools_payload_checksum IS NULL OR tools_payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_tools_grant_digest CHECK (tools_grant_digest IS NULL OR tools_grant_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_project_mcp_tool_inventory_error CHECK ((attempt_status = 'ready' AND public_error_code IS NULL) OR (attempt_status = 'failed' AND public_error_code IN ('mcp_discovery_unavailable', 'mcp_catalog_invalid'))),
    CONSTRAINT ck_project_mcp_tool_inventory_success_shape CHECK ((tools_payload_checksum IS NULL AND tools_grant_digest IS NULL AND last_success_at IS NULL) OR (tools_payload_checksum IS NOT NULL AND tools_grant_digest IS NOT NULL AND last_success_at IS NOT NULL)),
    CONSTRAINT ck_project_mcp_tool_inventory_tools_shape CHECK (jsonb_typeof(tools) = 'array' AND jsonb_array_length(tools) <= 128),
    CONSTRAINT ck_project_mcp_tool_inventory_time_order CHECK (last_success_at IS NULL OR last_success_at <= last_attempt_at),
    CONSTRAINT ck_project_mcp_tool_inventory_revision CHECK (revision >= 1),
    CONSTRAINT fk_project_mcp_tool_inventory_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_mcp_tool_inventory_version FOREIGN KEY(mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE CASCADE
);

CREATE INDEX ix_project_mcp_tool_inventories_asset ON project_mcp_tool_inventories (project_id, mcp_server_id, mcp_server_version_id);

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
    CONSTRAINT ck_project_system_agent_bindings_system_scope CHECK (system_asset_scope = 'system'),
    CONSTRAINT ck_project_system_agent_bindings_version CHECK (version >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_system_agent_bindings_system_asset FOREIGN KEY(system_agent_id, system_asset_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
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
    CONSTRAINT ck_project_system_mcp_bindings_system_scope CHECK (system_asset_scope = 'system'),
    CONSTRAINT ck_project_system_mcp_bindings_version CHECK (version >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_system_mcp_bindings_version FOREIGN KEY(system_mcp_server_id, mcp_server_version_id) REFERENCES mcp_server_versions (mcp_server_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_system_mcp_bindings_system_asset FOREIGN KEY(system_mcp_server_id, system_asset_scope) REFERENCES mcp_servers (id, scope) ON DELETE RESTRICT,
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
    CONSTRAINT ck_project_system_skill_bindings_system_scope CHECK (system_asset_scope = 'system'),
    CONSTRAINT ck_project_system_skill_bindings_version CHECK (version >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_system_skill_bindings_system_asset FOREIGN KEY(system_skill_id, system_asset_scope) REFERENCES skills (id, scope) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id)
);

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
    CONSTRAINT ck_scheduled_tasks_thread_mode CHECK ((context_mode = 'reuse_thread' AND thread_id IS NOT NULL) OR (context_mode = 'fresh_thread_per_run' AND thread_id IS NULL)),
    CONSTRAINT ck_scheduled_tasks_agent_scope CHECK (agent_scope IN ('system', 'project')),
    CONSTRAINT ck_scheduled_tasks_context_mode CHECK (context_mode IN ('fresh_thread_per_run', 'reuse_thread')),
    CONSTRAINT ck_scheduled_tasks_last_outcome CHECK (last_outcome IS NULL OR last_outcome IN ('success', 'failed', 'skipped', 'interrupted', 'cancelled', 'rejected')),
    CONSTRAINT ck_scheduled_tasks_overlap_policy CHECK (overlap_policy = 'skip'),
    CONSTRAINT ck_scheduled_tasks_schedule_type CHECK (schedule_type IN ('once', 'cron')),
    CONSTRAINT ck_scheduled_tasks_status CHECK (status IN ('enabled', 'paused', 'completed', 'failed', 'cancelled')),
    CONSTRAINT ck_scheduled_tasks_run_count CHECK (run_count >= 0),
    CONSTRAINT ck_scheduled_tasks_version CHECK (version >= 1),
    CONSTRAINT fk_scheduled_tasks_agent_asset FOREIGN KEY(agent_asset_id, agent_scope) REFERENCES agents (id, scope) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_scheduled_tasks_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_scheduled_tasks_private_scope UNIQUE (project_id, owner_user_id, id)
);

CREATE INDEX ix_scheduled_tasks_owner_user_id ON scheduled_tasks (owner_user_id);

CREATE INDEX ix_scheduled_tasks_project_id ON scheduled_tasks (project_id);

CREATE TABLE skill_version_files (
    skill_version_id UUID NOT NULL,
    path VARCHAR(1024) NOT NULL,
    media_type VARCHAR(255) NOT NULL,
    size_bytes BIGINT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    content BYTEA NOT NULL,
    PRIMARY KEY (skill_version_id, path),
    CONSTRAINT ck_skill_version_files_safe_path CHECK (path <> '' AND path !~ '(^/|(^|/)\.\.(/|$))'),
    CONSTRAINT ck_skill_version_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_version_files_content_size CHECK (size_bytes = octet_length(content)),
    CONSTRAINT ck_skill_version_files_size CHECK (size_bytes >= 0 AND size_bytes <= 104857600),
    FOREIGN KEY(skill_version_id) REFERENCES skill_versions (id) ON DELETE RESTRICT
);

CREATE TABLE thread_event_sequences (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    high_watermark BIGINT DEFAULT 0 NOT NULL,
    PRIMARY KEY (project_id, owner_user_id, thread_id),
    CONSTRAINT ck_thread_event_sequences_high_watermark CHECK (high_watermark >= 0),
    CONSTRAINT fk_thread_event_sequences_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE
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
    CONSTRAINT fk_artifacts_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_artifacts_private_file FOREIGN KEY(project_id, owner_user_id, thread_id, file_id) REFERENCES files (project_id, owner_user_id, thread_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_artifacts_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_artifacts_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_artifacts_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_artifacts_private_scope UNIQUE (project_id, owner_user_id, thread_id, run_id, id)
);

CREATE INDEX ix_artifacts_private_active ON artifacts (project_id, owner_user_id, thread_id, created_at) WHERE deleted_at IS NULL;

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
    CONSTRAINT ck_ea_output_delivery_candidates_path CHECK (logical_path LIKE 'outputs/%' AND logical_path <> 'outputs/' AND logical_path !~ '(^|/)\.\.(/|$)' AND logical_path !~ '^[A-Za-z]:'),
    CONSTRAINT ck_ea_output_delivery_candidates_version CHECK (file_version >= 1),
    CONSTRAINT ck_ea_output_delivery_candidates_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$')
);

CREATE INDEX ix_ea_output_delivery_candidates_private ON execution_approval_output_delivery_candidates (project_id, owner_user_id, thread_id, approval_id);

CREATE TABLE credential_grants (
    id UUID NOT NULL,
    mcp_server_version_id UUID NOT NULL,
    credential_slot_id UUID NOT NULL,
    credential_version_id UUID NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    version BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    PRIMARY KEY (id),
    CONSTRAINT ck_credential_grants_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_credential_grants_version CHECK (version >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(credential_version_id) REFERENCES credential_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_credential_grants_slot_version FOREIGN KEY(mcp_server_version_id, credential_slot_id) REFERENCES mcp_version_credential_slots (mcp_server_version_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(revoked_by_user_id) REFERENCES users (id)
);

CREATE UNIQUE INDEX uq_credential_grants_active_slot ON credential_grants (mcp_server_version_id, credential_slot_id) WHERE status = 'active';

CREATE TABLE file_chunks (
    file_id UUID NOT NULL,
    chunk_index INTEGER NOT NULL,
    content BYTEA NOT NULL,
    size INTEGER NOT NULL,
    sha256 CHAR(64) NOT NULL,
    PRIMARY KEY (file_id, chunk_index),
    CONSTRAINT ck_file_chunks_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_file_chunks_index CHECK (chunk_index >= 0),
    CONSTRAINT ck_file_chunks_content_size CHECK (size = octet_length(content)),
    CONSTRAINT ck_file_chunks_bounded_size CHECK (size > 0 AND size <= 1048576),
    CONSTRAINT ck_file_chunks_size CHECK (size >= 0),
    CONSTRAINT fk_file_chunks_file_id_files FOREIGN KEY(file_id) REFERENCES files (id) ON DELETE CASCADE
);

CREATE TABLE run_mcp_grant_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    mcp_version_id UUID NOT NULL,
    credential_slot_id UUID NOT NULL,
    credential_grant_id UUID NOT NULL,
    credential_version_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_run_mcp_grant_snapshots PRIMARY KEY (project_id, owner_user_id, run_id, mcp_version_id, credential_slot_id),
    CONSTRAINT fk_run_mcp_grant_snapshots_grant FOREIGN KEY(credential_grant_id) REFERENCES credential_grants (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_grant_snapshots_slot FOREIGN KEY(credential_slot_id) REFERENCES mcp_version_credential_slots (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_grant_snapshots_credential_version FOREIGN KEY(credential_version_id) REFERENCES credential_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_grant_snapshots_mcp_version FOREIGN KEY(mcp_version_id) REFERENCES mcp_server_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_grant_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_grant_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_mcp_grant_snapshots_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_mcp_grant_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
);

ALTER TABLE dead_jobs ADD CONSTRAINT fk_dead_jobs_job FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT;

ALTER TABLE dead_jobs ADD CONSTRAINT fk_dead_jobs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_lease_worker FOREIGN KEY(lease_owner_id) REFERENCES worker_nodes (id) ON DELETE SET NULL;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_predecessor_dead_job FOREIGN KEY(predecessor_dead_job_id) REFERENCES dead_jobs (job_id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_automation_occurrence FOREIGN KEY(project_id, owner_user_id, automation_occurrence_id) REFERENCES scheduled_task_runs (project_id, owner_user_id, id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_private_run FOREIGN KEY(project_id, owner_user_id, run_id, origin_trace_id) REFERENCES runs (project_id, owner_user_id, run_id, origin_trace_id) ON DELETE RESTRICT;

ALTER TABLE jobs ADD CONSTRAINT fk_jobs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_job FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE CASCADE;

ALTER TABLE runs ADD CONSTRAINT fk_runs_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT;

ALTER TABLE runs ADD CONSTRAINT fk_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_job FOREIGN KEY(job_id) REFERENCES jobs (id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_task FOREIGN KEY(project_id, owner_user_id, task_id) REFERENCES scheduled_tasks (project_id, owner_user_id, id) ON DELETE CASCADE;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_private_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT;

ALTER TABLE scheduled_task_runs ADD CONSTRAINT fk_scheduled_task_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT;

ALTER TABLE agents ADD CONSTRAINT fk_agents_current_version FOREIGN KEY(id, current_version_id) REFERENCES agent_versions (agent_id, id);

ALTER TABLE credentials ADD CONSTRAINT fk_credentials_current_version FOREIGN KEY(id, current_version_id) REFERENCES credential_versions (credential_id, id);

ALTER TABLE mcp_servers ADD CONSTRAINT fk_mcp_servers_current_published_version FOREIGN KEY(id, current_published_version_id) REFERENCES mcp_server_versions (mcp_server_id, id);

ALTER TABLE skills ADD CONSTRAINT fk_skills_current_version FOREIGN KEY(id, current_version_id) REFERENCES skill_versions (skill_id, id);

CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
DECLARE
    asset_scope text;
BEGIN
    IF current_setting('deerflow.system_asset_upgrade', true) = 'on'
       AND TG_TABLE_NAME IN ('agent_versions', 'skill_versions') THEN
        IF TG_TABLE_NAME = 'agent_versions' THEN
            SELECT scope INTO asset_scope FROM agents WHERE id = NEW.agent_id;
        ELSE
            SELECT scope INTO asset_scope FROM skills WHERE id = NEW.skill_id;
        END IF;
        IF asset_scope = 'system' THEN
            RETURN NEW;
        END IF;
    END IF;
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id', 'revocation_reason_code'
    ]::text[]) THEN
        RAISE EXCEPTION 'shared asset version payload is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
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
            SELECT current_version_id, status INTO current_id, asset_status
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
        RAISE EXCEPTION 'system binding requires an eligible Current Version'
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
    purge_allowed boolean := false;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'skill_version_files' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
            SELECT asset.scope, asset.project_id, asset.id
            INTO parent_scope, parent_project_id, parent_asset_id
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
                              AND asset.current_version_id IS NULL
                              AND current_setting(
                                  'deerflow.skill_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) INTO purge_allowed;
            END IF;
        WHEN 'agent_version_skill_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT asset.scope, asset.project_id, asset.id
            INTO parent_scope, parent_project_id, parent_asset_id
            FROM agent_versions version
            JOIN agents asset ON asset.id = version.agent_id
            WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_versions version
                    JOIN agents asset ON asset.id = version.agent_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.agent_version_id
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
                              AND asset.current_version_id IS NULL
                              AND current_setting(
                                  'deerflow.agent_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) INTO purge_allowed;
            END IF;
        WHEN 'agent_version_mcp_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT asset.scope, asset.project_id, asset.id
            INTO parent_scope, parent_project_id, parent_asset_id
            FROM agent_versions version
            JOIN agents asset ON asset.id = version.agent_id
            WHERE version.id = parent_version_id FOR UPDATE OF version, asset;
            IF TG_OP = 'DELETE' THEN
                SELECT EXISTS (
                    SELECT 1
                    FROM agent_versions version
                    JOIN agents asset ON asset.id = version.agent_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.agent_version_id
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
                              AND asset.current_version_id IS NULL
                              AND current_setting(
                                  'deerflow.agent_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM mcp_server_versions version
                    JOIN mcp_servers asset ON asset.id = version.mcp_server_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.mcp_server_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) INTO purge_allowed;
            END IF;
        WHEN 'mcp_version_credential_slots' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.mcp_server_version_id ELSE NEW.mcp_server_version_id END;
            SELECT workflow_status INTO parent_status
            FROM mcp_server_versions WHERE id = parent_version_id FOR UPDATE;
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
    IF TG_TABLE_NAME IN (
        'skill_version_files',
        'agent_version_skill_refs',
        'agent_version_mcp_refs'
    ) THEN
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
        RAISE EXCEPTION 'Agent and Skill version child rows are immutable'
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
    IF TG_TABLE_NAME = 'credential_versions' THEN
        IF NEW.status = OLD.status
           OR (OLD.status = 'active' AND NEW.status IN ('retired', 'revoked'))
           OR (OLD.status = 'retired' AND NEW.status = 'revoked') THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'invalid credential version status transition'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

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

CREATE TRIGGER trg_agent_versions_immutable BEFORE UPDATE ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_skill_versions_immutable BEFORE UPDATE ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_skill_versions_revocation BEFORE INSERT OR UPDATE OF revoked_at, revoked_by_user_id, revocation_reason_code ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_system_skill_version_revocation();

CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_credential_versions_immutable BEFORE UPDATE ON credential_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_version_skill_refs_immutable BEFORE UPDATE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_version_mcp_refs_immutable BEFORE UPDATE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_mcp_credential_slots_immutable BEFORE UPDATE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_bindings_current BEFORE INSERT OR UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version();

CREATE TRIGGER trg_skill_bindings_current BEFORE INSERT OR UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version();

CREATE TRIGGER trg_mcp_bindings_published BEFORE INSERT OR UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_eligible_version();

CREATE TRIGGER trg_mcp_server_versions_bound_published BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_mcp_published_version_downgrade();

CREATE TRIGGER trg_skill_version_files_child_immutable BEFORE INSERT OR DELETE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation();

CREATE TRIGGER trg_agent_version_skill_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation();

CREATE TRIGGER trg_agent_version_mcp_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation();

CREATE TRIGGER trg_mcp_credential_slots_child_immutable BEFORE INSERT OR DELETE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_asset_version_child_mutation();

CREATE TRIGGER trg_mcp_server_versions_state_transition BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_credential_versions_state_transition BEFORE UPDATE OF status ON credential_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, current_version_id, revision ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_version_id, revision ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skill_version_revocations_generation AFTER UPDATE OF revoked_at ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_server_versions_generation AFTER UPDATE OF workflow_status ON mcp_server_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_credentials_generation AFTER UPDATE OF status, current_version_id, is_delete ON credentials FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_credential_versions_generation AFTER UPDATE OF status ON credential_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_agent_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_agent_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skill_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_skill_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_bindings_generation AFTER INSERT OR UPDATE OR DELETE ON project_system_mcp_bindings FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_credential_grants_generation AFTER INSERT OR UPDATE OR DELETE ON credential_grants FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

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

CREATE OR REPLACE FUNCTION reject_m7_append_only_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'M7 append-only rows cannot be updated or deleted'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_project_usage_ledger_append_only BEFORE UPDATE OR DELETE ON project_usage_ledger FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_audit_logs_append_only BEFORE UPDATE OR DELETE ON audit_logs FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_dead_jobs_append_only BEFORE UPDATE OR DELETE ON dead_jobs FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

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

CREATE OR REPLACE FUNCTION set_m7_updated_at()
RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
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
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_agents_updated_at BEFORE UPDATE ON agents FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_agent_design_operations_updated_at BEFORE UPDATE ON agent_design_operations FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_agent_design_sessions_updated_at BEFORE UPDATE ON agent_design_sessions FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_asset_catalog_state_updated_at BEFORE UPDATE ON asset_catalog_state FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_channel_connections_updated_at BEFORE UPDATE ON channel_connections FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_channel_conversations_updated_at BEFORE UPDATE ON channel_conversations FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_channel_credentials_updated_at BEFORE UPDATE ON channel_credentials FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_channel_instances_updated_at BEFORE UPDATE ON project_channel_instances FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_channel_instance_leases_updated_at BEFORE UPDATE ON project_channel_instance_leases FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_channel_group_bindings_updated_at BEFORE UPDATE ON project_channel_group_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_channel_external_principals_updated_at BEFORE UPDATE ON channel_external_principals FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_credentials_updated_at BEFORE UPDATE ON credentials FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_files_updated_at BEFORE UPDATE ON files FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_mcp_servers_updated_at BEFORE UPDATE ON mcp_servers FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_invitation_rate_limits_updated_at BEFORE UPDATE ON project_invitation_rate_limits FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_memberships_updated_at BEFORE UPDATE ON project_memberships FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_default_agents_updated_at BEFORE UPDATE ON project_default_agents FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_quotas_updated_at BEFORE UPDATE ON project_quotas FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_system_agent_bindings_updated_at BEFORE UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_system_mcp_bindings_updated_at BEFORE UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_system_skill_bindings_updated_at BEFORE UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_project_usage_counters_updated_at BEFORE UPDATE ON project_usage_counters FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_projects_updated_at BEFORE UPDATE ON projects FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_runs_updated_at BEFORE UPDATE ON runs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_scheduled_task_runs_updated_at BEFORE UPDATE ON scheduled_task_runs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_scheduled_tasks_updated_at BEFORE UPDATE ON scheduled_tasks FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_skills_updated_at BEFORE UPDATE ON skills FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_threads_meta_updated_at BEFORE UPDATE ON threads_meta FOR EACH ROW EXECUTE FUNCTION set_threads_meta_updated_at();

ALTER TABLE skills ADD CONSTRAINT uq_skills_project_id_id UNIQUE (project_id, id);

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
    CONSTRAINT ck_skill_design_sessions_revision_target CHECK ((session_kind = 'create' AND target_skill_id IS NULL AND base_version_id IS NULL AND base_version_number IS NULL AND base_payload_checksum IS NULL AND target_skill_deleted IS FALSE) OR (session_kind = 'revise' AND ((target_skill_deleted IS FALSE AND target_skill_id IS NOT NULL AND base_version_id IS NOT NULL AND base_version_number IS NOT NULL AND base_payload_checksum IS NOT NULL) OR (target_skill_deleted IS TRUE AND target_skill_id IS NULL AND base_version_id IS NULL AND base_version_number IS NULL AND base_payload_checksum IS NULL)))),
    CONSTRAINT fk_skill_design_sessions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_skill_creator_version FOREIGN KEY(skill_creator_skill_id, skill_creator_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_created_skill_project FOREIGN KEY(project_id, created_skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_created_skill_version FOREIGN KEY(created_skill_id, created_skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_target_skill_project FOREIGN KEY(project_id, target_skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_base_version FOREIGN KEY(target_skill_id, base_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_skill_design_sessions_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT uq_skill_design_sessions_thread_scope UNIQUE (project_id, owner_user_id, thread_id),
    CONSTRAINT uq_skill_design_sessions_create_idempotency UNIQUE (project_id, owner_user_id, create_idempotency_key_hash)
);

CREATE INDEX ix_skill_design_sessions_resume ON skill_design_sessions (project_id, owner_user_id, status, updated_at DESC, id DESC);

CREATE UNIQUE INDEX uq_skill_design_sessions_live_revise_target ON skill_design_sessions (project_id, owner_user_id, target_skill_id) WHERE session_kind = 'revise' AND target_skill_id IS NOT NULL AND status NOT IN ('completed', 'cancelled');

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
    CONSTRAINT pk_skill_design_operations PRIMARY KEY (id),
    CONSTRAINT ck_skill_design_operations_kind CHECK (operation_kind IN ('turn', 'validate', 'commit', 'cancel')),
    CONSTRAINT ck_skill_design_operations_status CHECK (status IN ('in_progress', 'completed', 'failed')),
    CONSTRAINT ck_skill_design_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
    CONSTRAINT ck_skill_design_operations_terminal_kind CHECK (terminal_kind IS NULL OR terminal_kind IN ('clarification', 'candidate')),
    CONSTRAINT ck_skill_design_operations_terminal_checksum CHECK (terminal_request_checksum IS NULL OR terminal_request_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_design_operations_terminal_pair CHECK ((terminal_kind IS NULL AND terminal_request_checksum IS NULL) OR (terminal_kind IS NOT NULL AND terminal_request_checksum IS NOT NULL)),
    CONSTRAINT ck_skill_design_operations_completion CHECK ((status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL)),
    CONSTRAINT fk_skill_design_operations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_operations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_operations_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES skill_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_skill_design_operations_run FOREIGN KEY(project_id,owner_user_id,run_id) REFERENCES runs(project_id,owner_user_id,run_id) ON DELETE RESTRICT,
    CONSTRAINT uq_skill_design_operations_idempotency UNIQUE (project_id, owner_user_id, operation_kind, idempotency_key_hash),
    CONSTRAINT uq_skill_design_operations_run UNIQUE(project_id,owner_user_id,run_id)
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
    CONSTRAINT ck_skill_design_draft_files_safe_path CHECK (path <> '' AND path !~ '(^/|(^|/)\.\.(/|$))'),
    CONSTRAINT ck_skill_design_draft_files_size CHECK (size_bytes >= 0 AND size_bytes <= 104857600),
    CONSTRAINT ck_skill_design_draft_files_content_size CHECK (size_bytes = octet_length(content)),
    CONSTRAINT ck_skill_design_draft_files_sha256 CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_skill_design_draft_files_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES skill_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE
);

CREATE TRIGGER trg_skill_design_sessions_updated_at BEFORE UPDATE ON skill_design_sessions FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_skill_design_operations_updated_at BEFORE UPDATE ON skill_design_operations FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_skill_design_draft_files_updated_at BEFORE UPDATE ON skill_design_draft_files FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();



ALTER TABLE credentials ADD CONSTRAINT uq_credentials_project_asset_id UNIQUE (project_id, id);

CREATE TABLE project_channel_credential_bindings (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    channel_instance_id UUID NOT NULL,
    credential_id UUID NOT NULL,
    credential_version_id UUID NOT NULL,
    binding_revision BIGINT NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    CONSTRAINT pk_project_channel_credential_bindings PRIMARY KEY (id),
    CONSTRAINT ck_project_channel_credential_bindings_revision CHECK (binding_revision >= 1),
    CONSTRAINT ck_project_channel_credential_bindings_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_project_channel_credential_bindings_revocation CHECK ((status = 'active' AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)),
    CONSTRAINT fk_project_channel_credential_bindings_instance FOREIGN KEY(project_id, channel_instance_id) REFERENCES project_channel_instances (project_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_channel_credential_bindings_project_credential FOREIGN KEY(project_id, credential_id) REFERENCES credentials (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_credential_bindings_credential_version FOREIGN KEY(credential_id, credential_version_id) REFERENCES credential_versions (credential_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_credential_bindings_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_channel_credential_bindings_revoker FOREIGN KEY(revoked_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_channel_credential_bindings_scope_id UNIQUE (project_id, channel_instance_id, id)
);

CREATE UNIQUE INDEX uq_project_channel_credential_bindings_active_instance ON project_channel_credential_bindings (project_id, channel_instance_id) WHERE status = 'active';

CREATE INDEX ix_project_channel_credential_bindings_credential ON project_channel_credential_bindings (project_id, credential_id, credential_version_id, status);

CREATE TABLE project_skill_credential_configs (
    project_id UUID NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_project_skill_credential_configs PRIMARY KEY (project_id, skill_id, skill_version_id),
    CONSTRAINT ck_project_skill_credential_configs_revision CHECK (revision >= 1),
    CONSTRAINT fk_project_skill_credential_configs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_configs_skill FOREIGN KEY(skill_id) REFERENCES skills (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_credential_configs_skill_version FOREIGN KEY(skill_id, skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_credential_configs_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_configs_updater FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_skill_credential_configs_revision UNIQUE (project_id, skill_id, skill_version_id, revision)
);

CREATE TRIGGER trg_project_skill_credential_configs_updated_at BEFORE UPDATE ON project_skill_credential_configs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE INDEX ix_project_skill_credential_configs_skill_version ON project_skill_credential_configs (skill_id, skill_version_id);

CREATE TABLE project_skill_credential_bindings (
    id UUID NOT NULL,
    project_id UUID NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    secret_name VARCHAR(255) NOT NULL,
    credential_id UUID NOT NULL,
    credential_version_id UUID NOT NULL,
    config_revision BIGINT NOT NULL,
    status VARCHAR(16) DEFAULT 'active' NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    revoked_at TIMESTAMP WITH TIME ZONE,
    revoked_by_user_id VARCHAR(36),
    source_env_field_name VARCHAR(255) NOT NULL,
    admission_only BOOLEAN DEFAULT false NOT NULL,
    runtime_authority_binding_id UUID,
    CONSTRAINT pk_project_skill_credential_bindings PRIMARY KEY (id),
    CONSTRAINT ck_project_skill_credential_bindings_secret_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT ck_project_skill_credential_bindings_source_env_field_name CHECK (length(source_env_field_name) BETWEEN 1 AND 255),
    CONSTRAINT ck_project_skill_credential_bindings_revision CHECK (config_revision >= 1),
    CONSTRAINT ck_project_skill_credential_bindings_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_project_skill_credential_bindings_revocation CHECK ((status = 'active' AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)),
    CONSTRAINT ck_project_skill_credential_bindings_runtime_authority CHECK (runtime_authority_binding_id IS NULL OR admission_only = true),
    CONSTRAINT fk_project_skill_credential_bindings_skill FOREIGN KEY(skill_id) REFERENCES skills (id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_credential_bindings_runtime_authority FOREIGN KEY(runtime_authority_binding_id) REFERENCES project_skill_credential_bindings (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_credential_version FOREIGN KEY(credential_id, credential_version_id) REFERENCES credential_versions (credential_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_project_credential FOREIGN KEY(project_id, credential_id) REFERENCES credentials (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_revoker FOREIGN KEY(revoked_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_skill_credential_bindings_scope_id UNIQUE (project_id, skill_id, skill_version_id, id)
);

CREATE UNIQUE INDEX uq_project_skill_credential_bindings_active_name ON project_skill_credential_bindings (project_id, skill_id, skill_version_id, secret_name) WHERE status = 'active' AND admission_only = false;

CREATE INDEX ix_project_skill_credential_bindings_credential ON project_skill_credential_bindings (credential_id, credential_version_id, status);

CREATE INDEX ix_project_skill_credential_bindings_config ON project_skill_credential_bindings (project_id, skill_id, skill_version_id);

CREATE INDEX ix_project_skill_credential_bindings_skill_version ON project_skill_credential_bindings (skill_id, skill_version_id);

CREATE INDEX ix_project_skill_credential_bindings_project_credential ON project_skill_credential_bindings (project_id, credential_id);

CREATE OR REPLACE FUNCTION enforce_live_skill_credential_binding_target()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.admission_only = false AND NOT EXISTS (
        SELECT 1
        FROM project_skill_credential_configs config
        JOIN skill_versions version
          ON version.skill_id = config.skill_id
         AND version.id = config.skill_version_id
        WHERE config.project_id = NEW.project_id
          AND config.skill_id = NEW.skill_id
          AND config.skill_version_id = NEW.skill_version_id
    ) THEN
        RAISE EXCEPTION 'live Skill credential binding target unavailable'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.runtime_authority_binding_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM project_skill_credential_bindings authority
        WHERE authority.id = NEW.runtime_authority_binding_id
          AND authority.project_id = NEW.project_id
          AND authority.skill_id = NEW.skill_id
          AND authority.secret_name = NEW.secret_name
          AND authority.admission_only = false
    ) THEN
        RAISE EXCEPTION 'retired Skill credential runtime authority unavailable'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_project_skill_credential_bindings_live_target
BEFORE INSERT OR UPDATE OF project_id, skill_id, skill_version_id, secret_name, admission_only, runtime_authority_binding_id
ON project_skill_credential_bindings
FOR EACH ROW EXECUTE FUNCTION enforce_live_skill_credential_binding_target();

CREATE OR REPLACE FUNCTION protect_live_skill_credential_binding_target()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_TABLE_NAME = 'project_skill_credential_configs' THEN
        IF EXISTS (
            SELECT 1 FROM project_skill_credential_bindings binding
            WHERE binding.project_id = OLD.project_id
              AND binding.skill_id = OLD.skill_id
              AND binding.skill_version_id = OLD.skill_version_id
              AND binding.admission_only = false
        ) THEN
            RAISE EXCEPTION 'live Skill credential config is referenced'
                USING ERRCODE = '23503';
        END IF;
    ELSIF EXISTS (
        SELECT 1 FROM project_skill_credential_bindings binding
        WHERE binding.skill_id = OLD.skill_id
          AND binding.skill_version_id = OLD.id
          AND binding.admission_only = false
    ) THEN
        RAISE EXCEPTION 'live Skill credential version is referenced'
            USING ERRCODE = '23503';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_project_skill_credential_configs_live_binding
BEFORE DELETE OR UPDATE OF project_id, skill_id, skill_version_id
ON project_skill_credential_configs
FOR EACH ROW EXECUTE FUNCTION protect_live_skill_credential_binding_target();

CREATE TRIGGER trg_skill_versions_live_credential_binding
BEFORE DELETE OR UPDATE OF id, skill_id
ON skill_versions
FOR EACH ROW EXECUTE FUNCTION protect_live_skill_credential_binding_target();

CREATE TABLE run_skill_credential_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    skill_id UUID NOT NULL,
    skill_version_id UUID NOT NULL,
    secret_name VARCHAR(255) NOT NULL,
    skill_credential_binding_id UUID NOT NULL,
    binding_revision BIGINT NOT NULL,
    credential_id UUID NOT NULL,
    credential_version_id UUID NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    source_env_field_name VARCHAR(255) NOT NULL,
    CONSTRAINT pk_run_skill_credential_snapshots PRIMARY KEY (project_id, owner_user_id, run_id, skill_version_id, secret_name),
    CONSTRAINT ck_run_skill_credential_snapshots_secret_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT ck_run_skill_credential_snapshots_source_env_field_name CHECK (length(source_env_field_name) BETWEEN 1 AND 255),
    CONSTRAINT ck_run_skill_credential_snapshots_binding_revision CHECK (binding_revision >= 1),
    CONSTRAINT fk_run_skill_credential_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_credential_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_credential_snapshots_project_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_skill_credential_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE
);

CREATE INDEX ix_run_skill_credential_snapshots_binding ON run_skill_credential_snapshots (skill_credential_binding_id);

CREATE INDEX ix_run_skill_credential_snapshots_private_run ON run_skill_credential_snapshots (project_id, owner_user_id, thread_id, run_id);

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
    current_version_id UUID,
    revision BIGINT DEFAULT 1 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_model_configs_status CHECK (status IN ('active', 'suspended')),
    CONSTRAINT ck_system_model_configs_revision CHECK (revision >= 1),
    CONSTRAINT uq_system_model_configs_id_current_version UNIQUE (id, current_version_id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_system_model_configs_status_created ON system_model_configs (status, created_at DESC, id DESC);

CREATE TABLE system_model_config_versions (
    id UUID NOT NULL,
    model_config_id UUID NOT NULL,
    version_number BIGINT NOT NULL,
    provider_adapter VARCHAR(64) NOT NULL,
    provider_model VARCHAR(255) NOT NULL,
    settings JSONB DEFAULT '{}'::jsonb NOT NULL,
    supports_thinking BOOLEAN DEFAULT false NOT NULL,
    supports_reasoning_effort BOOLEAN DEFAULT false NOT NULL,
    supports_vision BOOLEAN DEFAULT false NOT NULL,
    credential_id UUID,
    credential_version_id UUID,
    credential_env_key VARCHAR(255),
    payload_checksum CHAR(64) NOT NULL,
    supersedes_version_id UUID,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_model_config_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_system_model_config_versions_settings_object CHECK (jsonb_typeof(settings) = 'object'),
    CONSTRAINT ck_system_model_config_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_system_model_config_versions_credential_group CHECK ((credential_id IS NULL AND credential_version_id IS NULL AND credential_env_key IS NULL) OR (credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND credential_env_key IS NOT NULL)),
    CONSTRAINT ck_system_model_config_versions_env_key CHECK (credential_env_key IS NULL OR credential_env_key ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT uq_system_model_config_versions_number UNIQUE (model_config_id, version_number),
    CONSTRAINT uq_system_model_config_versions_model_id UNIQUE (model_config_id, id),
    CONSTRAINT uq_system_model_config_versions_exact UNIQUE (model_config_id, id, payload_checksum),
    CONSTRAINT uq_system_model_config_versions_snapshot_closure UNIQUE (model_config_id, id, payload_checksum, credential_id, credential_version_id, credential_env_key),
    CONSTRAINT fk_system_model_config_versions_credential_version FOREIGN KEY(credential_id, credential_version_id) REFERENCES credential_versions (credential_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_system_model_config_versions_supersedes FOREIGN KEY(model_config_id, supersedes_version_id) REFERENCES system_model_config_versions (model_config_id, id) ON DELETE RESTRICT,
    FOREIGN KEY(model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_system_model_config_versions_credential ON system_model_config_versions (credential_id, credential_version_id);

ALTER TABLE system_model_configs ADD CONSTRAINT fk_system_model_configs_current_version FOREIGN KEY(id, current_version_id) REFERENCES system_model_config_versions (model_config_id, id) ON DELETE RESTRICT;

ALTER TABLE system_model_catalog_state ADD CONSTRAINT fk_system_model_catalog_state_default_model FOREIGN KEY(default_model_config_id) REFERENCES system_model_configs (id) ON DELETE RESTRICT;

CREATE TABLE run_model_config_snapshots (
    project_id UUID NOT NULL,
    owner_user_id VARCHAR(36) NOT NULL,
    thread_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    purpose VARCHAR(64) NOT NULL,
    model_config_id UUID NOT NULL,
    model_config_version_id UUID NOT NULL,
    payload_checksum CHAR(64) NOT NULL,
    credential_id UUID,
    credential_version_id UUID,
    credential_env_key VARCHAR(255),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (project_id, owner_user_id, run_id, purpose),
    CONSTRAINT ck_run_model_config_snapshots_purpose CHECK (purpose ~ '^[a-z][a-z0-9._-]{0,63}$'),
    CONSTRAINT ck_run_model_config_snapshots_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_run_model_config_snapshots_credential_group CHECK ((credential_id IS NULL AND credential_version_id IS NULL AND credential_env_key IS NULL) OR (credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND credential_env_key IS NOT NULL)),
    CONSTRAINT ck_run_model_config_snapshots_env_key CHECK (credential_env_key IS NULL OR credential_env_key ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT fk_run_model_config_snapshots_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_private_run FOREIGN KEY(project_id, owner_user_id, thread_id, run_id) REFERENCES runs (project_id, owner_user_id, thread_id, run_id) ON DELETE CASCADE,
    CONSTRAINT fk_run_model_config_snapshots_exact_model FOREIGN KEY(model_config_id, model_config_version_id, payload_checksum) REFERENCES system_model_config_versions (model_config_id, id, payload_checksum) ON DELETE RESTRICT,
    CONSTRAINT fk_run_model_config_snapshots_model_credential FOREIGN KEY(model_config_id, model_config_version_id, payload_checksum, credential_id, credential_version_id, credential_env_key) REFERENCES system_model_config_versions (model_config_id, id, payload_checksum, credential_id, credential_version_id, credential_env_key) ON DELETE RESTRICT
);

CREATE INDEX ix_run_model_config_snapshots_credential ON run_model_config_snapshots (credential_id, credential_version_id);

CREATE INDEX ix_run_model_config_snapshots_model_version ON run_model_config_snapshots (model_config_id, model_config_version_id);

CREATE INDEX ix_run_model_config_snapshots_private_run ON run_model_config_snapshots (project_id, owner_user_id, thread_id, run_id);

CREATE OR REPLACE FUNCTION enforce_run_model_snapshot_credential_closure()
RETURNS TRIGGER AS $$
DECLARE
    expected_credential_id UUID;
    expected_credential_version_id UUID;
    expected_credential_env_key VARCHAR(255);
BEGIN
    SELECT version.credential_id,
           version.credential_version_id,
           version.credential_env_key
      INTO expected_credential_id,
           expected_credential_version_id,
           expected_credential_env_key
      FROM system_model_config_versions AS version
     WHERE version.model_config_id = NEW.model_config_id
       AND version.id = NEW.model_config_version_id
       AND version.payload_checksum = NEW.payload_checksum;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'run model snapshot exact model unavailable'
            USING ERRCODE = '23503';
    END IF;

    IF ROW(
        NEW.credential_id,
        NEW.credential_version_id,
        NEW.credential_env_key
    ) IS DISTINCT FROM ROW(
        expected_credential_id,
        expected_credential_version_id,
        expected_credential_env_key
    ) THEN
        RAISE EXCEPTION 'run model snapshot credential closure mismatch'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

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

CREATE TRIGGER trg_system_model_catalog_state_updated_at BEFORE UPDATE ON system_model_catalog_state FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_system_model_configs_updated_at BEFORE UPDATE ON system_model_configs FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_system_model_config_versions_immutable BEFORE UPDATE OR DELETE ON system_model_config_versions FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_run_model_config_snapshots_credential_closure BEFORE INSERT ON run_model_config_snapshots FOR EACH ROW EXECUTE FUNCTION enforce_run_model_snapshot_credential_closure();

CREATE TRIGGER trg_run_model_config_snapshots_immutable BEFORE UPDATE OR DELETE ON run_model_config_snapshots FOR EACH ROW EXECUTE FUNCTION reject_direct_run_model_snapshot_mutation();

INSERT INTO system_model_catalog_state (id, revision) VALUES (1, 1);

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

ALTER TABLE system_runtime_policies ADD CONSTRAINT fk_system_runtime_policies_current_version FOREIGN KEY(section, current_version_id) REFERENCES system_runtime_policy_versions (section, id) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

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

CREATE TRIGGER trg_system_runtime_policy_catalog_state_updated_at BEFORE UPDATE ON system_runtime_policy_catalog_state FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_system_runtime_policies_updated_at BEFORE UPDATE ON system_runtime_policies FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_system_runtime_policy_versions_immutable BEFORE UPDATE OR DELETE ON system_runtime_policy_versions FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_run_runtime_policy_snapshots_immutable BEFORE UPDATE OR DELETE ON run_runtime_policy_snapshots FOR EACH ROW EXECUTE FUNCTION reject_direct_run_runtime_policy_snapshot_mutation();

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

CREATE TRIGGER trg_memory_documents_sections_immutable
BEFORE UPDATE ON memory_documents
FOR EACH ROW EXECUTE FUNCTION prevent_memory_document_sections_mutation();

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
    summary_model_ref UUID,
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
    CONSTRAINT fk_memory_history_entries_summary_model FOREIGN KEY(summary_model_ref) REFERENCES system_model_config_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_history_entries_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_history_entries_origin CHECK (origin IN ('snip', 'tool')),
    CONSTRAINT ck_memory_history_entries_origin_source CHECK ((origin = 'snip' AND source_run_id IS NULL AND source_checkpoint_id IS NOT NULL AND source_checkpoint_id <> '' AND committed_checkpoint_id IS NOT NULL AND committed_checkpoint_id <> '' AND summary_model_ref IS NOT NULL) OR (origin = 'tool' AND source_run_id IS NOT NULL AND source_run_id <> '' AND source_checkpoint_id IS NULL AND committed_checkpoint_id IS NULL AND summary_model_ref IS NULL)),
    CONSTRAINT ck_memory_history_entries_digests CHECK (source_digest ~ '^[0-9a-f]{64}$' AND content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_history_entries_preference_version CHECK (preference_version >= 1),
    CONSTRAINT ck_memory_history_entries_contract CHECK ((origin = 'snip' AND snip_prompt_version <> '') OR (origin = 'tool' AND snip_prompt_version = 'remember-tool-v1')),
    CONSTRAINT ck_memory_history_entries_text_size CHECK (tagged_text IS NULL OR char_length(tagged_text) <= 1000),
    CONSTRAINT ck_memory_history_entries_lifecycle CHECK ((status = 'pending' AND tagged_text IS NOT NULL AND dream_job_id IS NULL AND consumed_at IS NULL) OR (status = 'processing' AND tagged_text IS NOT NULL AND dream_job_id IS NOT NULL AND consumed_at IS NULL) OR (status = 'consumed' AND tagged_text IS NULL AND dream_job_id IS NOT NULL AND consumed_at IS NOT NULL))
);

CREATE INDEX ix_memory_history_entries_dream_job ON memory_history_entries (dream_job_id, sequence) WHERE dream_job_id IS NOT NULL;

CREATE INDEX ix_memory_history_entries_pending ON memory_history_entries (project_id, owner_user_id, namespace, sequence) WHERE status = 'pending';

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
    model_ref UUID NOT NULL,
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
    CONSTRAINT fk_memory_dream_runs_model FOREIGN KEY(model_ref) REFERENCES system_model_config_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_dream_runs_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_dream_runs_trigger CHECK (trigger IN ('auto_dream', 'manual_dream', 'budget_rewrite')),
    CONSTRAINT ck_memory_dream_runs_history CHECK ((trigger = 'budget_rewrite' AND history_count = 0 AND history_from IS NULL AND history_to IS NULL) OR (trigger IN ('auto_dream', 'manual_dream') AND history_count BETWEEN 1 AND 20 AND history_from >= 1 AND history_to >= history_from)),
    CONSTRAINT ck_memory_dream_runs_digests CHECK (history_digest ~ '^[0-9a-f]{64}$' AND base_content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_dream_runs_versions CHECK (base_document_version >= 0 AND preference_version >= 1 AND policy_revision >= 1),
    CONSTRAINT ck_memory_dream_runs_contract CHECK (prompt_version <> ''),
    CONSTRAINT ck_memory_dream_runs_result CHECK ((result_version IS NULL AND completed_at IS NULL) OR (result_version >= 1 AND completed_at IS NOT NULL))
);

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
    CONSTRAINT ck_memory_dream_prepare_runs_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_dream_prepare_runs_request CHECK (request_id <> ''),
    CONSTRAINT ck_memory_dream_prepare_runs_phase CHECK (phase IN ('queued', 'draining', 'verifying', 'dream_admitted', 'succeeded', 'cancelled', 'failed')),
    CONSTRAINT ck_memory_dream_prepare_runs_disposition CHECK (result_disposition IN ('queued', 'already_running', 'nothing_pending', 'cancelled', 'failed')),
    CONSTRAINT ck_memory_dream_prepare_runs_passes CHECK (compacted_passes >= 0),
    CONSTRAINT ck_memory_dream_prepare_runs_terminal CHECK ((phase IN ('succeeded', 'cancelled', 'failed')) = (completed_at IS NOT NULL)),
    CONSTRAINT ck_memory_dream_prepare_runs_child CHECK ((dream_job_id IS NULL AND admission_kind IS NULL AND (history_count IS NULL OR (result_disposition = 'nothing_pending' AND history_count = 0))) OR (dream_job_id IS NOT NULL AND history_count BETWEEN 0 AND 20 AND admission_kind IN ('history', 'budget_rewrite'))),
    CONSTRAINT ck_memory_dream_prepare_runs_admission_kind CHECK ((admission_kind = 'budget_rewrite') = (dream_job_id IS NOT NULL AND history_count = 0)),
    CONSTRAINT uq_memory_dream_prepare_runs_job_scope UNIQUE (job_id, project_id, owner_user_id, namespace),
    CONSTRAINT uq_memory_dream_prepare_runs_operation UNIQUE (project_id, owner_user_id, operation_id),
    CONSTRAINT fk_memory_dream_prepare_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_job FOREIGN KEY(job_id, project_id, owner_user_id, namespace) REFERENCES jobs (id, project_id, owner_user_id, namespace) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_thread FOREIGN KEY(project_id, owner_user_id, thread_id) REFERENCES threads_meta (project_id, owner_user_id, thread_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_dream_prepare_runs_dream FOREIGN KEY(dream_job_id, project_id, owner_user_id, namespace) REFERENCES memory_dream_runs (job_id, project_id, owner_user_id, namespace) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX uq_memory_dream_prepare_runs_active_thread ON memory_dream_prepare_runs (project_id, owner_user_id, thread_id) WHERE completed_at IS NULL;

CREATE INDEX ix_memory_dream_prepare_runs_scope_updated ON memory_dream_prepare_runs (project_id, owner_user_id, updated_at DESC, job_id DESC);

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
    model_ref UUID,
    needs_review BOOLEAN DEFAULT false NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT pk_memory_document_versions PRIMARY KEY (project_id, owner_user_id, namespace, version),
    CONSTRAINT fk_memory_document_versions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_document_versions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_document_versions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_memory_document_versions_document FOREIGN KEY(project_id, owner_user_id, namespace) REFERENCES memory_documents (project_id, owner_user_id, namespace) ON DELETE CASCADE,
    CONSTRAINT fk_memory_document_versions_dream_run FOREIGN KEY(dream_job_id, project_id, owner_user_id, namespace) REFERENCES memory_dream_runs (job_id, project_id, owner_user_id, namespace) ON DELETE CASCADE,
    CONSTRAINT fk_memory_document_versions_model FOREIGN KEY(model_ref) REFERENCES system_model_config_versions (id) ON DELETE RESTRICT,
    CONSTRAINT ck_memory_document_versions_namespace CHECK (namespace <> ''),
    CONSTRAINT ck_memory_document_versions_version CHECK (version >= 1),
    CONSTRAINT ck_memory_document_versions_digest CHECK (content_digest ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_memory_document_versions_content_size CHECK (char_length(content) <= 16000),
    CONSTRAINT ck_memory_document_versions_trigger CHECK (trigger IN ('auto_dream', 'manual_dream', 'budget_rewrite', 'restore')),
    CONSTRAINT ck_memory_document_versions_source CHECK ((trigger = 'restore' AND dream_job_id IS NULL AND history_from IS NULL AND history_to IS NULL AND history_count IS NULL AND prompt_version IS NULL AND model_ref IS NULL) OR (trigger = 'budget_rewrite' AND dream_job_id IS NOT NULL AND history_from IS NULL AND history_to IS NULL AND history_count = 0 AND prompt_version IS NOT NULL AND prompt_version <> '' AND model_ref IS NOT NULL) OR (trigger IN ('auto_dream', 'manual_dream') AND dream_job_id IS NOT NULL AND history_from >= 1 AND history_to >= history_from AND history_count BETWEEN 1 AND 20 AND prompt_version IS NOT NULL AND prompt_version <> '' AND model_ref IS NOT NULL))
);

CREATE UNIQUE INDEX uq_memory_document_versions_dream_job ON memory_document_versions (dream_job_id) WHERE dream_job_id IS NOT NULL;

ALTER TABLE memory_dream_runs ADD CONSTRAINT fk_memory_dream_runs_result_version FOREIGN KEY(project_id, owner_user_id, namespace, result_version) REFERENCES memory_document_versions (project_id, owner_user_id, namespace, version) DEFERRABLE INITIALLY DEFERRED;

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

CREATE TRIGGER trg_run_memory_context_snapshots_sections_immutable
BEFORE UPDATE ON run_memory_context_snapshots
FOR EACH ROW EXECUTE FUNCTION prevent_run_memory_snapshot_sections_mutation();

-- BEGIN GENERATED SCHEMA COMMENTS
-- Generated by backend/scripts/generate_schema_comments.py; DO NOT EDIT.
-- Source: full_schema.sql
-- Coverage: 91 static tables and 1121 columns.
-- Comments describe schema purpose only; they contain no runtime or secret values.

COMMENT ON TABLE alembic_version IS '记录当前数据库采用的 Alembic 架构版本。';
COMMENT ON COLUMN alembic_version.version_num IS '数据库迁移版本：数据库迁移版本号。';

COMMENT ON TABLE asset_catalog_state IS '记录系统资产目录的单例代次与更新时间。';
COMMENT ON COLUMN asset_catalog_state.id IS '资产目录状态：主键标识。';
COMMENT ON COLUMN asset_catalog_state.generation IS '资产目录状态：单调代次。';
COMMENT ON COLUMN asset_catalog_state.updated_at IS '资产目录状态：记录最近更新时间。';

COMMENT ON TABLE system_asset_upgrade_audit IS '记录软件包升级原子替换 System Agent 或 Skill Current v1 的校验和证据。';
COMMENT ON COLUMN system_asset_upgrade_audit.id IS '系统资产升级审计：主键标识。';
COMMENT ON COLUMN system_asset_upgrade_audit.asset_kind IS '系统资产升级审计：资产类型。';
COMMENT ON COLUMN system_asset_upgrade_audit.asset_id IS '系统资产升级审计：资产标识。';
COMMENT ON COLUMN system_asset_upgrade_audit.version_id IS '系统资产升级审计：版本标识。';
COMMENT ON COLUMN system_asset_upgrade_audit.before_checksum IS '系统资产升级审计：升级前载荷校验和。';
COMMENT ON COLUMN system_asset_upgrade_audit.after_checksum IS '系统资产升级审计：升级后载荷校验和。';
COMMENT ON COLUMN system_asset_upgrade_audit.package_digest IS '系统资产升级审计：升级软件包目录摘要。';
COMMENT ON COLUMN system_asset_upgrade_audit.operator_identity IS '系统资产升级审计：执行数据库升级的操作主体身份。';
COMMENT ON COLUMN system_asset_upgrade_audit.occurred_at IS '系统资产升级审计：发生时间。';

COMMENT ON TABLE dead_jobs IS '保存超过重试边界或无法安全重试的后台任务终态。';
COMMENT ON COLUMN dead_jobs.job_id IS '死信任务：任务标识。';
COMMENT ON COLUMN dead_jobs.project_id IS '死信任务：所属项目标识。';
COMMENT ON COLUMN dead_jobs.owner_ref_key_id IS '死信任务：所有者引用键标识。';
COMMENT ON COLUMN dead_jobs.owner_ref_hmac IS '死信任务：所有者引用的域分离 HMAC。';
COMMENT ON COLUMN dead_jobs.job_type IS '死信任务：任务类型。';
COMMENT ON COLUMN dead_jobs.attempt_count IS '死信任务：尝试数量。';
COMMENT ON COLUMN dead_jobs.retry_safety IS '死信任务：重试安全性。';
COMMENT ON COLUMN dead_jobs.public_error_code IS '死信任务：可公开的稳定错误代码。';
COMMENT ON COLUMN dead_jobs.dead_at IS '死信任务：死信时间。';

COMMENT ON TABLE jobs IS '保存 Worker 可领取、续租、重试和结算的持久化任务。';
COMMENT ON COLUMN jobs.id IS '后台任务：主键标识。';
COMMENT ON COLUMN jobs.job_type IS '后台任务：任务类型。';
COMMENT ON COLUMN jobs.project_id IS '后台任务：所属项目标识。';
COMMENT ON COLUMN jobs.owner_user_id IS '后台任务：私有数据所有者的用户标识。';
COMMENT ON COLUMN jobs.namespace IS '后台任务：私有数据命名空间。';
COMMENT ON COLUMN jobs.run_id IS '后台任务：运行标识。';
COMMENT ON COLUMN jobs.automation_occurrence_id IS '后台任务：自动化触发实例标识。';
COMMENT ON COLUMN jobs.predecessor_dead_job_id IS '后台任务：前序死信任务标识。';
COMMENT ON COLUMN jobs.origin_trace_id IS '后台任务：跨组件关联运行的追踪标识。';
COMMENT ON COLUMN jobs.idempotency_key IS '后台任务：幂等操作键。';
COMMENT ON COLUMN jobs.status IS '后台任务：生命周期状态。';
COMMENT ON COLUMN jobs.priority IS '后台任务：任务领取优先级。';
COMMENT ON COLUMN jobs.available_at IS '后台任务：可用时间。';
COMMENT ON COLUMN jobs.attempt_count IS '后台任务：尝试数量。';
COMMENT ON COLUMN jobs.max_attempts IS '后台任务：允许的最大执行尝试次数。';
COMMENT ON COLUMN jobs.lease_owner_id IS '后台任务：租约所有者标识。';
COMMENT ON COLUMN jobs.lease_token_hash IS '后台任务：执行租约令牌的不可逆哈希。';
COMMENT ON COLUMN jobs.lease_expires_at IS '后台任务：租约过期时间。';
COMMENT ON COLUMN jobs.heartbeat_at IS '后台任务：心跳时间。';
COMMENT ON COLUMN jobs.retry_safety IS '后台任务：重试安全性。';
COMMENT ON COLUMN jobs.public_error_code IS '后台任务：可公开的稳定错误代码。';
COMMENT ON COLUMN jobs.cancel_requested_at IS '后台任务：取消请求时间。';
COMMENT ON COLUMN jobs.cancel_reason IS '后台任务：取消原因。';
COMMENT ON COLUMN jobs.created_at IS '后台任务：记录创建时间。';
COMMENT ON COLUMN jobs.started_at IS '后台任务：开始时间。';
COMMENT ON COLUMN jobs.completed_at IS '后台任务：完成时间。';
COMMENT ON COLUMN jobs.updated_at IS '后台任务：记录最近更新时间。';
COMMENT ON COLUMN jobs.execution_domain_affinity IS '后台任务：限制本机命令续接任务的执行域亲和摘要。';

COMMENT ON TABLE project_invitation_rate_limits IS '记录项目邀请码失败尝试的限流窗口。';
COMMENT ON COLUMN project_invitation_rate_limits.key_hash IS '项目邀请限流：键不可逆哈希。';
COMMENT ON COLUMN project_invitation_rate_limits.failure_count IS '项目邀请限流：失败数量。';
COMMENT ON COLUMN project_invitation_rate_limits.window_started_at IS '项目邀请限流：窗口开始时间。';
COMMENT ON COLUMN project_invitation_rate_limits.expires_at IS '项目邀请限流：过期时间。';
COMMENT ON COLUMN project_invitation_rate_limits.updated_at IS '项目邀请限流：记录最近更新时间。';

COMMENT ON TABLE runs IS '保存一次智能体运行的身份、状态、用量与执行租约。';
COMMENT ON COLUMN runs.run_id IS '智能体运行：运行标识。';
COMMENT ON COLUMN runs.thread_id IS '智能体运行：线程标识。';
COMMENT ON COLUMN runs.assistant_id IS '智能体运行：助手标识。';
COMMENT ON COLUMN runs.owner_user_id IS '智能体运行：私有数据所有者的用户标识。';
COMMENT ON COLUMN runs.status IS '智能体运行：生命周期状态。';
COMMENT ON COLUMN runs.model_name IS '智能体运行：模型名称。';
COMMENT ON COLUMN runs.multitask_strategy IS '智能体运行：多任务策略。';
COMMENT ON COLUMN runs.metadata_json IS '智能体运行：非敏感业务元数据。';
COMMENT ON COLUMN runs.kwargs_json IS '智能体运行：冻结的运行关键字参数。';
COMMENT ON COLUMN runs.origin_trace_id IS '智能体运行：跨组件关联运行的追踪标识。';
COMMENT ON COLUMN runs.error IS '智能体运行：内部运行错误详情。';
COMMENT ON COLUMN runs.message_count IS '智能体运行：消息数量。';
COMMENT ON COLUMN runs.first_human_message IS '智能体运行：首条用户消息文本的截断副本（最多 2000 字符，属于私有内容）。';
COMMENT ON COLUMN runs.last_ai_message IS '智能体运行：末条主智能体可展示消息文本的截断副本（最多 2000 字符，属于私有内容）。';
COMMENT ON COLUMN runs.total_input_tokens IS '智能体运行：累计输入令牌数量。';
COMMENT ON COLUMN runs.total_output_tokens IS '智能体运行：累计输出令牌数量。';
COMMENT ON COLUMN runs.total_tokens IS '智能体运行：累计令牌总量。';
COMMENT ON COLUMN runs.llm_call_count IS '智能体运行：大模型调用数量。';
COMMENT ON COLUMN runs.lead_agent_tokens IS '智能体运行：主智能体消耗的令牌数量。';
COMMENT ON COLUMN runs.subagent_tokens IS '智能体运行：子智能体消耗的令牌数量。';
COMMENT ON COLUMN runs.middleware_tokens IS '智能体运行：中间件消耗的令牌数量。';
COMMENT ON COLUMN runs.token_usage_by_model IS '智能体运行：按模型汇总的令牌用量。';
COMMENT ON COLUMN runs.follow_up_to_run_id IS '智能体运行：被当前运行跟进的前序运行标识。';
COMMENT ON COLUMN runs.created_at IS '智能体运行：记录创建时间。';
COMMENT ON COLUMN runs.updated_at IS '智能体运行：记录最近更新时间。';
COMMENT ON COLUMN runs.project_id IS '智能体运行：所属项目标识。';
COMMENT ON COLUMN runs.job_id IS '智能体运行：任务标识。';
COMMENT ON COLUMN runs.execution_lease_token_hash IS '智能体运行：运行租约令牌的不可逆哈希。';
COMMENT ON COLUMN runs.execution_lease_expires_at IS '智能体运行：执行租约过期时间。';
COMMENT ON COLUMN runs.execution_heartbeat_at IS '智能体运行：执行心跳时间。';
COMMENT ON COLUMN runs.execution_started_at IS '智能体运行：执行开始时间。';
COMMENT ON COLUMN runs.cancel_requested_at IS '智能体运行：取消请求时间。';
COMMENT ON COLUMN runs.cancel_reason IS '智能体运行：取消原因。';
COMMENT ON COLUMN runs.authorization_cancel_requested_at IS '智能体运行：授权取消请求时间。';
COMMENT ON COLUMN runs.authorization_cancel_reason IS '智能体运行：授权取消原因。';
COMMENT ON COLUMN runs.finalization_status IS '智能体运行：收尾状态。';

COMMENT ON TABLE scheduled_task_runs IS '保存自动化任务每次计划或手动触发的运行记录。';
COMMENT ON COLUMN scheduled_task_runs.id IS '调度任务运行：主键标识。';
COMMENT ON COLUMN scheduled_task_runs.project_id IS '调度任务运行：所属项目标识。';
COMMENT ON COLUMN scheduled_task_runs.owner_user_id IS '调度任务运行：私有数据所有者的用户标识。';
COMMENT ON COLUMN scheduled_task_runs.task_id IS '调度任务运行：任务标识。';
COMMENT ON COLUMN scheduled_task_runs.task_version IS '调度任务运行：任务版本号。';
COMMENT ON COLUMN scheduled_task_runs.occurrence_key IS '调度任务运行：触发实例键。';
COMMENT ON COLUMN scheduled_task_runs.manual_idempotency_hash IS '调度任务运行：手动触发幂等键的不可逆哈希。';
COMMENT ON COLUMN scheduled_task_runs.scheduled_for IS '调度任务运行：本次任务计划触发时间。';
COMMENT ON COLUMN scheduled_task_runs.trigger IS '调度任务运行：触发方式。';
COMMENT ON COLUMN scheduled_task_runs.status IS '调度任务运行：生命周期状态。';
COMMENT ON COLUMN scheduled_task_runs.thread_id IS '调度任务运行：线程标识。';
COMMENT ON COLUMN scheduled_task_runs.run_id IS '调度任务运行：运行标识。';
COMMENT ON COLUMN scheduled_task_runs.job_id IS '调度任务运行：任务标识。';
COMMENT ON COLUMN scheduled_task_runs.resolved_membership_id IS '调度任务运行：解析成员关系标识。';
COMMENT ON COLUMN scheduled_task_runs.resolved_membership_version IS '调度任务运行：解析成员关系版本号。';
COMMENT ON COLUMN scheduled_task_runs.launch_attempt_count IS '调度任务运行：启动尝试数量。';
COMMENT ON COLUMN scheduled_task_runs.lease_owner IS '调度任务运行：租约所有者。';
COMMENT ON COLUMN scheduled_task_runs.lease_expires_at IS '调度任务运行：租约过期时间。';
COMMENT ON COLUMN scheduled_task_runs.next_attempt_at IS '调度任务运行：下次尝试时间。';
COMMENT ON COLUMN scheduled_task_runs.error_code IS '调度任务运行：稳定错误代码。';
COMMENT ON COLUMN scheduled_task_runs.error_message IS '调度任务运行：受限的错误说明。';
COMMENT ON COLUMN scheduled_task_runs.started_at IS '调度任务运行：开始时间。';
COMMENT ON COLUMN scheduled_task_runs.finished_at IS '调度任务运行：完成时间。';
COMMENT ON COLUMN scheduled_task_runs.created_at IS '调度任务运行：记录创建时间。';
COMMENT ON COLUMN scheduled_task_runs.updated_at IS '调度任务运行：记录最近更新时间。';

COMMENT ON TABLE users IS '保存平台用户、渠道访客及其登录与偏好状态。';
COMMENT ON COLUMN users.id IS '用户：主键标识。';
COMMENT ON COLUMN users.email IS '用户：规范化邮箱地址。';
COMMENT ON COLUMN users.username IS '用户：登录用户名。';
COMMENT ON COLUMN users.password_hash IS '用户：密码验证哈希（不存储明文密码）。';
COMMENT ON COLUMN users.principal_type IS '用户：主体类型。';
COMMENT ON COLUMN users.system_role IS '用户：系统角色。';
COMMENT ON COLUMN users.created_at IS '用户：记录创建时间。';
COMMENT ON COLUMN users.oauth_provider IS '用户：OAuth提供方。';
COMMENT ON COLUMN users.oauth_id IS '用户：OAuth标识。';
COMMENT ON COLUMN users.needs_setup IS '用户：是否需要初始化。';
COMMENT ON COLUMN users.token_version IS '用户：令牌版本号。';
COMMENT ON COLUMN users.memory_enabled IS '用户：是否启用用户记忆功能。';
COMMENT ON COLUMN users.preferences_version IS '用户：偏好版本号。';

COMMENT ON TABLE auth_sessions IS '保存可撤销的用户认证会话及其有效期。';
COMMENT ON COLUMN auth_sessions.session_id_hash IS '认证会话：认证会话标识的不可逆哈希。';
COMMENT ON COLUMN auth_sessions.user_id IS '认证会话：用户标识。';
COMMENT ON COLUMN auth_sessions.created_at IS '认证会话：记录创建时间。';
COMMENT ON COLUMN auth_sessions.expires_at IS '认证会话：过期时间。';
COMMENT ON COLUMN auth_sessions.revoked_at IS '认证会话：撤销时间。';
COMMENT ON COLUMN auth_sessions.last_seen_at IS '认证会话：最近发现时间。';

COMMENT ON TABLE worker_nodes IS '记录 Worker 节点能力、容量与心跳状态。';
COMMENT ON COLUMN worker_nodes.id IS '工作节点：主键标识。';
COMMENT ON COLUMN worker_nodes.version IS '工作节点：记录版本号。';
COMMENT ON COLUMN worker_nodes.capabilities_json IS '工作节点：工作节点能力列表。';
COMMENT ON COLUMN worker_nodes.max_concurrent_jobs IS '工作节点：可并发执行的任务上限。';
COMMENT ON COLUMN worker_nodes.draining IS '工作节点：是否处于排空状态。';
COMMENT ON COLUMN worker_nodes.started_at IS '工作节点：开始时间。';
COMMENT ON COLUMN worker_nodes.heartbeat_at IS '工作节点：心跳时间。';

COMMENT ON TABLE job_attempts IS '记录后台任务每次领取和执行尝试的结算信息。';
COMMENT ON COLUMN job_attempts.id IS '任务尝试：主键标识。';
COMMENT ON COLUMN job_attempts.job_id IS '任务尝试：任务标识。';
COMMENT ON COLUMN job_attempts.attempt_number IS '任务尝试：尝试编号。';
COMMENT ON COLUMN job_attempts.worker_id IS '任务尝试：工作节点标识。';
COMMENT ON COLUMN job_attempts.lease_token_hash IS '任务尝试：执行租约令牌的不可逆哈希。';
COMMENT ON COLUMN job_attempts.started_at IS '任务尝试：开始时间。';
COMMENT ON COLUMN job_attempts.heartbeat_at IS '任务尝试：心跳时间。';
COMMENT ON COLUMN job_attempts.finished_at IS '任务尝试：完成时间。';
COMMENT ON COLUMN job_attempts.outcome IS '任务尝试：执行结果。';
COMMENT ON COLUMN job_attempts.public_error_code IS '任务尝试：可公开的稳定错误代码。';
COMMENT ON COLUMN job_attempts.checkpoint_cursor IS '任务尝试：检查点游标。';
COMMENT ON COLUMN job_attempts.stream_cursor IS '任务尝试：事件流游标。';

COMMENT ON TABLE projects IS '保存项目基本信息、生命周期与所有者治理状态。';
COMMENT ON COLUMN projects.id IS '项目：主键标识。';
COMMENT ON COLUMN projects.slug IS '项目：稳定可读标识名。';
COMMENT ON COLUMN projects.display_name IS '项目：展示名称。';
COMMENT ON COLUMN projects.description IS '项目：用途描述。';
COMMENT ON COLUMN projects.icon IS '项目：图标。';
COMMENT ON COLUMN projects.status IS '项目：生命周期状态。';
COMMENT ON COLUMN projects.deletion_requested_at IS '项目：删除请求时间。';
COMMENT ON COLUMN projects.deletion_effective_at IS '项目：删除生效时间。';
COMMENT ON COLUMN projects.deletion_requested_by_user_id IS '项目：请求删除的用户标识。';
COMMENT ON COLUMN projects.is_suspended IS '项目：是否暂停成员权限。';
COMMENT ON COLUMN projects.membership_version IS '项目：成员关系版本号。';
COMMENT ON COLUMN projects.created_by_user_id IS '项目：创建操作的用户标识。';
COMMENT ON COLUMN projects.created_at IS '项目：记录创建时间。';
COMMENT ON COLUMN projects.updated_at IS '项目：记录最近更新时间。';

COMMENT ON TABLE agents IS '保存智能体的逻辑身份和 Current Version 指针。';
COMMENT ON COLUMN agents.id IS '项目智能体：主键标识。';
COMMENT ON COLUMN agents.scope IS '项目智能体：资产或数据归属范围。';
COMMENT ON COLUMN agents.project_id IS '项目智能体：所属项目标识。';
COMMENT ON COLUMN agents.slug IS '项目智能体：稳定可读标识名。';
COMMENT ON COLUMN agents.display_name IS '项目智能体：展示名称。';
COMMENT ON COLUMN agents.status IS '项目智能体：生命周期状态。';
COMMENT ON COLUMN agents.current_version_id IS '项目智能体：当前版本标识。';
COMMENT ON COLUMN agents.revision IS '项目智能体：配置修订号。';
COMMENT ON COLUMN agents.source_key IS '项目智能体：来源键。';
COMMENT ON COLUMN agents.created_by_user_id IS '项目智能体：创建操作的用户标识。';
COMMENT ON COLUMN agents.created_at IS '项目智能体：记录创建时间。';
COMMENT ON COLUMN agents.updated_at IS '项目智能体：记录最近更新时间。';

COMMENT ON TABLE project_default_agents IS '保存项目范围内默认智能体的唯一绑定。';
COMMENT ON COLUMN project_default_agents.project_id IS '项目默认智能体：所属项目标识。';
COMMENT ON COLUMN project_default_agents.agent_asset_id IS '项目默认智能体：智能体资产标识。';
COMMENT ON COLUMN project_default_agents.revision IS '项目默认智能体：配置修订号。';
COMMENT ON COLUMN project_default_agents.created_by_user_id IS '项目默认智能体：创建操作的用户标识。';
COMMENT ON COLUMN project_default_agents.updated_by_user_id IS '项目默认智能体：最近更新操作的用户标识。';
COMMENT ON COLUMN project_default_agents.created_at IS '项目默认智能体：记录创建时间。';
COMMENT ON COLUMN project_default_agents.updated_at IS '项目默认智能体：记录最近更新时间。';

COMMENT ON TABLE audit_logs IS '保存脱敏且不可变的安全与治理操作审计事件。';
COMMENT ON COLUMN audit_logs.id IS '审计日志：主键标识。';
COMMENT ON COLUMN audit_logs.occurred_at IS '审计日志：发生时间。';
COMMENT ON COLUMN audit_logs.actor_user_id IS '审计日志：操作主体用户标识。';
COMMENT ON COLUMN audit_logs.actor_process IS '审计日志：操作主体进程。';
COMMENT ON COLUMN audit_logs.actor_platform_role IS '审计日志：操作主体平台角色。';
COMMENT ON COLUMN audit_logs.project_id IS '审计日志：所属项目标识。';
COMMENT ON COLUMN audit_logs.action IS '审计日志：审计操作类型。';
COMMENT ON COLUMN audit_logs.target_kind IS '审计日志：目标类型。';
COMMENT ON COLUMN audit_logs.target_ref_key_id IS '审计日志：目标引用键标识。';
COMMENT ON COLUMN audit_logs.target_ref_hmac IS '审计日志：目标引用的域分离 HMAC。';
COMMENT ON COLUMN audit_logs.outcome IS '审计日志：执行结果。';
COMMENT ON COLUMN audit_logs.public_error_code IS '审计日志：可公开的稳定错误代码。';
COMMENT ON COLUMN audit_logs.request_id IS '审计日志：请求标识。';
COMMENT ON COLUMN audit_logs.job_id IS '审计日志：任务标识。';
COMMENT ON COLUMN audit_logs.attempt_id IS '审计日志：尝试标识。';
COMMENT ON COLUMN audit_logs.metadata_json IS '审计日志：非敏感业务元数据。';

COMMENT ON TABLE credentials IS '保存项目受管凭据的逻辑身份和当前版本指针。';
COMMENT ON COLUMN credentials.id IS '受管凭据：主键标识。';
COMMENT ON COLUMN credentials.scope IS '受管凭据：资产或数据归属范围。';
COMMENT ON COLUMN credentials.project_id IS '受管凭据：所属项目标识。';
COMMENT ON COLUMN credentials.name IS '受管凭据：名称。';
COMMENT ON COLUMN credentials.display_name IS '受管凭据：展示名称。';
COMMENT ON COLUMN credentials.credential_type IS '受管凭据：凭据类型。';
COMMENT ON COLUMN credentials.status IS '受管凭据：生命周期状态。';
COMMENT ON COLUMN credentials.is_delete IS '受管凭据：是否标记为删除。';
COMMENT ON COLUMN credentials.current_version_id IS '受管凭据：当前版本标识。';
COMMENT ON COLUMN credentials.version IS '受管凭据：记录版本号。';
COMMENT ON COLUMN credentials.source_key IS '受管凭据：来源键。';
COMMENT ON COLUMN credentials.revoked_at IS '受管凭据：撤销时间。';
COMMENT ON COLUMN credentials.revoked_by_user_id IS '受管凭据：执行撤销的用户标识。';
COMMENT ON COLUMN credentials.created_by_user_id IS '受管凭据：创建操作的用户标识。';
COMMENT ON COLUMN credentials.created_at IS '受管凭据：记录创建时间。';
COMMENT ON COLUMN credentials.updated_at IS '受管凭据：记录最近更新时间。';

COMMENT ON TABLE project_channel_instances IS '保存项目接入渠道实例及其期望运行状态。';
COMMENT ON COLUMN project_channel_instances.id IS '项目渠道实例：主键标识。';
COMMENT ON COLUMN project_channel_instances.project_id IS '项目渠道实例：所属项目标识。';
COMMENT ON COLUMN project_channel_instances.provider IS '项目渠道实例：外部服务提供方。';
COMMENT ON COLUMN project_channel_instances.display_name IS '项目渠道实例：展示名称。';
COMMENT ON COLUMN project_channel_instances.desired_status IS '项目渠道实例：控制面期望状态。';
COMMENT ON COLUMN project_channel_instances.observed_status IS '项目渠道实例：运行面观测状态。';
COMMENT ON COLUMN project_channel_instances.public_config IS '项目渠道实例：不含机密值的公开配置。';
COMMENT ON COLUMN project_channel_instances.provider_identity_digest IS '项目渠道实例：外部提供方身份的脱敏摘要。';
COMMENT ON COLUMN project_channel_instances.revision IS '项目渠道实例：配置修订号。';
COMMENT ON COLUMN project_channel_instances.last_error_code IS '项目渠道实例：最近错误代码。';
COMMENT ON COLUMN project_channel_instances.created_by_user_id IS '项目渠道实例：创建操作的用户标识。';
COMMENT ON COLUMN project_channel_instances.updated_by_user_id IS '项目渠道实例：最近更新操作的用户标识。';
COMMENT ON COLUMN project_channel_instances.created_at IS '项目渠道实例：记录创建时间。';
COMMENT ON COLUMN project_channel_instances.updated_at IS '项目渠道实例：记录最近更新时间。';
COMMENT ON COLUMN project_channel_instances.deleted_at IS '项目渠道实例：记录删除时间。';

COMMENT ON TABLE project_channel_instance_leases IS '保存渠道实例运行者的栅栏代次与租约。';
COMMENT ON COLUMN project_channel_instance_leases.channel_instance_id IS '渠道实例租约：渠道实例标识。';
COMMENT ON COLUMN project_channel_instance_leases.project_id IS '渠道实例租约：所属项目标识。';
COMMENT ON COLUMN project_channel_instance_leases.holder_id IS '渠道实例租约：持有者标识。';
COMMENT ON COLUMN project_channel_instance_leases.lease_token_hash IS '渠道实例租约：执行租约令牌的不可逆哈希。';
COMMENT ON COLUMN project_channel_instance_leases.fencing_generation IS '渠道实例租约：栅栏代次。';
COMMENT ON COLUMN project_channel_instance_leases.lease_expires_at IS '渠道实例租约：租约过期时间。';
COMMENT ON COLUMN project_channel_instance_leases.last_heartbeat_at IS '渠道实例租约：最近心跳时间。';
COMMENT ON COLUMN project_channel_instance_leases.created_at IS '渠道实例租约：记录创建时间。';
COMMENT ON COLUMN project_channel_instance_leases.updated_at IS '渠道实例租约：记录最近更新时间。';

COMMENT ON TABLE mcp_servers IS '保存项目 MCP 服务的逻辑身份和当前发布指针。';
COMMENT ON COLUMN mcp_servers.id IS '项目 MCP 服务：主键标识。';
COMMENT ON COLUMN mcp_servers.scope IS '项目 MCP 服务：资产或数据归属范围。';
COMMENT ON COLUMN mcp_servers.project_id IS '项目 MCP 服务：所属项目标识。';
COMMENT ON COLUMN mcp_servers.slug IS '项目 MCP 服务：稳定可读标识名。';
COMMENT ON COLUMN mcp_servers.display_name IS '项目 MCP 服务：展示名称。';
COMMENT ON COLUMN mcp_servers.status IS '项目 MCP 服务：生命周期状态。';
COMMENT ON COLUMN mcp_servers.current_published_version_id IS '项目 MCP 服务：当前发布版本标识。';
COMMENT ON COLUMN mcp_servers.version IS '项目 MCP 服务：记录版本号。';
COMMENT ON COLUMN mcp_servers.source_key IS '项目 MCP 服务：来源键。';
COMMENT ON COLUMN mcp_servers.created_by_user_id IS '项目 MCP 服务：创建操作的用户标识。';
COMMENT ON COLUMN mcp_servers.created_at IS '项目 MCP 服务：记录创建时间。';
COMMENT ON COLUMN mcp_servers.updated_at IS '项目 MCP 服务：记录最近更新时间。';

COMMENT ON TABLE project_invitations IS '保存项目成员邀请、兑换和撤销生命周期。';
COMMENT ON COLUMN project_invitations.id IS '项目邀请：主键标识。';
COMMENT ON COLUMN project_invitations.project_id IS '项目邀请：所属项目标识。';
COMMENT ON COLUMN project_invitations.invited_email IS '项目邀请：受邀邮箱。';
COMMENT ON COLUMN project_invitations.role IS '项目邀请：授权角色。';
COMMENT ON COLUMN project_invitations.token_hash IS '项目邀请：令牌的不可逆哈希。';
COMMENT ON COLUMN project_invitations.status IS '项目邀请：生命周期状态。';
COMMENT ON COLUMN project_invitations.expires_at IS '项目邀请：过期时间。';
COMMENT ON COLUMN project_invitations.version IS '项目邀请：记录版本号。';
COMMENT ON COLUMN project_invitations.created_by_user_id IS '项目邀请：创建操作的用户标识。';
COMMENT ON COLUMN project_invitations.redeemed_by_user_id IS '项目邀请：兑换邀请的用户标识。';
COMMENT ON COLUMN project_invitations.redeemed_at IS '项目邀请：兑换时间。';
COMMENT ON COLUMN project_invitations.revoked_at IS '项目邀请：撤销时间。';
COMMENT ON COLUMN project_invitations.created_at IS '项目邀请：记录创建时间。';

COMMENT ON TABLE user_notifications IS '保存面向用户的站内通知及已读状态。';
COMMENT ON COLUMN user_notifications.id IS '用户通知：主键标识。';
COMMENT ON COLUMN user_notifications.recipient_user_id IS '用户通知：接收者用户标识。';
COMMENT ON COLUMN user_notifications.kind IS '用户通知：业务类型。';
COMMENT ON COLUMN user_notifications.project_invitation_id IS '用户通知：项目邀请标识。';
COMMENT ON COLUMN user_notifications.read_at IS '用户通知：已读时间。';
COMMENT ON COLUMN user_notifications.acted_at IS '用户通知：操作发生时间。';
COMMENT ON COLUMN user_notifications.version IS '用户通知：记录版本号。';
COMMENT ON COLUMN user_notifications.created_at IS '用户通知：记录创建时间。';

COMMENT ON TABLE project_memberships IS '保存用户在项目中的角色、能力版本与停用状态。';
COMMENT ON COLUMN project_memberships.id IS '项目成员关系：主键标识。';
COMMENT ON COLUMN project_memberships.project_id IS '项目成员关系：所属项目标识。';
COMMENT ON COLUMN project_memberships.user_id IS '项目成员关系：用户标识。';
COMMENT ON COLUMN project_memberships.role IS '项目成员关系：授权角色。';
COMMENT ON COLUMN project_memberships.status IS '项目成员关系：生命周期状态。';
COMMENT ON COLUMN project_memberships.ended_at IS '项目成员关系：结束时间。';
COMMENT ON COLUMN project_memberships.retention_until IS '项目成员关系：成员私有数据的保留截止时间。';
COMMENT ON COLUMN project_memberships.ended_by_user_id IS '项目成员关系：结束流程的用户标识。';
COMMENT ON COLUMN project_memberships.end_reason IS '项目成员关系：结束原因。';
COMMENT ON COLUMN project_memberships.version IS '项目成员关系：记录版本号。';
COMMENT ON COLUMN project_memberships.activation_generation IS '项目成员关系：启用代次。';
COMMENT ON COLUMN project_memberships.is_pinned IS '项目成员关系：是否置顶。';
COMMENT ON COLUMN project_memberships.last_entered_at IS '项目成员关系：最近进入时间。';
COMMENT ON COLUMN project_memberships.created_at IS '项目成员关系：记录创建时间。';
COMMENT ON COLUMN project_memberships.updated_at IS '项目成员关系：记录最近更新时间。';

COMMENT ON TABLE project_channel_group_binding_challenges IS '保存外部渠道群组绑定前的短期验证挑战。';
COMMENT ON COLUMN project_channel_group_binding_challenges.id IS '渠道群组绑定验证：主键标识。';
COMMENT ON COLUMN project_channel_group_binding_challenges.project_id IS '渠道群组绑定验证：所属项目标识。';
COMMENT ON COLUMN project_channel_group_binding_challenges.channel_instance_id IS '渠道群组绑定验证：渠道实例标识。';
COMMENT ON COLUMN project_channel_group_binding_challenges.provider IS '渠道群组绑定验证：外部服务提供方。';
COMMENT ON COLUMN project_channel_group_binding_challenges.code_digest IS '渠道群组绑定验证：代码摘要。';
COMMENT ON COLUMN project_channel_group_binding_challenges.agent_asset_id IS '渠道群组绑定验证：智能体资产标识。';
COMMENT ON COLUMN project_channel_group_binding_challenges.agent_scope IS '渠道群组绑定验证：智能体范围。';
COMMENT ON COLUMN project_channel_group_binding_challenges.membership_id IS '渠道群组绑定验证：成员关系标识。';
COMMENT ON COLUMN project_channel_group_binding_challenges.membership_version IS '渠道群组绑定验证：成员关系版本号。';
COMMENT ON COLUMN project_channel_group_binding_challenges.created_by_user_id IS '渠道群组绑定验证：创建操作的用户标识。';
COMMENT ON COLUMN project_channel_group_binding_challenges.expires_at IS '渠道群组绑定验证：过期时间。';
COMMENT ON COLUMN project_channel_group_binding_challenges.consumed_at IS '渠道群组绑定验证：消费时间。';
COMMENT ON COLUMN project_channel_group_binding_challenges.created_at IS '渠道群组绑定验证：记录创建时间。';

COMMENT ON TABLE project_channel_group_bindings IS '保存外部渠道群组与项目之间的受管绑定及可复用身份锚点。';
COMMENT ON COLUMN project_channel_group_bindings.id IS '渠道群组绑定：主键标识。';
COMMENT ON COLUMN project_channel_group_bindings.project_id IS '渠道群组绑定：所属项目标识。';
COMMENT ON COLUMN project_channel_group_bindings.channel_instance_id IS '渠道群组绑定：渠道实例标识。';
COMMENT ON COLUMN project_channel_group_bindings.provider IS '渠道群组绑定：外部服务提供方。';
COMMENT ON COLUMN project_channel_group_bindings.external_group_ref IS '渠道群组绑定：外部群组引用。';
COMMENT ON COLUMN project_channel_group_bindings.external_group_name IS '渠道群组绑定：外部群组名称。';
COMMENT ON COLUMN project_channel_group_bindings.agent_scope IS '渠道群组绑定：活动绑定的智能体范围；软删除后为空。';
COMMENT ON COLUMN project_channel_group_bindings.agent_asset_id IS '渠道群组绑定：活动绑定的智能体资产标识；软删除后为空。';
COMMENT ON COLUMN project_channel_group_bindings.status IS '渠道群组绑定：生命周期状态。';
COMMENT ON COLUMN project_channel_group_bindings.revision IS '渠道群组绑定：配置修订号。';
COMMENT ON COLUMN project_channel_group_bindings.created_by_user_id IS '渠道群组绑定：创建操作的用户标识。';
COMMENT ON COLUMN project_channel_group_bindings.updated_by_user_id IS '渠道群组绑定：最近更新操作的用户标识。';
COMMENT ON COLUMN project_channel_group_bindings.first_activity_at IS '渠道群组绑定：首次活动时间。';
COMMENT ON COLUMN project_channel_group_bindings.last_activity_at IS '渠道群组绑定：最近活动时间。';
COMMENT ON COLUMN project_channel_group_bindings.created_at IS '渠道群组绑定：记录创建时间。';
COMMENT ON COLUMN project_channel_group_bindings.updated_at IS '渠道群组绑定：记录最近更新时间。';
COMMENT ON COLUMN project_channel_group_bindings.deleted_at IS '渠道群组绑定：软删除时间；置值后保留身份锚点并释放智能体引用。';

COMMENT ON TABLE channel_external_principals IS '保存渠道外部身份到平台主体的映射。';
COMMENT ON COLUMN channel_external_principals.id IS '渠道外部主体：主键标识。';
COMMENT ON COLUMN channel_external_principals.project_id IS '渠道外部主体：所属项目标识。';
COMMENT ON COLUMN channel_external_principals.group_binding_id IS '渠道外部主体：群组绑定标识。';
COMMENT ON COLUMN channel_external_principals.external_account_ref IS '渠道外部主体：外部账户引用。';
COMMENT ON COLUMN channel_external_principals.principal_user_id IS '渠道外部主体：主体用户标识。';
COMMENT ON COLUMN channel_external_principals.principal_type IS '渠道外部主体：主体类型。';
COMMENT ON COLUMN channel_external_principals.membership_id IS '渠道外部主体：成员关系标识。';
COMMENT ON COLUMN channel_external_principals.membership_role IS '渠道外部主体：成员关系角色。';
COMMENT ON COLUMN channel_external_principals.status IS '渠道外部主体：生命周期状态。';
COMMENT ON COLUMN channel_external_principals.first_seen_at IS '渠道外部主体：首次发现时间。';
COMMENT ON COLUMN channel_external_principals.last_seen_at IS '渠道外部主体：最近发现时间。';
COMMENT ON COLUMN channel_external_principals.created_at IS '渠道外部主体：记录创建时间。';
COMMENT ON COLUMN channel_external_principals.updated_at IS '渠道外部主体：记录最近更新时间。';

COMMENT ON TABLE project_quotas IS '保存项目可收紧的平台资源限额。';
COMMENT ON COLUMN project_quotas.project_id IS '项目配额：所属项目标识。';
COMMENT ON COLUMN project_quotas.member_limit IS '项目配额：成员限额。';
COMMENT ON COLUMN project_quotas.storage_bytes_limit IS '项目配额：存储字节数限额。';
COMMENT ON COLUMN project_quotas.concurrent_run_limit IS '项目配额：并发运行限额。';
COMMENT ON COLUMN project_quotas.mcp_calls_daily_limit IS '项目配额：MCP调用每日限额。';
COMMENT ON COLUMN project_quotas.version IS '项目配额：记录版本号。';
COMMENT ON COLUMN project_quotas.updated_by_user_id IS '项目配额：最近更新操作的用户标识。';
COMMENT ON COLUMN project_quotas.updated_at IS '项目配额：记录最近更新时间。';

COMMENT ON TABLE project_usage_counters IS '保存项目当前计量桶中的事务性用量。';
COMMENT ON COLUMN project_usage_counters.project_id IS '项目用量计数：所属项目标识。';
COMMENT ON COLUMN project_usage_counters.dimension IS '项目用量计数：维度。';
COMMENT ON COLUMN project_usage_counters.bucket IS '项目用量计数：计量桶。';
COMMENT ON COLUMN project_usage_counters.used IS '项目用量计数：已使用计量数量。';
COMMENT ON COLUMN project_usage_counters.reserved IS '项目用量计数：预留计量数量。';
COMMENT ON COLUMN project_usage_counters.version IS '项目用量计数：记录版本号。';
COMMENT ON COLUMN project_usage_counters.updated_at IS '项目用量计数：记录最近更新时间。';

COMMENT ON TABLE project_usage_ledger IS '保存项目已提交用量变化的追加式台账。';
COMMENT ON COLUMN project_usage_ledger.id IS '项目用量台账：主键标识。';
COMMENT ON COLUMN project_usage_ledger.project_id IS '项目用量台账：所属项目标识。';
COMMENT ON COLUMN project_usage_ledger.dimension IS '项目用量台账：维度。';
COMMENT ON COLUMN project_usage_ledger.delta IS '项目用量台账：增量。';
COMMENT ON COLUMN project_usage_ledger.bucket IS '项目用量台账：计量桶。';
COMMENT ON COLUMN project_usage_ledger.source_kind IS '项目用量台账：来源类型。';
COMMENT ON COLUMN project_usage_ledger.source_ref_key_id IS '项目用量台账：来源引用键标识。';
COMMENT ON COLUMN project_usage_ledger.source_ref_hmac IS '项目用量台账：来源引用的域分离 HMAC。';
COMMENT ON COLUMN project_usage_ledger.idempotency_key IS '项目用量台账：幂等操作键。';
COMMENT ON COLUMN project_usage_ledger.request_id IS '项目用量台账：请求标识。';
COMMENT ON COLUMN project_usage_ledger.occurred_at IS '项目用量台账：发生时间。';

COMMENT ON TABLE skills IS '保存技能的逻辑身份和 Current Version 指针。';
COMMENT ON COLUMN skills.id IS '项目技能：主键标识。';
COMMENT ON COLUMN skills.scope IS '项目技能：资产或数据归属范围。';
COMMENT ON COLUMN skills.project_id IS '项目技能：所属项目标识。';
COMMENT ON COLUMN skills.slug IS '项目技能：稳定可读标识名。';
COMMENT ON COLUMN skills.display_name IS '项目技能：展示名称。';
COMMENT ON COLUMN skills.status IS '项目技能：生命周期状态。';
COMMENT ON COLUMN skills.current_version_id IS '项目技能：当前版本标识。';
COMMENT ON COLUMN skills.revision IS '项目技能：配置修订号。';
COMMENT ON COLUMN skills.source_key IS '项目技能：来源键。';
COMMENT ON COLUMN skills.created_by_user_id IS '项目技能：创建操作的用户标识。';
COMMENT ON COLUMN skills.created_at IS '项目技能：记录创建时间。';
COMMENT ON COLUMN skills.updated_at IS '项目技能：记录最近更新时间。';

COMMENT ON TABLE agent_versions IS '保存不可变的项目智能体版本内容与运行配置。';
COMMENT ON COLUMN agent_versions.id IS '智能体版本：主键标识。';
COMMENT ON COLUMN agent_versions.agent_id IS '智能体版本：智能体标识。';
COMMENT ON COLUMN agent_versions.version_number IS '智能体版本：版本编号。';
COMMENT ON COLUMN agent_versions.description IS '智能体版本：用途描述。';
COMMENT ON COLUMN agent_versions.soul IS '智能体版本：项目智能体人格设定。';
COMMENT ON COLUMN agent_versions.model_ref IS '智能体版本：模型引用。';
COMMENT ON COLUMN agent_versions.model_settings IS '智能体版本：模型调用设置。';
COMMENT ON COLUMN agent_versions.tool_groups IS '智能体版本：允许使用的工具分组。';
COMMENT ON COLUMN agent_versions.supersedes_version_id IS '智能体版本：替代目标版本标识。';
COMMENT ON COLUMN agent_versions.payload_checksum IS '智能体版本：载荷内容校验和。';
COMMENT ON COLUMN agent_versions.created_by_user_id IS '智能体版本：创建操作的用户标识。';
COMMENT ON COLUMN agent_versions.created_at IS '智能体版本：记录创建时间。';
COMMENT ON COLUMN agent_versions.agents_instructions IS '智能体版本：项目智能体行为指令。';
COMMENT ON COLUMN agent_versions.identity IS '智能体版本：身份。';
COMMENT ON COLUMN agent_versions.user_context IS '智能体版本：智能体使用的用户上下文。';
COMMENT ON COLUMN agent_versions.payload_schema_version IS '智能体版本：载荷架构版本号。';

COMMENT ON TABLE agent_design_sessions IS '保存智能体设计向导的私有会话状态与产物引用。';
COMMENT ON COLUMN agent_design_sessions.id IS '智能体设计会话：主键标识。';
COMMENT ON COLUMN agent_design_sessions.project_id IS '智能体设计会话：所属项目标识。';
COMMENT ON COLUMN agent_design_sessions.owner_user_id IS '智能体设计会话：私有数据所有者的用户标识。';
COMMENT ON COLUMN agent_design_sessions.thread_id IS '智能体设计会话：线程标识。';
COMMENT ON COLUMN agent_design_sessions.slug IS '智能体设计会话：稳定可读标识名。';
COMMENT ON COLUMN agent_design_sessions.display_name IS '智能体设计会话：展示名称。';
COMMENT ON COLUMN agent_design_sessions.status IS '智能体设计会话：生命周期状态。';
COMMENT ON COLUMN agent_design_sessions.revision IS '智能体设计会话：配置修订号。';
COMMENT ON COLUMN agent_design_sessions.messages_json IS '智能体设计会话：设计会话消息列表。';
COMMENT ON COLUMN agent_design_sessions.progress_json IS '智能体设计会话：设计会话进度列表。';
COMMENT ON COLUMN agent_design_sessions.active_clarification_json IS '智能体设计会话：活跃澄清JSON 数据。';
COMMENT ON COLUMN agent_design_sessions.blueprint_json IS '智能体设计会话：蓝图JSON 数据。';
COMMENT ON COLUMN agent_design_sessions.blueprint_checksum IS '智能体设计会话：蓝图校验和。';
COMMENT ON COLUMN agent_design_sessions.error_code IS '智能体设计会话：稳定错误代码。';
COMMENT ON COLUMN agent_design_sessions.error_message IS '智能体设计会话：受限的错误说明。';
COMMENT ON COLUMN agent_design_sessions.created_agent_id IS '智能体设计会话：创建智能体标识。';
COMMENT ON COLUMN agent_design_sessions.created_agent_version_id IS '智能体设计会话：创建智能体版本标识。';
COMMENT ON COLUMN agent_design_sessions.create_idempotency_key_hash IS '智能体设计会话：创建操作幂等键的不可逆哈希。';
COMMENT ON COLUMN agent_design_sessions.create_request_checksum IS '智能体设计会话：创建请求校验和。';
COMMENT ON COLUMN agent_design_sessions.created_at IS '智能体设计会话：记录创建时间。';
COMMENT ON COLUMN agent_design_sessions.updated_at IS '智能体设计会话：记录最近更新时间。';
COMMENT ON COLUMN agent_design_sessions.created_agent_deleted IS '智能体设计会话：已创建的智能体是否随后删除。';
COMMENT ON COLUMN agent_design_sessions.generation_model_ref IS '智能体设计会话：生成模型引用。';
COMMENT ON COLUMN agent_design_sessions.generation_mode IS '智能体设计会话：生成模式。';

COMMENT ON TABLE agent_design_operations IS '保存智能体设计会话中的幂等操作及其结果。';
COMMENT ON COLUMN agent_design_operations.id IS '智能体设计操作：主键标识。';
COMMENT ON COLUMN agent_design_operations.project_id IS '智能体设计操作：所属项目标识。';
COMMENT ON COLUMN agent_design_operations.owner_user_id IS '智能体设计操作：私有数据所有者的用户标识。';
COMMENT ON COLUMN agent_design_operations.session_id IS '智能体设计操作：会话标识。';
COMMENT ON COLUMN agent_design_operations.operation_kind IS '智能体设计操作：操作类型。';
COMMENT ON COLUMN agent_design_operations.idempotency_key_hash IS '智能体设计操作：幂等操作键的不可逆哈希。';
COMMENT ON COLUMN agent_design_operations.request_checksum IS '智能体设计操作：请求校验和。';
COMMENT ON COLUMN agent_design_operations.status IS '智能体设计操作：生命周期状态。';
COMMENT ON COLUMN agent_design_operations.result_revision IS '智能体设计操作：结果修订号。';
COMMENT ON COLUMN agent_design_operations.public_error_code IS '智能体设计操作：可公开的稳定错误代码。';
COMMENT ON COLUMN agent_design_operations.created_at IS '智能体设计操作：记录创建时间。';
COMMENT ON COLUMN agent_design_operations.updated_at IS '智能体设计操作：记录最近更新时间。';
COMMENT ON COLUMN agent_design_operations.stop_requested_at IS '智能体设计操作：停止请求时间。';
COMMENT ON COLUMN agent_design_operations.requested_generation_profile_json IS '智能体设计操作：请求生成配置 JSON 数据。';
COMMENT ON COLUMN agent_design_operations.effective_generation_profile_json IS '智能体设计操作：生效生成配置 JSON 数据。';

COMMENT ON TABLE agent_design_activities IS '保存智能体设计会话中可回放的公开过程事件。';
COMMENT ON COLUMN agent_design_activities.seq IS '智能体设计活动：单调序号。';
COMMENT ON COLUMN agent_design_activities.project_id IS '智能体设计活动：所属项目标识。';
COMMENT ON COLUMN agent_design_activities.owner_user_id IS '智能体设计活动：私有数据所有者的用户标识。';
COMMENT ON COLUMN agent_design_activities.session_id IS '智能体设计活动：会话标识。';
COMMENT ON COLUMN agent_design_activities.operation_id IS '智能体设计活动：操作标识。';
COMMENT ON COLUMN agent_design_activities.attempt IS '智能体设计活动：尝试。';
COMMENT ON COLUMN agent_design_activities.kind IS '智能体设计活动：业务类型。';
COMMENT ON COLUMN agent_design_activities.payload_json IS '智能体设计活动：公开载荷 JSON 数据。';
COMMENT ON COLUMN agent_design_activities.created_at IS '智能体设计活动：记录创建时间。';

COMMENT ON TABLE channel_connections IS '保存用户授权的外部渠道账户连接。';
COMMENT ON COLUMN channel_connections.id IS '渠道连接：主键标识。';
COMMENT ON COLUMN channel_connections.owner_user_id IS '渠道连接：私有数据所有者的用户标识。';
COMMENT ON COLUMN channel_connections.provider IS '渠道连接：外部服务提供方。';
COMMENT ON COLUMN channel_connections.status IS '渠道连接：生命周期状态。';
COMMENT ON COLUMN channel_connections.external_account_id IS '渠道连接：外部账户标识。';
COMMENT ON COLUMN channel_connections.external_account_name IS '渠道连接：外部账户名称。';
COMMENT ON COLUMN channel_connections.workspace_id IS '渠道连接：工作区标识。';
COMMENT ON COLUMN channel_connections.workspace_name IS '渠道连接：工作区名称。';
COMMENT ON COLUMN channel_connections.bot_user_id IS '渠道连接：机器人用户标识。';
COMMENT ON COLUMN channel_connections.scopes_json IS '渠道连接：已授予的 OAuth 权限范围。';
COMMENT ON COLUMN channel_connections.capabilities_json IS '渠道连接：工作节点能力列表。';
COMMENT ON COLUMN channel_connections.metadata_json IS '渠道连接：非敏感业务元数据。';
COMMENT ON COLUMN channel_connections.created_at IS '渠道连接：记录创建时间。';
COMMENT ON COLUMN channel_connections.updated_at IS '渠道连接：记录最近更新时间。';
COMMENT ON COLUMN channel_connections.last_seen_at IS '渠道连接：最近发现时间。';
COMMENT ON COLUMN channel_connections.last_error_at IS '渠道连接：最近错误时间。';
COMMENT ON COLUMN channel_connections.project_id IS '渠道连接：所属项目标识。';
COMMENT ON COLUMN channel_connections.channel_instance_id IS '渠道连接：渠道实例标识。';
COMMENT ON COLUMN channel_connections.frozen_at IS '渠道连接：冻结时间。';

COMMENT ON TABLE channel_oauth_states IS '保存渠道 OAuth 流程的一次性校验状态。';
COMMENT ON COLUMN channel_oauth_states.state_hash IS '渠道 OAuth 状态：状态内容哈希。';
COMMENT ON COLUMN channel_oauth_states.owner_user_id IS '渠道 OAuth 状态：私有数据所有者的用户标识。';
COMMENT ON COLUMN channel_oauth_states.provider IS '渠道 OAuth 状态：外部服务提供方。';
COMMENT ON COLUMN channel_oauth_states.code_verifier_encrypted IS '渠道 OAuth 状态：加密保存的 OAuth 校验器。';
COMMENT ON COLUMN channel_oauth_states.nonce_hash IS '渠道 OAuth 状态：一次性随机数的不可逆哈希。';
COMMENT ON COLUMN channel_oauth_states.redirect_after IS '渠道 OAuth 状态：OAuth 完成后的站内跳转路径。';
COMMENT ON COLUMN channel_oauth_states.requested_scopes_json IS '渠道 OAuth 状态：请求的 OAuth 权限范围。';
COMMENT ON COLUMN channel_oauth_states.metadata_json IS '渠道 OAuth 状态：非敏感业务元数据。';
COMMENT ON COLUMN channel_oauth_states.expires_at IS '渠道 OAuth 状态：过期时间。';
COMMENT ON COLUMN channel_oauth_states.consumed_at IS '渠道 OAuth 状态：消费时间。';
COMMENT ON COLUMN channel_oauth_states.created_at IS '渠道 OAuth 状态：记录创建时间。';
COMMENT ON COLUMN channel_oauth_states.project_id IS '渠道 OAuth 状态：所属项目标识。';
COMMENT ON COLUMN channel_oauth_states.channel_instance_id IS '渠道 OAuth 状态：渠道实例标识。';

COMMENT ON TABLE credential_versions IS '保存受管凭据的不可变版本元数据。';
COMMENT ON COLUMN credential_versions.id IS '凭据版本：主键标识。';
COMMENT ON COLUMN credential_versions.credential_id IS '凭据版本：凭据标识。';
COMMENT ON COLUMN credential_versions.version_number IS '凭据版本：版本编号。';
COMMENT ON COLUMN credential_versions.status IS '凭据版本：生命周期状态。';
COMMENT ON COLUMN credential_versions.payload_schema_version IS '凭据版本：载荷架构版本号。';
COMMENT ON COLUMN credential_versions.payload_schema IS '凭据版本：载荷结构定义。';
COMMENT ON COLUMN credential_versions.supersedes_version_id IS '凭据版本：替代目标版本标识。';
COMMENT ON COLUMN credential_versions.retired_at IS '凭据版本：停用时间。';
COMMENT ON COLUMN credential_versions.revoked_at IS '凭据版本：撤销时间。';
COMMENT ON COLUMN credential_versions.revoked_by_user_id IS '凭据版本：执行撤销的用户标识。';
COMMENT ON COLUMN credential_versions.created_by_user_id IS '凭据版本：创建操作的用户标识。';
COMMENT ON COLUMN credential_versions.created_at IS '凭据版本：记录创建时间。';

COMMENT ON TABLE feedback IS '保存用户针对智能体运行提交的评分与意见。';
COMMENT ON COLUMN feedback.feedback_id IS '运行反馈：反馈标识。';
COMMENT ON COLUMN feedback.run_id IS '运行反馈：运行标识。';
COMMENT ON COLUMN feedback.thread_id IS '运行反馈：线程标识。';
COMMENT ON COLUMN feedback.owner_user_id IS '运行反馈：私有数据所有者的用户标识。';
COMMENT ON COLUMN feedback.message_id IS '运行反馈：消息标识。';
COMMENT ON COLUMN feedback.rating IS '运行反馈：用户评分。';
COMMENT ON COLUMN feedback.comment IS '运行反馈：用户反馈说明。';
COMMENT ON COLUMN feedback.created_at IS '运行反馈：记录创建时间。';
COMMENT ON COLUMN feedback.project_id IS '运行反馈：所属项目标识。';

COMMENT ON TABLE mcp_server_versions IS '保存不可变的 MCP 服务连接与公开配置。';
COMMENT ON COLUMN mcp_server_versions.id IS 'MCP 服务版本：主键标识。';
COMMENT ON COLUMN mcp_server_versions.mcp_server_id IS 'MCP 服务版本：MCP服务标识。';
COMMENT ON COLUMN mcp_server_versions.version_number IS 'MCP 服务版本：版本编号。';
COMMENT ON COLUMN mcp_server_versions.workflow_status IS 'MCP 服务版本：工作流状态。';
COMMENT ON COLUMN mcp_server_versions.description IS 'MCP 服务版本：用途描述。';
COMMENT ON COLUMN mcp_server_versions.transport IS 'MCP 服务版本：传输。';
COMMENT ON COLUMN mcp_server_versions.command IS 'MCP 服务版本：命令。';
COMMENT ON COLUMN mcp_server_versions.args IS 'MCP 服务版本：参数。';
COMMENT ON COLUMN mcp_server_versions.url IS 'MCP 服务版本：不含凭据的服务访问地址。';
COMMENT ON COLUMN mcp_server_versions.non_secret_env IS 'MCP 服务版本：不含机密值的环境变量配置。';
COMMENT ON COLUMN mcp_server_versions.non_secret_headers IS 'MCP 服务版本：不含机密值的请求头配置。';
COMMENT ON COLUMN mcp_server_versions.oauth_metadata IS 'MCP 服务版本：OAuth 能力的非敏感元数据。';
COMMENT ON COLUMN mcp_server_versions.routing IS 'MCP 服务版本：路由。';
COMMENT ON COLUMN mcp_server_versions.tool_overrides IS 'MCP 服务版本：工具级策略覆盖配置。';
COMMENT ON COLUMN mcp_server_versions.timeout_seconds IS 'MCP 服务版本：超时秒数。';
COMMENT ON COLUMN mcp_server_versions.supersedes_version_id IS 'MCP 服务版本：替代目标版本标识。';
COMMENT ON COLUMN mcp_server_versions.payload_checksum IS 'MCP 服务版本：载荷内容校验和。';
COMMENT ON COLUMN mcp_server_versions.submitted_at IS 'MCP 服务版本：提交时间。';
COMMENT ON COLUMN mcp_server_versions.reviewed_at IS 'MCP 服务版本：审核时间。';
COMMENT ON COLUMN mcp_server_versions.reviewed_by_user_id IS 'MCP 服务版本：执行审核的用户标识。';
COMMENT ON COLUMN mcp_server_versions.review_note IS 'MCP 服务版本：静态审核说明。';
COMMENT ON COLUMN mcp_server_versions.created_by_user_id IS 'MCP 服务版本：创建操作的用户标识。';
COMMENT ON COLUMN mcp_server_versions.created_at IS 'MCP 服务版本：记录创建时间。';

COMMENT ON TABLE run_asset_versions IS '冻结一次运行准入时解析出的智能体、技能或 MCP 完整版本内容。';
COMMENT ON COLUMN run_asset_versions.project_id IS '运行资产快照：所属项目标识。';
COMMENT ON COLUMN run_asset_versions.owner_user_id IS '运行资产快照：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_asset_versions.thread_id IS '运行资产快照：线程标识。';
COMMENT ON COLUMN run_asset_versions.run_id IS '运行资产快照：运行标识。';
COMMENT ON COLUMN run_asset_versions.asset_kind IS '运行资产快照：资产类型。';
COMMENT ON COLUMN run_asset_versions.dependency_order IS '运行资产快照：依赖顺序。';
COMMENT ON COLUMN run_asset_versions.asset_scope IS '运行资产快照：资产范围。';
COMMENT ON COLUMN run_asset_versions.asset_id IS '运行资产快照：资产标识。';
COMMENT ON COLUMN run_asset_versions.version_id IS '运行资产快照：版本标识。';
COMMENT ON COLUMN run_asset_versions.payload_checksum IS '运行资产快照：载荷内容校验和。';
COMMENT ON COLUMN run_asset_versions.catalog_generation IS '运行资产快照：目录代次。';
COMMENT ON COLUMN run_asset_versions.created_at IS '运行资产快照：记录创建时间。';
COMMENT ON COLUMN run_asset_versions.snapshot_json IS '运行资产快照：准入时冻结的完整且不含明文凭据的资产内容。';

COMMENT ON TABLE run_event_partition_state IS '记录运行事件分区维护的高水位。';
COMMENT ON COLUMN run_event_partition_state.singleton IS '运行事件分区状态：单例约束标识。';
COMMENT ON COLUMN run_event_partition_state.retained_from IS '运行事件分区状态：运行事件当前保留范围的起始日期。';
COMMENT ON COLUMN run_event_partition_state.updated_at IS '运行事件分区状态：记录最近更新时间。';

COMMENT ON TABLE run_event_invariants IS '保存运行事件全局单调序列的单例状态。';
COMMENT ON COLUMN run_event_invariants.id IS '运行事件不变量：主键标识。';
COMMENT ON COLUMN run_event_invariants.created_at IS '运行事件不变量：记录创建时间。';
COMMENT ON COLUMN run_event_invariants.project_id IS '运行事件不变量：所属项目标识。';
COMMENT ON COLUMN run_event_invariants.owner_user_id IS '运行事件不变量：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_event_invariants.thread_id IS '运行事件不变量：线程标识。';
COMMENT ON COLUMN run_event_invariants.run_id IS '运行事件不变量：运行标识。';
COMMENT ON COLUMN run_event_invariants.seq IS '运行事件不变量：单调序号。';
COMMENT ON COLUMN run_event_invariants.is_stream_terminal IS '运行事件不变量：是否为事件流终态。';

COMMENT ON TABLE run_events IS '保存按日期分区的持久化运行事件流。';
COMMENT ON COLUMN run_events.id IS '运行事件：主键标识。';
COMMENT ON COLUMN run_events.thread_id IS '运行事件：线程标识。';
COMMENT ON COLUMN run_events.run_id IS '运行事件：运行标识。';
COMMENT ON COLUMN run_events.owner_user_id IS '运行事件：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_events.event_type IS '运行事件：事件类型。';
COMMENT ON COLUMN run_events.category IS '运行事件：类别。';
COMMENT ON COLUMN run_events.content IS '运行事件：事件正文文本（可能包含私有消息、轨迹或生命周期内容）。';
COMMENT ON COLUMN run_events.event_metadata IS '运行事件：事件的结构化非敏感元数据。';
COMMENT ON COLUMN run_events.seq IS '运行事件：单调序号。';
COMMENT ON COLUMN run_events.created_at IS '运行事件：记录创建时间。';
COMMENT ON COLUMN run_events.project_id IS '运行事件：所属项目标识。';

COMMENT ON TABLE skill_versions IS '保存不可变的项目技能版本及扫描结论。';
COMMENT ON COLUMN skill_versions.id IS '技能版本：主键标识。';
COMMENT ON COLUMN skill_versions.skill_id IS '技能版本：技能标识。';
COMMENT ON COLUMN skill_versions.version_number IS '技能版本：版本编号。';
COMMENT ON COLUMN skill_versions.description IS '技能版本：用途描述。';
COMMENT ON COLUMN skill_versions.frontmatter IS '技能版本：技能入口文件的元数据头。';
COMMENT ON COLUMN skill_versions.compatibility IS '技能版本：兼容性。';
COMMENT ON COLUMN skill_versions.secret_requirements IS '技能版本：所需机密项的名称与用途声明。';
COMMENT ON COLUMN skill_versions.scan_decision IS '技能版本：安全扫描结论。';
COMMENT ON COLUMN skill_versions.scan_summary IS '技能版本：安全扫描摘要。';
COMMENT ON COLUMN skill_versions.supersedes_version_id IS '技能版本：替代目标版本标识。';
COMMENT ON COLUMN skill_versions.payload_checksum IS '技能版本：载荷内容校验和。';
COMMENT ON COLUMN skill_versions.created_by_user_id IS '技能版本：创建操作的用户标识。';
COMMENT ON COLUMN skill_versions.created_at IS '技能版本：记录创建时间。';
COMMENT ON COLUMN skill_versions.revoked_at IS '技能版本：不可逆治理撤销时间。';
COMMENT ON COLUMN skill_versions.revoked_by_user_id IS '技能版本：执行撤销的用户标识。';
COMMENT ON COLUMN skill_versions.revocation_reason_code IS '技能版本：撤销原因代码。';

COMMENT ON TABLE threads_meta IS '保存项目私有线程的标题、状态与活动时间。';
COMMENT ON COLUMN threads_meta.thread_id IS '线程元数据：线程标识。';
COMMENT ON COLUMN threads_meta.assistant_id IS '线程元数据：助手标识。';
COMMENT ON COLUMN threads_meta.owner_user_id IS '线程元数据：私有数据所有者的用户标识。';
COMMENT ON COLUMN threads_meta.display_name IS '线程元数据：展示名称。';
COMMENT ON COLUMN threads_meta.status IS '线程元数据：生命周期状态。';
COMMENT ON COLUMN threads_meta.metadata_json IS '线程元数据：非敏感业务元数据。';
COMMENT ON COLUMN threads_meta.created_at IS '线程元数据：记录创建时间。';
COMMENT ON COLUMN threads_meta.updated_at IS '线程元数据：记录最近更新时间。';
COMMENT ON COLUMN threads_meta.project_id IS '线程元数据：所属项目标识。';
COMMENT ON COLUMN threads_meta.agent_asset_id IS '线程元数据：智能体资产标识。';
COMMENT ON COLUMN threads_meta.agent_scope IS '线程元数据：智能体范围。';
COMMENT ON COLUMN threads_meta.frozen_at IS '线程元数据：冻结时间。';
COMMENT ON COLUMN threads_meta.deleted_at IS '线程元数据：记录删除时间。';
COMMENT ON COLUMN threads_meta.memory_sealed_at IS '线程元数据：记忆封存时间。';
COMMENT ON COLUMN threads_meta.checkpoint_delete_status IS '线程元数据：检查点删除状态。';
COMMENT ON COLUMN threads_meta.version IS '线程元数据：记录版本号。';
COMMENT ON COLUMN threads_meta.thread_kind IS '线程元数据：线程类型。';

COMMENT ON TABLE execution_approval_requests IS '保存本机命令的一次性审批、领取与终态生命周期。';
COMMENT ON COLUMN execution_approval_requests.id IS '执行审批请求：主键标识。';
COMMENT ON COLUMN execution_approval_requests.project_id IS '执行审批请求：所属项目标识。';
COMMENT ON COLUMN execution_approval_requests.owner_user_id IS '执行审批请求：私有数据所有者的用户标识。';
COMMENT ON COLUMN execution_approval_requests.thread_id IS '执行审批请求：线程标识。';
COMMENT ON COLUMN execution_approval_requests.source_run_id IS '执行审批请求：产生审批请求的运行标识。';
COMMENT ON COLUMN execution_approval_requests.source_job_id IS '执行审批请求：产生审批请求的任务标识。';
COMMENT ON COLUMN execution_approval_requests.source_job_attempt_id IS '执行审批请求：产生审批请求的任务尝试标识。';
COMMENT ON COLUMN execution_approval_requests.source_agent_path IS '执行审批请求：产生命令的智能体调用路径。';
COMMENT ON COLUMN execution_approval_requests.tool_call_id IS '执行审批请求：产生命令的工具调用标识。';
COMMENT ON COLUMN execution_approval_requests.kind IS '执行审批请求：业务类型。';
COMMENT ON COLUMN execution_approval_requests.command_digest IS '执行审批请求：规范化私有命令的内容摘要。';
COMMENT ON COLUMN execution_approval_requests.execution_domain_affinity IS '执行审批请求：执行域私有快照的不可逆亲和摘要。';
COMMENT ON COLUMN execution_approval_requests.command_private_json IS '执行审批请求：规范化且仅限授权边界读取的私有命令计划 JSON（最多 1 MiB）。';
COMMENT ON COLUMN execution_approval_requests.status IS '执行审批请求：生命周期状态。';
COMMENT ON COLUMN execution_approval_requests.version IS '执行审批请求：记录版本号。';
COMMENT ON COLUMN execution_approval_requests.decision IS '执行审批请求：一次性审批决定。';
COMMENT ON COLUMN execution_approval_requests.decision_idempotency_key IS '执行审批请求：审批决定的幂等键摘要。';
COMMENT ON COLUMN execution_approval_requests.decision_request_digest IS '执行审批请求：审批决定请求的内容摘要。';
COMMENT ON COLUMN execution_approval_requests.decided_by_user_id IS '执行审批请求：作出审批决定的用户标识。';
COMMENT ON COLUMN execution_approval_requests.decided_at IS '执行审批请求：审批决定时间。';
COMMENT ON COLUMN execution_approval_requests.continuation_run_id IS '执行审批请求：审批通过后续接运行的标识。';
COMMENT ON COLUMN execution_approval_requests.continuation_job_id IS '执行审批请求：审批通过后续接任务的标识。';
COMMENT ON COLUMN execution_approval_requests.execution_job_attempt_id IS '执行审批请求：执行已审批命令的任务尝试标识。';
COMMENT ON COLUMN execution_approval_requests.claimed_at IS '执行审批请求：已审批命令的领取时间。';
COMMENT ON COLUMN execution_approval_requests.expires_at IS '执行审批请求：审批请求过期时间。';
COMMENT ON COLUMN execution_approval_requests.terminal_at IS '执行审批请求：审批请求进入终态的时间。';
COMMENT ON COLUMN execution_approval_requests.created_at IS '执行审批请求：记录创建时间。';
COMMENT ON COLUMN execution_approval_requests.updated_at IS '执行审批请求：记录最近更新时间。';
COMMENT ON COLUMN execution_approval_requests.spawn_authorized_at IS '执行审批请求：一次性进程创建授权提交时间。';

COMMENT ON TABLE execution_approval_result_receipts IS '保存一次已审批本机命令的有界私有执行结果。';
COMMENT ON COLUMN execution_approval_result_receipts.id IS '执行审批结果回执：主键标识。';
COMMENT ON COLUMN execution_approval_result_receipts.approval_id IS '执行审批结果回执：执行审批请求标识。';
COMMENT ON COLUMN execution_approval_result_receipts.project_id IS '执行审批结果回执：所属项目标识。';
COMMENT ON COLUMN execution_approval_result_receipts.owner_user_id IS '执行审批结果回执：私有数据所有者的用户标识。';
COMMENT ON COLUMN execution_approval_result_receipts.thread_id IS '执行审批结果回执：线程标识。';
COMMENT ON COLUMN execution_approval_result_receipts.execution_job_id IS '执行审批结果回执：执行已审批命令的任务标识。';
COMMENT ON COLUMN execution_approval_result_receipts.execution_job_attempt_id IS '执行审批结果回执：执行已审批命令的任务尝试标识。';
COMMENT ON COLUMN execution_approval_result_receipts.outcome IS '执行审批结果回执：命令启动或完成结果。';
COMMENT ON COLUMN execution_approval_result_receipts.exit_code IS '执行审批结果回执：命令进程退出代码。';
COMMENT ON COLUMN execution_approval_result_receipts.result_digest IS '执行审批结果回执：有界私有执行结果的内容摘要。';
COMMENT ON COLUMN execution_approval_result_receipts.result_private_json IS '执行审批结果回执：仅限授权边界读取的有界命令结果 JSON（最多 2 MiB）。';
COMMENT ON COLUMN execution_approval_result_receipts.public_error_code IS '执行审批结果回执：可公开的稳定错误代码。';
COMMENT ON COLUMN execution_approval_result_receipts.created_at IS '执行审批结果回执：记录创建时间。';

COMMENT ON TABLE agent_version_mcp_refs IS '保存智能体版本到 MCP 服务版本的有序依赖。';
COMMENT ON COLUMN agent_version_mcp_refs.agent_version_id IS '智能体 MCP 引用：智能体版本标识。';
COMMENT ON COLUMN agent_version_mcp_refs.mcp_server_version_id IS '智能体 MCP 引用：MCP服务版本标识。';
COMMENT ON COLUMN agent_version_mcp_refs.sort_order IS '智能体 MCP 引用：排序顺序。';

COMMENT ON TABLE agent_version_skill_refs IS '保存智能体版本到技能资产的有序依赖；运行时解析其 Current Version。';
COMMENT ON COLUMN agent_version_skill_refs.agent_version_id IS '智能体技能引用：智能体版本标识。';
COMMENT ON COLUMN agent_version_skill_refs.sort_order IS '智能体技能引用：排序顺序。';
COMMENT ON COLUMN agent_version_skill_refs.skill_asset_scope IS '智能体技能引用：技能资产范围。';
COMMENT ON COLUMN agent_version_skill_refs.skill_asset_id IS '智能体技能引用：技能资产标识。';

COMMENT ON TABLE channel_conversations IS '映射外部渠道会话与项目私有线程。';
COMMENT ON COLUMN channel_conversations.id IS '渠道会话：主键标识。';
COMMENT ON COLUMN channel_conversations.connection_id IS '渠道会话：连接标识。';
COMMENT ON COLUMN channel_conversations.owner_user_id IS '渠道会话：私有数据所有者的用户标识。';
COMMENT ON COLUMN channel_conversations.provider IS '渠道会话：外部服务提供方。';
COMMENT ON COLUMN channel_conversations.external_conversation_id IS '渠道会话：外部会话标识。';
COMMENT ON COLUMN channel_conversations.external_topic_id IS '渠道会话：外部主题标识。';
COMMENT ON COLUMN channel_conversations.thread_id IS '渠道会话：线程标识。';
COMMENT ON COLUMN channel_conversations.created_at IS '渠道会话：记录创建时间。';
COMMENT ON COLUMN channel_conversations.updated_at IS '渠道会话：记录最近更新时间。';
COMMENT ON COLUMN channel_conversations.project_id IS '渠道会话：所属项目标识。';

COMMENT ON TABLE channel_inbound_deliveries IS '保存渠道入站消息的幂等接收与处理状态。';
COMMENT ON COLUMN channel_inbound_deliveries.id IS '渠道入站投递：主键标识。';
COMMENT ON COLUMN channel_inbound_deliveries.project_id IS '渠道入站投递：所属项目标识。';
COMMENT ON COLUMN channel_inbound_deliveries.owner_user_id IS '渠道入站投递：私有数据所有者的用户标识。';
COMMENT ON COLUMN channel_inbound_deliveries.connection_id IS '渠道入站投递：连接标识。';
COMMENT ON COLUMN channel_inbound_deliveries.provider IS '渠道入站投递：外部服务提供方。';
COMMENT ON COLUMN channel_inbound_deliveries.external_conversation_id IS '渠道入站投递：外部会话标识。';
COMMENT ON COLUMN channel_inbound_deliveries.external_topic_id IS '渠道入站投递：外部主题标识。';
COMMENT ON COLUMN channel_inbound_deliveries.thread_id IS '渠道入站投递：线程标识。';
COMMENT ON COLUMN channel_inbound_deliveries.provider_delivery_digest IS '渠道入站投递：外部投递标识的脱敏摘要。';
COMMENT ON COLUMN channel_inbound_deliveries.run_id IS '渠道入站投递：运行标识。';
COMMENT ON COLUMN channel_inbound_deliveries.created_at IS '渠道入站投递：记录创建时间。';

COMMENT ON TABLE channel_credentials IS '保存渠道连接令牌的加密材料与有效期。';
COMMENT ON COLUMN channel_credentials.connection_id IS '渠道令牌凭据：连接标识。';
COMMENT ON COLUMN channel_credentials.encrypted_access_token IS '渠道令牌凭据：加密保存的访问令牌。';
COMMENT ON COLUMN channel_credentials.encrypted_refresh_token IS '渠道令牌凭据：加密保存的刷新令牌。';
COMMENT ON COLUMN channel_credentials.token_type IS '渠道令牌凭据：令牌类型。';
COMMENT ON COLUMN channel_credentials.expires_at IS '渠道令牌凭据：过期时间。';
COMMENT ON COLUMN channel_credentials.refresh_expires_at IS '渠道令牌凭据：刷新过期时间。';
COMMENT ON COLUMN channel_credentials.encrypted_extra_json IS '渠道令牌凭据：加密保存的扩展认证数据。';
COMMENT ON COLUMN channel_credentials.version IS '渠道令牌凭据：记录版本号。';
COMMENT ON COLUMN channel_credentials.updated_at IS '渠道令牌凭据：记录最近更新时间。';

COMMENT ON TABLE credential_envelopes IS '保存凭据版本的受管加密信封。';
COMMENT ON COLUMN credential_envelopes.id IS '凭据信封：主键标识。';
COMMENT ON COLUMN credential_envelopes.credential_version_id IS '凭据信封：凭据版本标识。';
COMMENT ON COLUMN credential_envelopes.envelope_generation IS '凭据信封：信封代次。';
COMMENT ON COLUMN credential_envelopes.key_id IS '凭据信封：键标识。';
COMMENT ON COLUMN credential_envelopes.nonce IS '凭据信封：加密信封使用的随机数。';
COMMENT ON COLUMN credential_envelopes.ciphertext IS '凭据信封：由受管密钥保护的密文。';
COMMENT ON COLUMN credential_envelopes.is_active IS '凭据信封：是否处于启用状态。';
COMMENT ON COLUMN credential_envelopes.created_by_user_id IS '凭据信封：创建操作的用户标识。';
COMMENT ON COLUMN credential_envelopes.rotated_from_envelope_id IS '凭据信封：轮换起始信封标识。';
COMMENT ON COLUMN credential_envelopes.created_at IS '凭据信封：记录创建时间。';
COMMENT ON COLUMN credential_envelopes.activated_at IS '凭据信封：启用时间。';

COMMENT ON TABLE files IS '保存项目私有文件的身份、存储元数据与生命周期。';
COMMENT ON COLUMN files.id IS '项目文件：主键标识。';
COMMENT ON COLUMN files.project_id IS '项目文件：所属项目标识。';
COMMENT ON COLUMN files.owner_user_id IS '项目文件：私有数据所有者的用户标识。';
COMMENT ON COLUMN files.thread_id IS '项目文件：线程标识。';
COMMENT ON COLUMN files.kind IS '项目文件：业务类型。';
COMMENT ON COLUMN files.logical_path IS '项目文件：文件在项目中的逻辑路径。';
COMMENT ON COLUMN files.media_type IS '项目文件：媒体类型。';
COMMENT ON COLUMN files.size IS '项目文件：大小。';
COMMENT ON COLUMN files.sha256 IS '项目文件：内容的 SHA-256 摘要。';
COMMENT ON COLUMN files.status IS '项目文件：生命周期状态。';
COMMENT ON COLUMN files.version IS '项目文件：记录版本号。';
COMMENT ON COLUMN files.created_by_run_id IS '项目文件：生成该记录的运行标识。';
COMMENT ON COLUMN files.deleted_at IS '项目文件：记录删除时间。';
COMMENT ON COLUMN files.created_at IS '项目文件：记录创建时间。';
COMMENT ON COLUMN files.updated_at IS '项目文件：记录最近更新时间。';
COMMENT ON COLUMN files.source_file_id IS '项目文件：来源文件标识。';

COMMENT ON TABLE mcp_version_credential_slots IS '声明 MCP 服务版本所需的受管凭据槽位。';
COMMENT ON COLUMN mcp_version_credential_slots.id IS 'MCP 凭据槽位：主键标识。';
COMMENT ON COLUMN mcp_version_credential_slots.mcp_server_version_id IS 'MCP 凭据槽位：MCP服务版本标识。';
COMMENT ON COLUMN mcp_version_credential_slots.name IS 'MCP 凭据槽位：名称。';
COMMENT ON COLUMN mcp_version_credential_slots.purpose IS 'MCP 凭据槽位：用途。';
COMMENT ON COLUMN mcp_version_credential_slots.payload_schema IS 'MCP 凭据槽位：载荷结构定义。';
COMMENT ON COLUMN mcp_version_credential_slots.required IS 'MCP 凭据槽位：该凭据槽位是否必需。';

COMMENT ON TABLE mcp_tool_discovery_attempts IS '记录 MCP 工具清单发现任务的尝试与结论。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.job_id IS 'MCP 工具发现尝试：任务标识。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.project_id IS 'MCP 工具发现尝试：所属项目标识。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.mcp_server_id IS 'MCP 工具发现尝试：MCP服务标识。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.mcp_server_version_id IS 'MCP 工具发现尝试：MCP服务版本标识。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.requested_by_user_id IS 'MCP 工具发现尝试：发起请求的用户标识。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.trigger IS 'MCP 工具发现尝试：触发方式。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.payload_checksum IS 'MCP 工具发现尝试：载荷内容校验和。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.grant_digest IS 'MCP 工具发现尝试：凭据授权闭包的摘要。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.result_status IS 'MCP 工具发现尝试：结果状态。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.public_error_code IS 'MCP 工具发现尝试：可公开的稳定错误代码。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.requested_at IS 'MCP 工具发现尝试：请求时间。';
COMMENT ON COLUMN mcp_tool_discovery_attempts.revision IS 'MCP 工具发现尝试：配置修订号。';

COMMENT ON TABLE project_mcp_tool_inventories IS '保存项目 MCP 服务版本最近发现的诊断性工具清单。';
COMMENT ON COLUMN project_mcp_tool_inventories.project_id IS '项目 MCP 工具清单：所属项目标识。';
COMMENT ON COLUMN project_mcp_tool_inventories.mcp_server_version_id IS '项目 MCP 工具清单：MCP服务版本标识。';
COMMENT ON COLUMN project_mcp_tool_inventories.mcp_server_id IS '项目 MCP 工具清单：MCP服务标识。';
COMMENT ON COLUMN project_mcp_tool_inventories.attempt_payload_checksum IS '项目 MCP 工具清单：尝试载荷校验和。';
COMMENT ON COLUMN project_mcp_tool_inventories.attempt_grant_digest IS '项目 MCP 工具清单：本次尝试授权闭包的摘要。';
COMMENT ON COLUMN project_mcp_tool_inventories.attempt_status IS '项目 MCP 工具清单：尝试状态。';
COMMENT ON COLUMN project_mcp_tool_inventories.public_error_code IS '项目 MCP 工具清单：可公开的稳定错误代码。';
COMMENT ON COLUMN project_mcp_tool_inventories.tools IS '项目 MCP 工具清单：发现或配置的工具列表。';
COMMENT ON COLUMN project_mcp_tool_inventories.tools_payload_checksum IS '项目 MCP 工具清单：工具载荷校验和。';
COMMENT ON COLUMN project_mcp_tool_inventories.tools_grant_digest IS '项目 MCP 工具清单：工具授权闭包的摘要。';
COMMENT ON COLUMN project_mcp_tool_inventories.last_attempt_at IS '项目 MCP 工具清单：最近尝试时间。';
COMMENT ON COLUMN project_mcp_tool_inventories.last_success_at IS '项目 MCP 工具清单：最近成功时间。';
COMMENT ON COLUMN project_mcp_tool_inventories.revision IS '项目 MCP 工具清单：配置修订号。';

COMMENT ON TABLE project_system_agent_bindings IS '保存项目对系统智能体资产的启用绑定。';
COMMENT ON COLUMN project_system_agent_bindings.project_id IS '项目系统智能体绑定：所属项目标识。';
COMMENT ON COLUMN project_system_agent_bindings.system_agent_id IS '项目系统智能体绑定：系统智能体标识。';
COMMENT ON COLUMN project_system_agent_bindings.system_asset_scope IS '项目系统智能体绑定：系统资产范围。';
COMMENT ON COLUMN project_system_agent_bindings.enabled IS '项目系统智能体绑定：是否启用。';
COMMENT ON COLUMN project_system_agent_bindings.version IS '项目系统智能体绑定：记录版本号。';
COMMENT ON COLUMN project_system_agent_bindings.created_by_user_id IS '项目系统智能体绑定：创建操作的用户标识。';
COMMENT ON COLUMN project_system_agent_bindings.updated_by_user_id IS '项目系统智能体绑定：最近更新操作的用户标识。';
COMMENT ON COLUMN project_system_agent_bindings.created_at IS '项目系统智能体绑定：记录创建时间。';
COMMENT ON COLUMN project_system_agent_bindings.updated_at IS '项目系统智能体绑定：记录最近更新时间。';

COMMENT ON TABLE project_system_mcp_bindings IS '保存项目对系统 MCP 资产的启用绑定。';
COMMENT ON COLUMN project_system_mcp_bindings.project_id IS '项目系统 MCP 绑定：所属项目标识。';
COMMENT ON COLUMN project_system_mcp_bindings.system_mcp_server_id IS '项目系统 MCP 绑定：系统MCP服务标识。';
COMMENT ON COLUMN project_system_mcp_bindings.system_asset_scope IS '项目系统 MCP 绑定：系统资产范围。';
COMMENT ON COLUMN project_system_mcp_bindings.mcp_server_version_id IS '项目系统 MCP 绑定：MCP服务版本标识。';
COMMENT ON COLUMN project_system_mcp_bindings.enabled IS '项目系统 MCP 绑定：是否启用。';
COMMENT ON COLUMN project_system_mcp_bindings.version IS '项目系统 MCP 绑定：记录版本号。';
COMMENT ON COLUMN project_system_mcp_bindings.created_by_user_id IS '项目系统 MCP 绑定：创建操作的用户标识。';
COMMENT ON COLUMN project_system_mcp_bindings.updated_by_user_id IS '项目系统 MCP 绑定：最近更新操作的用户标识。';
COMMENT ON COLUMN project_system_mcp_bindings.created_at IS '项目系统 MCP 绑定：记录创建时间。';
COMMENT ON COLUMN project_system_mcp_bindings.updated_at IS '项目系统 MCP 绑定：记录最近更新时间。';

COMMENT ON TABLE project_system_skill_bindings IS '保存项目对系统技能资产的启用绑定。';
COMMENT ON COLUMN project_system_skill_bindings.project_id IS '项目系统技能绑定：所属项目标识。';
COMMENT ON COLUMN project_system_skill_bindings.system_skill_id IS '项目系统技能绑定：系统技能标识。';
COMMENT ON COLUMN project_system_skill_bindings.system_asset_scope IS '项目系统技能绑定：系统资产范围。';
COMMENT ON COLUMN project_system_skill_bindings.enabled IS '项目系统技能绑定：是否启用。';
COMMENT ON COLUMN project_system_skill_bindings.version IS '项目系统技能绑定：记录版本号。';
COMMENT ON COLUMN project_system_skill_bindings.created_by_user_id IS '项目系统技能绑定：创建操作的用户标识。';
COMMENT ON COLUMN project_system_skill_bindings.updated_by_user_id IS '项目系统技能绑定：最近更新操作的用户标识。';
COMMENT ON COLUMN project_system_skill_bindings.created_at IS '项目系统技能绑定：记录创建时间。';
COMMENT ON COLUMN project_system_skill_bindings.updated_at IS '项目系统技能绑定：记录最近更新时间。';

COMMENT ON TABLE scheduled_tasks IS '保存项目自动化任务定义、计划与并发策略。';
COMMENT ON COLUMN scheduled_tasks.id IS '自动化任务：主键标识。';
COMMENT ON COLUMN scheduled_tasks.project_id IS '自动化任务：所属项目标识。';
COMMENT ON COLUMN scheduled_tasks.owner_user_id IS '自动化任务：私有数据所有者的用户标识。';
COMMENT ON COLUMN scheduled_tasks.thread_id IS '自动化任务：线程标识。';
COMMENT ON COLUMN scheduled_tasks.context_mode IS '自动化任务：上下文模式。';
COMMENT ON COLUMN scheduled_tasks.agent_asset_id IS '自动化任务：智能体资产标识。';
COMMENT ON COLUMN scheduled_tasks.agent_scope IS '自动化任务：智能体范围。';
COMMENT ON COLUMN scheduled_tasks.title IS '自动化任务：标题。';
COMMENT ON COLUMN scheduled_tasks.prompt IS '自动化任务：提示词。';
COMMENT ON COLUMN scheduled_tasks.schedule_type IS '自动化任务：调度类型。';
COMMENT ON COLUMN scheduled_tasks.schedule_spec IS '自动化任务：调度计划表达式。';
COMMENT ON COLUMN scheduled_tasks.timezone IS '自动化任务：计划解释所用时区。';
COMMENT ON COLUMN scheduled_tasks.status IS '自动化任务：生命周期状态。';
COMMENT ON COLUMN scheduled_tasks.overlap_policy IS '自动化任务：重叠策略。';
COMMENT ON COLUMN scheduled_tasks.next_run_at IS '自动化任务：下次运行时间。';
COMMENT ON COLUMN scheduled_tasks.last_run_at IS '自动化任务：最近运行时间。';
COMMENT ON COLUMN scheduled_tasks.last_outcome IS '自动化任务：最近结果。';
COMMENT ON COLUMN scheduled_tasks.last_error_code IS '自动化任务：最近错误代码。';
COMMENT ON COLUMN scheduled_tasks.run_count IS '自动化任务：运行数量。';
COMMENT ON COLUMN scheduled_tasks.version IS '自动化任务：记录版本号。';
COMMENT ON COLUMN scheduled_tasks.frozen_at IS '自动化任务：冻结时间。';
COMMENT ON COLUMN scheduled_tasks.deleted_at IS '自动化任务：记录删除时间。';
COMMENT ON COLUMN scheduled_tasks.created_at IS '自动化任务：记录创建时间。';
COMMENT ON COLUMN scheduled_tasks.updated_at IS '自动化任务：记录最近更新时间。';

COMMENT ON TABLE skill_version_files IS '保存技能版本归档内文件的路径与内容。';
COMMENT ON COLUMN skill_version_files.skill_version_id IS '技能版本文件：技能版本标识。';
COMMENT ON COLUMN skill_version_files.path IS '技能版本文件：路径。';
COMMENT ON COLUMN skill_version_files.media_type IS '技能版本文件：媒体类型。';
COMMENT ON COLUMN skill_version_files.size_bytes IS '技能版本文件：大小字节数。';
COMMENT ON COLUMN skill_version_files.sha256 IS '技能版本文件：内容的 SHA-256 摘要。';
COMMENT ON COLUMN skill_version_files.content IS '技能版本文件：技能版本文件的原始字节内容。';

COMMENT ON TABLE thread_event_sequences IS '保存每个私有线程下一条事件序号的单例状态。';
COMMENT ON COLUMN thread_event_sequences.project_id IS '线程事件序列：所属项目标识。';
COMMENT ON COLUMN thread_event_sequences.owner_user_id IS '线程事件序列：私有数据所有者的用户标识。';
COMMENT ON COLUMN thread_event_sequences.thread_id IS '线程事件序列：线程标识。';
COMMENT ON COLUMN thread_event_sequences.high_watermark IS '线程事件序列：已经分配的最大事件序号。';

COMMENT ON TABLE artifacts IS '保存运行生成制品的逻辑身份与存储元数据。';
COMMENT ON COLUMN artifacts.id IS '运行制品：主键标识。';
COMMENT ON COLUMN artifacts.project_id IS '运行制品：所属项目标识。';
COMMENT ON COLUMN artifacts.owner_user_id IS '运行制品：私有数据所有者的用户标识。';
COMMENT ON COLUMN artifacts.thread_id IS '运行制品：线程标识。';
COMMENT ON COLUMN artifacts.run_id IS '运行制品：运行标识。';
COMMENT ON COLUMN artifacts.file_id IS '运行制品：文件标识。';
COMMENT ON COLUMN artifacts.display_name IS '运行制品：展示名称。';
COMMENT ON COLUMN artifacts.media_type IS '运行制品：媒体类型。';
COMMENT ON COLUMN artifacts.artifact_metadata IS '运行制品：制品的结构化非敏感元数据。';
COMMENT ON COLUMN artifacts.created_at IS '运行制品：记录创建时间。';
COMMENT ON COLUMN artifacts.deleted_at IS '运行制品：记录删除时间。';

COMMENT ON TABLE execution_approval_output_delivery_obligations IS '保存审批暂停后必须由续接运行完成的私有输出交付义务。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.approval_id IS '审批输出交付义务：执行审批请求标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.project_id IS '审批输出交付义务：所属项目标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.owner_user_id IS '审批输出交付义务：私有数据所有者的用户标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.thread_id IS '审批输出交付义务：线程标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.mode IS '审批输出交付义务：履约模式。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.status IS '审批输出交付义务：生命周期状态。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.continuation_run_id IS '审批输出交付义务：审批通过后续接运行的标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.continuation_job_id IS '审批输出交付义务：审批通过后续接任务的标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.intent_tool_call_id IS '审批输出交付义务：记录输出交付意图的工具调用标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.intent_digest IS '审批输出交付义务：规范化私有输出交付意图的内容摘要。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.intent_private_json IS '审批输出交付义务：仅限授权边界读取的规范化输出交付意图 JSON（最多 1 MiB）。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.satisfied_artifact_id IS '审批输出交付义务：满足输出交付义务的运行制品标识。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.version IS '审批输出交付义务：记录版本号。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.assigned_at IS '审批输出交付义务：输出交付义务分配给续接运行的时间。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.intent_recorded_at IS '审批输出交付义务：输出交付意图持久化的时间。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.terminal_at IS '审批输出交付义务：输出交付义务进入终态的时间。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.created_at IS '审批输出交付义务：记录创建时间。';
COMMENT ON COLUMN execution_approval_output_delivery_obligations.updated_at IS '审批输出交付义务：记录最近更新时间。';

COMMENT ON TABLE execution_approval_output_delivery_candidates IS '冻结可满足审批输出交付义务的私有文件身份与版本。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.approval_id IS '审批输出交付候选：执行审批请求标识。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.file_id IS '审批输出交付候选：文件标识。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.project_id IS '审批输出交付候选：所属项目标识。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.owner_user_id IS '审批输出交付候选：私有数据所有者的用户标识。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.thread_id IS '审批输出交付候选：线程标识。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.logical_path IS '审批输出交付候选：文件在项目中的逻辑路径。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.file_version IS '审批输出交付候选：候选文件的冻结版本号。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.sha256 IS '审批输出交付候选：内容的 SHA-256 摘要。';
COMMENT ON COLUMN execution_approval_output_delivery_candidates.created_at IS '审批输出交付候选：记录创建时间。';

COMMENT ON TABLE credential_grants IS '保存项目凭据向智能体或 MCP 目标的授权。';
COMMENT ON COLUMN credential_grants.id IS '凭据授权：主键标识。';
COMMENT ON COLUMN credential_grants.mcp_server_version_id IS '凭据授权：MCP服务版本标识。';
COMMENT ON COLUMN credential_grants.credential_slot_id IS '凭据授权：凭据槽位标识。';
COMMENT ON COLUMN credential_grants.credential_version_id IS '凭据授权：凭据版本标识。';
COMMENT ON COLUMN credential_grants.status IS '凭据授权：生命周期状态。';
COMMENT ON COLUMN credential_grants.version IS '凭据授权：记录版本号。';
COMMENT ON COLUMN credential_grants.created_by_user_id IS '凭据授权：创建操作的用户标识。';
COMMENT ON COLUMN credential_grants.created_at IS '凭据授权：记录创建时间。';
COMMENT ON COLUMN credential_grants.revoked_at IS '凭据授权：撤销时间。';
COMMENT ON COLUMN credential_grants.revoked_by_user_id IS '凭据授权：执行撤销的用户标识。';

COMMENT ON TABLE file_chunks IS '保存项目文件的有序二进制分块。';
COMMENT ON COLUMN file_chunks.file_id IS '文件分块：文件标识。';
COMMENT ON COLUMN file_chunks.chunk_index IS '文件分块：分块序号。';
COMMENT ON COLUMN file_chunks.content IS '文件分块：文件分块的原始字节内容。';
COMMENT ON COLUMN file_chunks.size IS '文件分块：大小。';
COMMENT ON COLUMN file_chunks.sha256 IS '文件分块：内容的 SHA-256 摘要。';

COMMENT ON TABLE run_mcp_grant_snapshots IS '冻结运行使用 MCP 服务时的凭据授权闭包。';
COMMENT ON COLUMN run_mcp_grant_snapshots.project_id IS '运行 MCP 授权快照：所属项目标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.owner_user_id IS '运行 MCP 授权快照：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.thread_id IS '运行 MCP 授权快照：线程标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.run_id IS '运行 MCP 授权快照：运行标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.mcp_version_id IS '运行 MCP 授权快照：MCP版本标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.credential_slot_id IS '运行 MCP 授权快照：凭据槽位标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.credential_grant_id IS '运行 MCP 授权快照：凭据授权标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.credential_version_id IS '运行 MCP 授权快照：凭据版本标识。';
COMMENT ON COLUMN run_mcp_grant_snapshots.created_at IS '运行 MCP 授权快照：记录创建时间。';

COMMENT ON TABLE skill_design_sessions IS '保存技能设计向导的私有会话状态与产物引用。';
COMMENT ON COLUMN skill_design_sessions.id IS '技能设计会话：主键标识。';
COMMENT ON COLUMN skill_design_sessions.project_id IS '技能设计会话：所属项目标识。';
COMMENT ON COLUMN skill_design_sessions.owner_user_id IS '技能设计会话：私有数据所有者的用户标识。';
COMMENT ON COLUMN skill_design_sessions.thread_id IS '技能设计会话：线程标识。';
COMMENT ON COLUMN skill_design_sessions.slug IS '技能设计会话：稳定可读标识名。';
COMMENT ON COLUMN skill_design_sessions.display_name IS '技能设计会话：展示名称。';
COMMENT ON COLUMN skill_design_sessions.status IS '技能设计会话：生命周期状态。';
COMMENT ON COLUMN skill_design_sessions.revision IS '技能设计会话：配置修订号。';
COMMENT ON COLUMN skill_design_sessions.messages_json IS '技能设计会话：设计会话消息列表。';
COMMENT ON COLUMN skill_design_sessions.progress_json IS '技能设计会话：设计会话进度列表。';
COMMENT ON COLUMN skill_design_sessions.active_clarification_json IS '技能设计会话：活跃澄清JSON 数据。';
COMMENT ON COLUMN skill_design_sessions.draft_checksum IS '技能设计会话：草稿校验和。';
COMMENT ON COLUMN skill_design_sessions.validation_json IS '技能设计会话：草稿验证结果。';
COMMENT ON COLUMN skill_design_sessions.validated_draft_checksum IS '技能设计会话：验证草稿校验和。';
COMMENT ON COLUMN skill_design_sessions.skill_creator_skill_id IS '技能设计会话：技能创建器技能标识。';
COMMENT ON COLUMN skill_design_sessions.skill_creator_version_id IS '技能设计会话：技能创建器版本标识。';
COMMENT ON COLUMN skill_design_sessions.skill_creator_payload_checksum IS '技能设计会话：技能创建器载荷校验和。';
COMMENT ON COLUMN skill_design_sessions.error_code IS '技能设计会话：稳定错误代码。';
COMMENT ON COLUMN skill_design_sessions.error_message IS '技能设计会话：受限的错误说明。';
COMMENT ON COLUMN skill_design_sessions.created_skill_id IS '技能设计会话：创建技能标识。';
COMMENT ON COLUMN skill_design_sessions.created_skill_version_id IS '技能设计会话：创建技能版本标识。';
COMMENT ON COLUMN skill_design_sessions.created_skill_deleted IS '技能设计会话：已创建的技能是否随后删除。';
COMMENT ON COLUMN skill_design_sessions.create_idempotency_key_hash IS '技能设计会话：创建操作幂等键的不可逆哈希。';
COMMENT ON COLUMN skill_design_sessions.create_request_checksum IS '技能设计会话：创建请求校验和。';
COMMENT ON COLUMN skill_design_sessions.created_at IS '技能设计会话：记录创建时间。';
COMMENT ON COLUMN skill_design_sessions.updated_at IS '技能设计会话：记录最近更新时间。';
COMMENT ON COLUMN skill_design_sessions.authoring_dependencies_json IS '技能设计会话：编写用途依赖JSON 数据。';
COMMENT ON COLUMN skill_design_sessions.session_kind IS '技能设计会话：会话类型。';
COMMENT ON COLUMN skill_design_sessions.target_skill_id IS '技能设计会话：目标技能标识。';
COMMENT ON COLUMN skill_design_sessions.base_version_id IS '技能设计会话：基线版本标识。';
COMMENT ON COLUMN skill_design_sessions.base_version_number IS '技能设计会话：基线版本编号。';
COMMENT ON COLUMN skill_design_sessions.base_payload_checksum IS '技能设计会话：基线载荷校验和。';
COMMENT ON COLUMN skill_design_sessions.target_skill_deleted IS '技能设计会话：目标技能已删除。';

COMMENT ON TABLE skill_design_operations IS '保存技能设计会话中的幂等操作及其结果。';
COMMENT ON COLUMN skill_design_operations.id IS '技能设计操作：主键标识。';
COMMENT ON COLUMN skill_design_operations.project_id IS '技能设计操作：所属项目标识。';
COMMENT ON COLUMN skill_design_operations.owner_user_id IS '技能设计操作：私有数据所有者的用户标识。';
COMMENT ON COLUMN skill_design_operations.session_id IS '技能设计操作：会话标识。';
COMMENT ON COLUMN skill_design_operations.operation_kind IS '技能设计操作：操作类型。';
COMMENT ON COLUMN skill_design_operations.idempotency_key_hash IS '技能设计操作：幂等操作键的不可逆哈希。';
COMMENT ON COLUMN skill_design_operations.request_checksum IS '技能设计操作：请求校验和。';
COMMENT ON COLUMN skill_design_operations.status IS '技能设计操作：生命周期状态。';
COMMENT ON COLUMN skill_design_operations.result_revision IS '技能设计操作：结果修订号。';
COMMENT ON COLUMN skill_design_operations.public_error_code IS '技能设计操作：可公开的稳定错误代码。';
COMMENT ON COLUMN skill_design_operations.created_at IS '技能设计操作：记录创建时间。';
COMMENT ON COLUMN skill_design_operations.updated_at IS '技能设计操作：记录最近更新时间。';
COMMENT ON COLUMN skill_design_operations.run_id IS '技能设计操作：运行标识。';
COMMENT ON COLUMN skill_design_operations.terminal_kind IS '技能设计操作：终态类型。';
COMMENT ON COLUMN skill_design_operations.terminal_request_checksum IS '技能设计操作：终态请求校验和。';

COMMENT ON TABLE skill_design_draft_files IS '保存技能设计会话当前草稿中的文件内容。';
COMMENT ON COLUMN skill_design_draft_files.project_id IS '技能设计草稿文件：所属项目标识。';
COMMENT ON COLUMN skill_design_draft_files.owner_user_id IS '技能设计草稿文件：私有数据所有者的用户标识。';
COMMENT ON COLUMN skill_design_draft_files.session_id IS '技能设计草稿文件：会话标识。';
COMMENT ON COLUMN skill_design_draft_files.path IS '技能设计草稿文件：路径。';
COMMENT ON COLUMN skill_design_draft_files.media_type IS '技能设计草稿文件：媒体类型。';
COMMENT ON COLUMN skill_design_draft_files.size_bytes IS '技能设计草稿文件：大小字节数。';
COMMENT ON COLUMN skill_design_draft_files.sha256 IS '技能设计草稿文件：内容的 SHA-256 摘要。';
COMMENT ON COLUMN skill_design_draft_files.content IS '技能设计草稿文件：技能设计草稿文件的原始字节内容。';
COMMENT ON COLUMN skill_design_draft_files.updated_at IS '技能设计草稿文件：记录最近更新时间。';

COMMENT ON TABLE project_channel_credential_bindings IS '保存项目渠道实例使用的受管凭据版本绑定。';
COMMENT ON COLUMN project_channel_credential_bindings.id IS '渠道凭据绑定：主键标识。';
COMMENT ON COLUMN project_channel_credential_bindings.project_id IS '渠道凭据绑定：所属项目标识。';
COMMENT ON COLUMN project_channel_credential_bindings.channel_instance_id IS '渠道凭据绑定：渠道实例标识。';
COMMENT ON COLUMN project_channel_credential_bindings.credential_id IS '渠道凭据绑定：凭据标识。';
COMMENT ON COLUMN project_channel_credential_bindings.credential_version_id IS '渠道凭据绑定：凭据版本标识。';
COMMENT ON COLUMN project_channel_credential_bindings.binding_revision IS '渠道凭据绑定：绑定修订号。';
COMMENT ON COLUMN project_channel_credential_bindings.status IS '渠道凭据绑定：生命周期状态。';
COMMENT ON COLUMN project_channel_credential_bindings.created_by_user_id IS '渠道凭据绑定：创建操作的用户标识。';
COMMENT ON COLUMN project_channel_credential_bindings.created_at IS '渠道凭据绑定：记录创建时间。';
COMMENT ON COLUMN project_channel_credential_bindings.revoked_at IS '渠道凭据绑定：撤销时间。';
COMMENT ON COLUMN project_channel_credential_bindings.revoked_by_user_id IS '渠道凭据绑定：执行撤销的用户标识。';

COMMENT ON TABLE project_skill_credential_configs IS '保存项目技能版本凭据配置的修订状态。';
COMMENT ON COLUMN project_skill_credential_configs.project_id IS '技能凭据配置：所属项目标识。';
COMMENT ON COLUMN project_skill_credential_configs.skill_id IS '技能凭据配置：技能标识。';
COMMENT ON COLUMN project_skill_credential_configs.skill_version_id IS '技能凭据配置：技能版本标识。';
COMMENT ON COLUMN project_skill_credential_configs.revision IS '技能凭据配置：配置修订号。';
COMMENT ON COLUMN project_skill_credential_configs.created_by_user_id IS '技能凭据配置：创建操作的用户标识。';
COMMENT ON COLUMN project_skill_credential_configs.updated_by_user_id IS '技能凭据配置：最近更新操作的用户标识。';
COMMENT ON COLUMN project_skill_credential_configs.created_at IS '技能凭据配置：记录创建时间。';
COMMENT ON COLUMN project_skill_credential_configs.updated_at IS '技能凭据配置：记录最近更新时间。';

COMMENT ON TABLE project_skill_credential_bindings IS '保存技能凭据槽位到受管凭据版本的绑定。';
COMMENT ON COLUMN project_skill_credential_bindings.id IS '技能凭据绑定：主键标识。';
COMMENT ON COLUMN project_skill_credential_bindings.project_id IS '技能凭据绑定：所属项目标识。';
COMMENT ON COLUMN project_skill_credential_bindings.skill_id IS '技能凭据绑定：技能标识。';
COMMENT ON COLUMN project_skill_credential_bindings.skill_version_id IS '技能凭据绑定：技能版本标识。';
COMMENT ON COLUMN project_skill_credential_bindings.secret_name IS '技能凭据绑定：机密项名称。';
COMMENT ON COLUMN project_skill_credential_bindings.credential_id IS '技能凭据绑定：凭据标识。';
COMMENT ON COLUMN project_skill_credential_bindings.credential_version_id IS '技能凭据绑定：凭据版本标识。';
COMMENT ON COLUMN project_skill_credential_bindings.config_revision IS '技能凭据绑定：配置修订号。';
COMMENT ON COLUMN project_skill_credential_bindings.status IS '技能凭据绑定：生命周期状态。';
COMMENT ON COLUMN project_skill_credential_bindings.created_by_user_id IS '技能凭据绑定：创建操作的用户标识。';
COMMENT ON COLUMN project_skill_credential_bindings.created_at IS '技能凭据绑定：记录创建时间。';
COMMENT ON COLUMN project_skill_credential_bindings.revoked_at IS '技能凭据绑定：撤销时间。';
COMMENT ON COLUMN project_skill_credential_bindings.revoked_by_user_id IS '技能凭据绑定：执行撤销的用户标识。';
COMMENT ON COLUMN project_skill_credential_bindings.source_env_field_name IS '技能凭据绑定：来源环境变量字段名称。';
COMMENT ON COLUMN project_skill_credential_bindings.admission_only IS '技能凭据绑定：仅供已准入运行继续验证的退役权限标记。';
COMMENT ON COLUMN project_skill_credential_bindings.runtime_authority_binding_id IS '技能凭据绑定：退役绑定关联的当前运行权限绑定标识。';

COMMENT ON TABLE run_skill_credential_snapshots IS '冻结运行使用技能时的凭据绑定闭包。';
COMMENT ON COLUMN run_skill_credential_snapshots.project_id IS '运行技能凭据快照：所属项目标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.owner_user_id IS '运行技能凭据快照：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.thread_id IS '运行技能凭据快照：线程标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.run_id IS '运行技能凭据快照：运行标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.skill_id IS '运行技能凭据快照：技能标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.skill_version_id IS '运行技能凭据快照：技能版本标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.secret_name IS '运行技能凭据快照：机密项名称。';
COMMENT ON COLUMN run_skill_credential_snapshots.skill_credential_binding_id IS '运行技能凭据快照：技能凭据绑定标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.binding_revision IS '运行技能凭据快照：绑定修订号。';
COMMENT ON COLUMN run_skill_credential_snapshots.credential_id IS '运行技能凭据快照：凭据标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.credential_version_id IS '运行技能凭据快照：凭据版本标识。';
COMMENT ON COLUMN run_skill_credential_snapshots.created_at IS '运行技能凭据快照：记录创建时间。';
COMMENT ON COLUMN run_skill_credential_snapshots.source_env_field_name IS '运行技能凭据快照：来源环境变量字段名称。';

COMMENT ON TABLE system_model_catalog_state IS '记录系统模型目录的单例修订号。';
COMMENT ON COLUMN system_model_catalog_state.id IS '系统模型目录状态：主键标识。';
COMMENT ON COLUMN system_model_catalog_state.revision IS '系统模型目录状态：配置修订号。';
COMMENT ON COLUMN system_model_catalog_state.default_model_config_id IS '系统模型目录状态：默认模型配置标识。';
COMMENT ON COLUMN system_model_catalog_state.updated_by_user_id IS '系统模型目录状态：最近更新操作的用户标识。';
COMMENT ON COLUMN system_model_catalog_state.created_at IS '系统模型目录状态：记录创建时间。';
COMMENT ON COLUMN system_model_catalog_state.updated_at IS '系统模型目录状态：记录最近更新时间。';

COMMENT ON TABLE system_model_configs IS '保存系统模型配置的稳定标识、展示名称和当前版本指针。';
COMMENT ON COLUMN system_model_configs.id IS '系统模型配置：主键标识。';
COMMENT ON COLUMN system_model_configs.display_name IS '系统模型配置：展示名称。';
COMMENT ON COLUMN system_model_configs.status IS '系统模型配置：生命周期状态。';
COMMENT ON COLUMN system_model_configs.current_version_id IS '系统模型配置：当前版本标识。';
COMMENT ON COLUMN system_model_configs.revision IS '系统模型配置：配置修订号。';
COMMENT ON COLUMN system_model_configs.created_by_user_id IS '系统模型配置：创建操作的用户标识。';
COMMENT ON COLUMN system_model_configs.updated_by_user_id IS '系统模型配置：最近更新操作的用户标识。';
COMMENT ON COLUMN system_model_configs.created_at IS '系统模型配置：记录创建时间。';
COMMENT ON COLUMN system_model_configs.updated_at IS '系统模型配置：记录最近更新时间。';

COMMENT ON TABLE system_model_config_versions IS '保存不可变的系统模型提供方与能力配置。';
COMMENT ON COLUMN system_model_config_versions.id IS '系统模型配置版本：主键标识。';
COMMENT ON COLUMN system_model_config_versions.model_config_id IS '系统模型配置版本：模型配置标识。';
COMMENT ON COLUMN system_model_config_versions.version_number IS '系统模型配置版本：版本编号。';
COMMENT ON COLUMN system_model_config_versions.provider_adapter IS '系统模型配置版本：提供方适配器。';
COMMENT ON COLUMN system_model_config_versions.provider_model IS '系统模型配置版本：提供方模型。';
COMMENT ON COLUMN system_model_config_versions.settings IS '系统模型配置版本：运行策略设置。';
COMMENT ON COLUMN system_model_config_versions.supports_thinking IS '系统模型配置版本：是否支持思考。';
COMMENT ON COLUMN system_model_config_versions.supports_reasoning_effort IS '系统模型配置版本：是否支持推理强度。';
COMMENT ON COLUMN system_model_config_versions.supports_vision IS '系统模型配置版本：是否支持视觉。';
COMMENT ON COLUMN system_model_config_versions.credential_id IS '系统模型配置版本：凭据标识。';
COMMENT ON COLUMN system_model_config_versions.credential_version_id IS '系统模型配置版本：凭据版本标识。';
COMMENT ON COLUMN system_model_config_versions.credential_env_key IS '系统模型配置版本：凭据环境变量键。';
COMMENT ON COLUMN system_model_config_versions.payload_checksum IS '系统模型配置版本：载荷内容校验和。';
COMMENT ON COLUMN system_model_config_versions.supersedes_version_id IS '系统模型配置版本：替代目标版本标识。';
COMMENT ON COLUMN system_model_config_versions.created_by_user_id IS '系统模型配置版本：创建操作的用户标识。';
COMMENT ON COLUMN system_model_config_versions.created_at IS '系统模型配置版本：记录创建时间。';

COMMENT ON TABLE run_model_config_snapshots IS '冻结一次运行采用的模型配置版本。';
COMMENT ON COLUMN run_model_config_snapshots.project_id IS '运行模型配置快照：所属项目标识。';
COMMENT ON COLUMN run_model_config_snapshots.owner_user_id IS '运行模型配置快照：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_model_config_snapshots.thread_id IS '运行模型配置快照：线程标识。';
COMMENT ON COLUMN run_model_config_snapshots.run_id IS '运行模型配置快照：运行标识。';
COMMENT ON COLUMN run_model_config_snapshots.purpose IS '运行模型配置快照：用途。';
COMMENT ON COLUMN run_model_config_snapshots.model_config_id IS '运行模型配置快照：模型配置标识。';
COMMENT ON COLUMN run_model_config_snapshots.model_config_version_id IS '运行模型配置快照：模型配置版本标识。';
COMMENT ON COLUMN run_model_config_snapshots.payload_checksum IS '运行模型配置快照：载荷内容校验和。';
COMMENT ON COLUMN run_model_config_snapshots.credential_id IS '运行模型配置快照：凭据标识。';
COMMENT ON COLUMN run_model_config_snapshots.credential_version_id IS '运行模型配置快照：凭据版本标识。';
COMMENT ON COLUMN run_model_config_snapshots.credential_env_key IS '运行模型配置快照：凭据环境变量键。';
COMMENT ON COLUMN run_model_config_snapshots.created_at IS '运行模型配置快照：记录创建时间。';

COMMENT ON TABLE system_runtime_policy_catalog_state IS '记录系统运行策略目录的单例修订号。';
COMMENT ON COLUMN system_runtime_policy_catalog_state.id IS '系统运行策略目录状态：主键标识。';
COMMENT ON COLUMN system_runtime_policy_catalog_state.revision IS '系统运行策略目录状态：配置修订号。';
COMMENT ON COLUMN system_runtime_policy_catalog_state.updated_by_user_id IS '系统运行策略目录状态：最近更新操作的用户标识。';
COMMENT ON COLUMN system_runtime_policy_catalog_state.created_at IS '系统运行策略目录状态：记录创建时间。';
COMMENT ON COLUMN system_runtime_policy_catalog_state.updated_at IS '系统运行策略目录状态：记录最近更新时间。';

COMMENT ON TABLE system_runtime_policies IS '保存各策略节当前采用的版本指针。';
COMMENT ON COLUMN system_runtime_policies.section IS '系统运行策略：策略节。';
COMMENT ON COLUMN system_runtime_policies.current_version_id IS '系统运行策略：当前版本标识。';
COMMENT ON COLUMN system_runtime_policies.revision IS '系统运行策略：配置修订号。';
COMMENT ON COLUMN system_runtime_policies.updated_by_user_id IS '系统运行策略：最近更新操作的用户标识。';
COMMENT ON COLUMN system_runtime_policies.created_at IS '系统运行策略：记录创建时间。';
COMMENT ON COLUMN system_runtime_policies.updated_at IS '系统运行策略：记录最近更新时间。';

COMMENT ON TABLE system_runtime_policy_versions IS '保存不可变的系统运行策略载荷。';
COMMENT ON COLUMN system_runtime_policy_versions.id IS '系统运行策略版本：主键标识。';
COMMENT ON COLUMN system_runtime_policy_versions.section IS '系统运行策略版本：策略节。';
COMMENT ON COLUMN system_runtime_policy_versions.version_number IS '系统运行策略版本：版本编号。';
COMMENT ON COLUMN system_runtime_policy_versions.schema_version IS '系统运行策略版本：架构版本号。';
COMMENT ON COLUMN system_runtime_policy_versions.value IS '系统运行策略版本：值。';
COMMENT ON COLUMN system_runtime_policy_versions.payload_checksum IS '系统运行策略版本：载荷内容校验和。';
COMMENT ON COLUMN system_runtime_policy_versions.supersedes_version_id IS '系统运行策略版本：替代目标版本标识。';
COMMENT ON COLUMN system_runtime_policy_versions.created_by_user_id IS '系统运行策略版本：创建操作的用户标识。';
COMMENT ON COLUMN system_runtime_policy_versions.created_at IS '系统运行策略版本：记录创建时间。';

COMMENT ON TABLE run_runtime_policy_snapshots IS '冻结一次运行采用的系统运行策略版本。';
COMMENT ON COLUMN run_runtime_policy_snapshots.project_id IS '运行策略快照：所属项目标识。';
COMMENT ON COLUMN run_runtime_policy_snapshots.owner_user_id IS '运行策略快照：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_runtime_policy_snapshots.thread_id IS '运行策略快照：线程标识。';
COMMENT ON COLUMN run_runtime_policy_snapshots.run_id IS '运行策略快照：运行标识。';
COMMENT ON COLUMN run_runtime_policy_snapshots.section IS '运行策略快照：策略节。';
COMMENT ON COLUMN run_runtime_policy_snapshots.policy_version_id IS '运行策略快照：策略版本标识。';
COMMENT ON COLUMN run_runtime_policy_snapshots.schema_version IS '运行策略快照：架构版本号。';
COMMENT ON COLUMN run_runtime_policy_snapshots.payload_checksum IS '运行策略快照：载荷内容校验和。';
COMMENT ON COLUMN run_runtime_policy_snapshots.created_at IS '运行策略快照：记录创建时间。';

COMMENT ON TABLE memory_documents IS '保存项目用户命名空间下的当前结构化记忆文档。';
COMMENT ON COLUMN memory_documents.project_id IS '记忆文档：所属项目标识。';
COMMENT ON COLUMN memory_documents.owner_user_id IS '记忆文档：私有数据所有者的用户标识。';
COMMENT ON COLUMN memory_documents.namespace IS '记忆文档：私有数据命名空间。';
COMMENT ON COLUMN memory_documents.content IS '记忆文档：当前结构化记忆文档正文（属于私有内容）。';
COMMENT ON COLUMN memory_documents.content_digest IS '记忆文档：内容摘要。';
COMMENT ON COLUMN memory_documents.version IS '记忆文档：记录版本号。';
COMMENT ON COLUMN memory_documents.dream_cursor IS '记忆文档：记忆整理游标。';
COMMENT ON COLUMN memory_documents.active_dream_job_id IS '记忆文档：活跃记忆整理任务标识。';
COMMENT ON COLUMN memory_documents.updated_at IS '记忆文档：记录最近更新时间。';
COMMENT ON COLUMN memory_documents.sections IS '记忆文档：有序的记忆文档章节名称。';
COMMENT ON COLUMN memory_documents.sections_policy_section IS '记忆文档：约束章节结构的运行策略节。';
COMMENT ON COLUMN memory_documents.sections_policy_version_id IS '记忆文档：约束章节结构的运行策略版本标识。';

COMMENT ON TABLE memory_history_entries IS '保存等待整理或已消费的记忆输入历史。';
COMMENT ON COLUMN memory_history_entries.id IS '记忆历史条目：主键标识。';
COMMENT ON COLUMN memory_history_entries.sequence IS '记忆历史条目：单调序号。';
COMMENT ON COLUMN memory_history_entries.project_id IS '记忆历史条目：所属项目标识。';
COMMENT ON COLUMN memory_history_entries.owner_user_id IS '记忆历史条目：私有数据所有者的用户标识。';
COMMENT ON COLUMN memory_history_entries.namespace IS '记忆历史条目：私有数据命名空间。';
COMMENT ON COLUMN memory_history_entries.thread_id IS '记忆历史条目：线程标识。';
COMMENT ON COLUMN memory_history_entries.origin IS '记忆历史条目：来源。';
COMMENT ON COLUMN memory_history_entries.source_run_id IS '记忆历史条目：来源运行标识。';
COMMENT ON COLUMN memory_history_entries.source_checkpoint_id IS '记忆历史条目：来源检查点标识。';
COMMENT ON COLUMN memory_history_entries.committed_checkpoint_id IS '记忆历史条目：已提交检查点标识。';
COMMENT ON COLUMN memory_history_entries.source_digest IS '记忆历史条目：来源内容摘要。';
COMMENT ON COLUMN memory_history_entries.status IS '记忆历史条目：生命周期状态。';
COMMENT ON COLUMN memory_history_entries.tagged_text IS '记忆历史条目：带来源标签的记忆文本。';
COMMENT ON COLUMN memory_history_entries.content_digest IS '记忆历史条目：内容摘要。';
COMMENT ON COLUMN memory_history_entries.preference_version IS '记忆历史条目：偏好版本号。';
COMMENT ON COLUMN memory_history_entries.snip_prompt_version IS '记忆历史条目：摘录提示词版本号。';
COMMENT ON COLUMN memory_history_entries.summary_model_ref IS '记忆历史条目：摘要模型引用。';
COMMENT ON COLUMN memory_history_entries.dream_job_id IS '记忆历史条目：记忆整理任务标识。';
COMMENT ON COLUMN memory_history_entries.created_at IS '记忆历史条目：记录创建时间。';
COMMENT ON COLUMN memory_history_entries.consumed_at IS '记忆历史条目：消费时间。';

COMMENT ON TABLE memory_dream_runs IS '保存一次记忆整理任务的输入范围与结算版本。';
COMMENT ON COLUMN memory_dream_runs.job_id IS '记忆整理运行：任务标识。';
COMMENT ON COLUMN memory_dream_runs.project_id IS '记忆整理运行：所属项目标识。';
COMMENT ON COLUMN memory_dream_runs.owner_user_id IS '记忆整理运行：私有数据所有者的用户标识。';
COMMENT ON COLUMN memory_dream_runs.namespace IS '记忆整理运行：私有数据命名空间。';
COMMENT ON COLUMN memory_dream_runs.trigger IS '记忆整理运行：触发方式。';
COMMENT ON COLUMN memory_dream_runs.history_from IS '记忆整理运行：本次记忆整理的起始历史序号。';
COMMENT ON COLUMN memory_dream_runs.history_to IS '记忆整理运行：本次记忆整理的结束历史序号。';
COMMENT ON COLUMN memory_dream_runs.history_count IS '记忆整理运行：历史数量。';
COMMENT ON COLUMN memory_dream_runs.history_digest IS '记忆整理运行：历史摘要。';
COMMENT ON COLUMN memory_dream_runs.base_document_version IS '记忆整理运行：基线文档版本号。';
COMMENT ON COLUMN memory_dream_runs.base_content_digest IS '记忆整理运行：基线内容摘要。';
COMMENT ON COLUMN memory_dream_runs.preference_version IS '记忆整理运行：偏好版本号。';
COMMENT ON COLUMN memory_dream_runs.policy_revision IS '记忆整理运行：策略修订号。';
COMMENT ON COLUMN memory_dream_runs.model_ref IS '记忆整理运行：模型引用。';
COMMENT ON COLUMN memory_dream_runs.prompt_version IS '记忆整理运行：提示词版本号。';
COMMENT ON COLUMN memory_dream_runs.result_version IS '记忆整理运行：结果版本号。';
COMMENT ON COLUMN memory_dream_runs.created_at IS '记忆整理运行：记录创建时间。';
COMMENT ON COLUMN memory_dream_runs.completed_at IS '记忆整理运行：完成时间。';

COMMENT ON TABLE memory_dream_prepare_runs IS '保存线程消息排空、进度恢复与子记忆整理任务准入的持久化状态。';
COMMENT ON COLUMN memory_dream_prepare_runs.job_id IS '记忆整理准备运行：任务标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.project_id IS '记忆整理准备运行：所属项目标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.owner_user_id IS '记忆整理准备运行：私有数据所有者的用户标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.namespace IS '记忆整理准备运行：私有数据命名空间。';
COMMENT ON COLUMN memory_dream_prepare_runs.thread_id IS '记忆整理准备运行：线程标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.operation_id IS '记忆整理准备运行：操作标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.request_id IS '记忆整理准备运行：请求标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.phase IS '记忆整理准备运行：阶段。';
COMMENT ON COLUMN memory_dream_prepare_runs.compacted_passes IS '记忆整理准备运行：压缩轮次。';
COMMENT ON COLUMN memory_dream_prepare_runs.last_checkpoint_id IS '记忆整理准备运行：最近检查点标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.dream_job_id IS '记忆整理准备运行：记忆整理任务标识。';
COMMENT ON COLUMN memory_dream_prepare_runs.history_count IS '记忆整理准备运行：历史数量。';
COMMENT ON COLUMN memory_dream_prepare_runs.admission_kind IS '记忆整理准备运行：准入类型。';
COMMENT ON COLUMN memory_dream_prepare_runs.result_disposition IS '记忆整理准备运行：结果处置。';
COMMENT ON COLUMN memory_dream_prepare_runs.created_at IS '记忆整理准备运行：记录创建时间。';
COMMENT ON COLUMN memory_dream_prepare_runs.updated_at IS '记忆整理准备运行：记录最近更新时间。';
COMMENT ON COLUMN memory_dream_prepare_runs.completed_at IS '记忆整理准备运行：完成时间。';

COMMENT ON TABLE memory_document_versions IS '保存记忆文档每次变更的不可变版本与差异。';
COMMENT ON COLUMN memory_document_versions.project_id IS '记忆文档版本：所属项目标识。';
COMMENT ON COLUMN memory_document_versions.owner_user_id IS '记忆文档版本：私有数据所有者的用户标识。';
COMMENT ON COLUMN memory_document_versions.namespace IS '记忆文档版本：私有数据命名空间。';
COMMENT ON COLUMN memory_document_versions.version IS '记忆文档版本：记录版本号。';
COMMENT ON COLUMN memory_document_versions.content IS '记忆文档版本：该版本的结构化记忆文档正文（属于私有内容）。';
COMMENT ON COLUMN memory_document_versions.content_digest IS '记忆文档版本：内容摘要。';
COMMENT ON COLUMN memory_document_versions.unified_diff IS '记忆文档版本：相对上一记忆版本的统一格式差异。';
COMMENT ON COLUMN memory_document_versions.trigger IS '记忆文档版本：触发方式。';
COMMENT ON COLUMN memory_document_versions.dream_job_id IS '记忆文档版本：记忆整理任务标识。';
COMMENT ON COLUMN memory_document_versions.history_from IS '记忆文档版本：本次记忆整理的起始历史序号。';
COMMENT ON COLUMN memory_document_versions.history_to IS '记忆文档版本：本次记忆整理的结束历史序号。';
COMMENT ON COLUMN memory_document_versions.history_count IS '记忆文档版本：历史数量。';
COMMENT ON COLUMN memory_document_versions.prompt_version IS '记忆文档版本：提示词版本号。';
COMMENT ON COLUMN memory_document_versions.model_ref IS '记忆文档版本：模型引用。';
COMMENT ON COLUMN memory_document_versions.needs_review IS '记忆文档版本：是否需要审核。';
COMMENT ON COLUMN memory_document_versions.created_at IS '记忆文档版本：记录创建时间。';

COMMENT ON TABLE memory_episodes IS '保存可检索的历史记忆片段归档。';
COMMENT ON COLUMN memory_episodes.id IS '记忆片段：主键标识。';
COMMENT ON COLUMN memory_episodes.project_id IS '记忆片段：所属项目标识。';
COMMENT ON COLUMN memory_episodes.owner_user_id IS '记忆片段：私有数据所有者的用户标识。';
COMMENT ON COLUMN memory_episodes.namespace IS '记忆片段：私有数据命名空间。';
COMMENT ON COLUMN memory_episodes.thread_id IS '记忆片段：线程标识。';
COMMENT ON COLUMN memory_episodes.origin IS '记忆片段：来源。';
COMMENT ON COLUMN memory_episodes.tagged_text IS '记忆片段：带来源标签的记忆文本。';
COMMENT ON COLUMN memory_episodes.content_digest IS '记忆片段：内容摘要。';
COMMENT ON COLUMN memory_episodes.occurred_at IS '记忆片段：发生时间。';
COMMENT ON COLUMN memory_episodes.consumed_dream_job_id IS '记忆片段：消费记忆整理任务标识。';
COMMENT ON COLUMN memory_episodes.created_at IS '记忆片段：记录创建时间。';

COMMENT ON TABLE run_memory_context_snapshots IS '冻结一次运行注入的记忆文档内容。';
COMMENT ON COLUMN run_memory_context_snapshots.project_id IS '运行记忆上下文快照：所属项目标识。';
COMMENT ON COLUMN run_memory_context_snapshots.owner_user_id IS '运行记忆上下文快照：私有数据所有者的用户标识。';
COMMENT ON COLUMN run_memory_context_snapshots.run_id IS '运行记忆上下文快照：运行标识。';
COMMENT ON COLUMN run_memory_context_snapshots.namespace IS '运行记忆上下文快照：私有数据命名空间。';
COMMENT ON COLUMN run_memory_context_snapshots.document_version IS '运行记忆上下文快照：文档版本号。';
COMMENT ON COLUMN run_memory_context_snapshots.content IS '运行记忆上下文快照：运行时冻结的记忆文档正文（属于私有内容）。';
COMMENT ON COLUMN run_memory_context_snapshots.content_digest IS '运行记忆上下文快照：内容摘要。';
COMMENT ON COLUMN run_memory_context_snapshots.created_at IS '运行记忆上下文快照：记录创建时间。';
COMMENT ON COLUMN run_memory_context_snapshots.sections IS '运行记忆上下文快照：有序的记忆文档章节名称。';
-- END GENERATED SCHEMA COMMENTS

-- Parent comments are present before partitions are created, allowing the
-- partition helper to copy the same descriptions to every physical child.
SELECT ensure_run_events_month_partition(now());

SELECT ensure_run_events_month_partition(now() + INTERVAL '1 month');

INSERT INTO system_runtime_policy_catalog_state (id, revision) VALUES (1, 1);

INSERT INTO alembic_version (version_num) VALUES ('agent_design_activity_terminal');

COMMIT;
