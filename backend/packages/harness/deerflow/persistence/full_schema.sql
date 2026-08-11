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
    PRIMARY KEY (id),
    CONSTRAINT ck_jobs_authority_shape CHECK ((job_type = 'private_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NOT NULL) OR (job_type = 'automation_run' AND run_id IS NOT NULL AND owner_user_id IS NOT NULL AND automation_occurrence_id IS NOT NULL AND origin_trace_id IS NOT NULL) OR (job_type = 'retention_purge' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'mcp_discovery' AND owner_user_id IS NOT NULL AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_dream' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL) OR (job_type = 'memory_seal' AND owner_user_id IS NOT NULL AND namespace IS NOT NULL AND namespace <> '' AND run_id IS NULL AND automation_occurrence_id IS NULL AND origin_trace_id IS NULL)),
    CONSTRAINT ck_jobs_memory_namespace CHECK ((job_type IN ('memory_dream', 'memory_seal')) = (namespace IS NOT NULL)),
    CONSTRAINT ck_jobs_type CHECK (job_type IN ('private_run', 'automation_run', 'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_seal')),
    CONSTRAINT ck_jobs_retry_safety CHECK (retry_safety IN ('safe', 'unknown', 'unsafe')),
    CONSTRAINT ck_jobs_status CHECK (status IN ('queued', 'leased', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'dead')),
    CONSTRAINT ck_jobs_attempts CHECK (attempt_count >= 0 AND max_attempts >= 1),
    CONSTRAINT uq_jobs_type_idempotency UNIQUE (job_type, idempotency_key),
    CONSTRAINT uq_jobs_id_project_owner UNIQUE (id, project_id, owner_user_id),
    CONSTRAINT uq_jobs_id_project_owner_run UNIQUE (id, project_id, owner_user_id, run_id),
    CONSTRAINT uq_jobs_id_project_owner_namespace UNIQUE (id, project_id, owner_user_id, namespace),
    CONSTRAINT uq_jobs_predecessor_dead_job UNIQUE (predecessor_dead_job_id)
);

CREATE INDEX ix_jobs_active_lease ON jobs (lease_expires_at, id) WHERE status IN ('leased', 'running');

CREATE INDEX ix_jobs_claim ON jobs (status, available_at, priority DESC, created_at);

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
    CONSTRAINT ck_users_channel_guest_identity CHECK ((principal_type = 'human' AND email IS NOT NULL) OR (principal_type = 'channel_guest' AND email IS NULL AND password_hash IS NULL AND oauth_provider IS NULL AND oauth_id IS NULL AND system_role = 'user' AND needs_setup IS FALSE AND token_version = 0)),
    CONSTRAINT ck_users_preferences_version CHECK (preferences_version >= 1),
    CONSTRAINT uq_users_id_principal_type UNIQUE (id, principal_type)
);

CREATE UNIQUE INDEX idx_users_oauth_identity ON users (oauth_provider, oauth_id) WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL;

CREATE UNIQUE INDEX ix_users_email ON users (lower(email)) WHERE email IS NOT NULL;

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
    CONSTRAINT uq_job_attempts_job_number_worker UNIQUE (job_id, attempt_number, worker_id),
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
    current_published_version_id UUID,
    version BIGINT DEFAULT 1 NOT NULL,
    source_key VARCHAR(255),
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_agents_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_agents_status CHECK (status IN ('active', 'archived', 'suspended')),
    CONSTRAINT ck_agents_version CHECK (version >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(project_id) REFERENCES projects (id),
    CONSTRAINT uq_agents_id_scope UNIQUE (id, scope),
    CONSTRAINT uq_agents_project_id_id UNIQUE (project_id, id),
    CONSTRAINT uq_agents_source_key UNIQUE (source_key)
);

CREATE UNIQUE INDEX uq_agents_project_slug ON agents (project_id, lower(slug)) WHERE scope = 'project';

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
    agent_scope VARCHAR(16) NOT NULL,
    agent_asset_id UUID NOT NULL,
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
    CONSTRAINT ck_project_channel_group_bindings_agent_scope CHECK (agent_scope IN ('system', 'project')),
    CONSTRAINT ck_project_channel_group_bindings_status CHECK (status IN ('active', 'disabled')),
    CONSTRAINT ck_project_channel_group_bindings_revision CHECK (revision >= 1),
    CONSTRAINT ck_project_channel_group_bindings_activity CHECK ((first_activity_at IS NULL AND last_activity_at IS NULL) OR (first_activity_at IS NOT NULL AND last_activity_at IS NOT NULL AND first_activity_at <= last_activity_at)),
    CONSTRAINT ck_project_channel_group_bindings_deleted_status CHECK (deleted_at IS NULL OR status = 'disabled'),
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
    current_published_version_id UUID,
    version BIGINT DEFAULT 1 NOT NULL,
    source_key VARCHAR(255),
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_skills_scope_project CHECK ((scope = 'system' AND project_id IS NULL) OR (scope = 'project' AND project_id IS NOT NULL)),
    CONSTRAINT ck_skills_status CHECK (status IN ('active', 'archived', 'suspended')),
    CONSTRAINT ck_skills_version CHECK (version >= 1),
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
    workflow_status VARCHAR(24) DEFAULT 'draft' NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    soul TEXT NOT NULL,
    model_ref VARCHAR(255) NOT NULL,
    model_settings JSONB DEFAULT '{}'::jsonb NOT NULL,
    tool_groups JSONB DEFAULT '[]'::jsonb NOT NULL,
    supersedes_version_id UUID,
    payload_checksum CHAR(64) NOT NULL,
    submitted_at TIMESTAMP WITH TIME ZONE,
    reviewed_at TIMESTAMP WITH TIME ZONE,
    reviewed_by_user_id VARCHAR(36),
    review_note TEXT,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    agents_instructions TEXT DEFAULT '' NOT NULL,
    identity TEXT DEFAULT '' NOT NULL,
    user_context TEXT DEFAULT '' NOT NULL,
    payload_schema_version INTEGER DEFAULT 1 NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_agent_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_agent_versions_payload_schema_version CHECK (payload_schema_version IN (1, 2, 3)),
    CONSTRAINT ck_agent_versions_model_settings CHECK (
        jsonb_typeof(model_settings) = 'object'
        AND (
            payload_schema_version = 3
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
    CONSTRAINT ck_agent_versions_workflow_status CHECK (workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')),
    CONSTRAINT ck_agent_versions_number CHECK (version_number >= 1),
    FOREIGN KEY(agent_id) REFERENCES agents (id) ON DELETE RESTRICT,
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(reviewed_by_user_id) REFERENCES users (id),
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
    CONSTRAINT pk_agent_design_sessions PRIMARY KEY (id),
    CONSTRAINT ck_agent_design_sessions_blueprint CHECK ((blueprint_json IS NULL AND blueprint_checksum IS NULL) OR (blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL)),
    CONSTRAINT ck_agent_design_sessions_completion CHECK ((status = 'completed' AND ((created_agent_deleted IS FALSE AND created_agent_id IS NOT NULL AND created_agent_version_id IS NOT NULL) OR (created_agent_deleted IS TRUE AND created_agent_id IS NULL AND created_agent_version_id IS NULL))) OR (status <> 'completed' AND created_agent_deleted IS FALSE AND created_agent_id IS NULL AND created_agent_version_id IS NULL)),
    CONSTRAINT ck_agent_design_sessions_ready_blueprint CHECK ((status IN ('proposal_ready', 'committing', 'completed') AND blueprint_json IS NOT NULL AND blueprint_checksum IS NOT NULL) OR status NOT IN ('proposal_ready', 'committing', 'completed')),
    CONSTRAINT ck_agent_design_sessions_clarification CHECK ((status = 'awaiting_clarification' AND active_clarification_json IS NOT NULL) OR (status <> 'awaiting_clarification' AND active_clarification_json IS NULL)),
    CONSTRAINT ck_agent_design_sessions_error CHECK ((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)),
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

CREATE INDEX ix_agent_design_sessions_resume ON agent_design_sessions (project_id, owner_user_id, status, updated_at DESC, id DESC);

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
    CONSTRAINT pk_agent_design_operations PRIMARY KEY (id),
    CONSTRAINT ck_agent_design_operations_kind CHECK (operation_kind IN ('turn', 'commit', 'cancel')),
    CONSTRAINT ck_agent_design_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
    CONSTRAINT ck_agent_design_operations_status CHECK (status IN ('in_progress', 'completed', 'failed')),
    CONSTRAINT ck_agent_design_operations_completion CHECK ((status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL)),
    CONSTRAINT fk_agent_design_operations_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES agent_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_agent_design_operations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_agent_design_operations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT uq_agent_design_operations_idempotency UNIQUE (project_id, owner_user_id, operation_kind, idempotency_key_hash)
);

CREATE INDEX ix_agent_design_operations_session ON agent_design_operations (project_id, owner_user_id, session_id, created_at DESC);

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
    workflow_status VARCHAR(24) DEFAULT 'draft' NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    frontmatter JSONB DEFAULT '{}'::jsonb NOT NULL,
    compatibility VARCHAR(255),
    secret_requirements JSONB DEFAULT '[]'::jsonb NOT NULL,
    scan_decision VARCHAR(24) NOT NULL,
    scan_summary JSONB DEFAULT '{}'::jsonb NOT NULL,
    supersedes_version_id UUID,
    payload_checksum CHAR(64) NOT NULL,
    submitted_at TIMESTAMP WITH TIME ZONE,
    reviewed_at TIMESTAMP WITH TIME ZONE,
    reviewed_by_user_id VARCHAR(36),
    review_note TEXT,
    created_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_skill_versions_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_skill_versions_scan_decision CHECK (scan_decision IN ('allow', 'warn', 'block')),
    CONSTRAINT ck_skill_versions_workflow_status CHECK (workflow_status IN ('draft', 'pending_approval', 'published', 'rejected')),
    CONSTRAINT ck_skill_versions_number CHECK (version_number >= 1),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id),
    FOREIGN KEY(reviewed_by_user_id) REFERENCES users (id),
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
    PRIMARY KEY (thread_id),
    CONSTRAINT ck_threads_meta_agent_scope CHECK (agent_scope IN ('system', 'project')),
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
    skill_version_id UUID NOT NULL,
    sort_order BIGINT DEFAULT 0 NOT NULL,
    PRIMARY KEY (agent_version_id, skill_version_id),
    CONSTRAINT ck_agent_version_skill_refs_sort_order CHECK (sort_order >= 0),
    FOREIGN KEY(agent_version_id) REFERENCES agent_versions (id) ON DELETE RESTRICT,
    FOREIGN KEY(skill_version_id) REFERENCES skill_versions (id) ON DELETE RESTRICT
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
    agent_version_id UUID NOT NULL,
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
    CONSTRAINT fk_project_system_agent_bindings_version FOREIGN KEY(system_agent_id, agent_version_id) REFERENCES agent_versions (agent_id, id) ON DELETE RESTRICT,
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
    skill_version_id UUID NOT NULL,
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
    CONSTRAINT fk_project_system_skill_bindings_version FOREIGN KEY(system_skill_id, skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
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

ALTER TABLE agents ADD CONSTRAINT fk_agents_current_published_version FOREIGN KEY(id, current_published_version_id) REFERENCES agent_versions (agent_id, id);

ALTER TABLE credentials ADD CONSTRAINT fk_credentials_current_version FOREIGN KEY(id, current_version_id) REFERENCES credential_versions (credential_id, id);

ALTER TABLE mcp_servers ADD CONSTRAINT fk_mcp_servers_current_published_version FOREIGN KEY(id, current_published_version_id) REFERENCES mcp_server_versions (mcp_server_id, id);

ALTER TABLE skills ADD CONSTRAINT fk_skills_current_published_version FOREIGN KEY(id, current_published_version_id) REFERENCES skill_versions (skill_id, id);

CREATE OR REPLACE FUNCTION prevent_shared_asset_version_payload_update()
RETURNS trigger AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id'
    ]::text[]) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY[
        'workflow_status', 'status', 'submitted_at', 'reviewed_at',
        'reviewed_by_user_id', 'review_note', 'retired_at', 'revoked_at',
        'revoked_by_user_id'
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

CREATE OR REPLACE FUNCTION ensure_system_binding_published_version()
RETURNS trigger AS $$
DECLARE
    version_status text;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'project_system_agent_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM agent_versions
            WHERE id = NEW.agent_version_id AND agent_id = NEW.system_agent_id
            FOR UPDATE;
        WHEN 'project_system_skill_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM skill_versions
            WHERE id = NEW.skill_version_id AND skill_id = NEW.system_skill_id
            FOR UPDATE;
        WHEN 'project_system_mcp_bindings' THEN
            SELECT workflow_status INTO version_status
            FROM mcp_server_versions
            WHERE id = NEW.mcp_server_version_id
              AND mcp_server_id = NEW.system_mcp_server_id
            FOR UPDATE;
        ELSE
            RAISE EXCEPTION 'unsupported system binding table';
    END CASE;
    IF version_status IS DISTINCT FROM 'published' THEN
        RAISE EXCEPTION 'system binding requires published version'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION prevent_bound_published_version_downgrade()
RETURNS trigger AS $$
DECLARE
    is_bound boolean;
BEGIN
    IF OLD.workflow_status = 'published'
       AND NEW.workflow_status IS DISTINCT FROM 'published' THEN
        CASE TG_TABLE_NAME
            WHEN 'agent_versions' THEN
                SELECT EXISTS (
                    SELECT 1 FROM project_system_agent_bindings
                    WHERE agent_version_id = OLD.id
                ) INTO is_bound;
            WHEN 'skill_versions' THEN
                SELECT EXISTS (
                    SELECT 1 FROM project_system_skill_bindings
                    WHERE skill_version_id = OLD.id
                ) INTO is_bound;
            WHEN 'mcp_server_versions' THEN
                SELECT EXISTS (
                    SELECT 1 FROM project_system_mcp_bindings
                    WHERE mcp_server_version_id = OLD.id
                ) INTO is_bound;
            ELSE
                is_bound := false;
        END CASE;
        IF is_bound THEN
            RAISE EXCEPTION 'bound published version cannot change workflow status'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION prevent_published_version_child_mutation()
RETURNS trigger AS $$
DECLARE
    parent_version_id uuid;
    parent_status text;
    purge_allowed boolean := false;
BEGIN
    CASE TG_TABLE_NAME
        WHEN 'skill_version_files' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.skill_version_id ELSE NEW.skill_version_id END;
            SELECT workflow_status INTO parent_status
            FROM skill_versions WHERE id = parent_version_id FOR UPDATE;
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
                              AND asset.current_published_version_id IS NULL
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
            SELECT workflow_status INTO parent_status
            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;
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
                              AND asset.current_published_version_id IS NULL
                              AND current_setting(
                                  'deerflow.agent_hard_delete_asset_id',
                                  true
                              ) = asset.id::text
                          )
                      )
                ) OR EXISTS (
                    SELECT 1
                    FROM skill_versions version
                    JOIN skills asset ON asset.id = version.skill_id
                    JOIN projects project ON project.id = asset.project_id
                    WHERE version.id = OLD.skill_version_id
                      AND asset.scope = 'project'
                      AND project.status = 'pending_deletion'
                      AND project.deletion_effective_at IS NOT NULL
                      AND project.deletion_effective_at <= now()
                ) INTO purge_allowed;
            END IF;
        WHEN 'agent_version_mcp_refs' THEN
            parent_version_id := CASE WHEN TG_OP = 'DELETE'
                THEN OLD.agent_version_id ELSE NEW.agent_version_id END;
            SELECT workflow_status INTO parent_status
            FROM agent_versions WHERE id = parent_version_id FOR UPDATE;
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
                              AND asset.current_published_version_id IS NULL
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

CREATE TRIGGER trg_mcp_server_versions_immutable BEFORE UPDATE ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_credential_versions_immutable BEFORE UPDATE ON credential_versions FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_skill_version_files_immutable BEFORE UPDATE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_version_skill_refs_immutable BEFORE UPDATE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_version_mcp_refs_immutable BEFORE UPDATE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_mcp_credential_slots_immutable BEFORE UPDATE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_shared_asset_version_payload_update();

CREATE TRIGGER trg_agent_bindings_published BEFORE INSERT OR UPDATE ON project_system_agent_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_published_version();

CREATE TRIGGER trg_skill_bindings_published BEFORE INSERT OR UPDATE ON project_system_skill_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_published_version();

CREATE TRIGGER trg_mcp_bindings_published BEFORE INSERT OR UPDATE ON project_system_mcp_bindings FOR EACH ROW EXECUTE FUNCTION ensure_system_binding_published_version();

CREATE TRIGGER trg_agent_versions_bound_published BEFORE UPDATE OF workflow_status ON agent_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_published_version_downgrade();

CREATE TRIGGER trg_skill_versions_bound_published BEFORE UPDATE OF workflow_status ON skill_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_published_version_downgrade();

CREATE TRIGGER trg_mcp_server_versions_bound_published BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION prevent_bound_published_version_downgrade();

CREATE TRIGGER trg_skill_version_files_child_immutable BEFORE INSERT OR DELETE ON skill_version_files FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation();

CREATE TRIGGER trg_agent_version_skill_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_skill_refs FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation();

CREATE TRIGGER trg_agent_version_mcp_refs_child_immutable BEFORE INSERT OR DELETE ON agent_version_mcp_refs FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation();

CREATE TRIGGER trg_mcp_credential_slots_child_immutable BEFORE INSERT OR DELETE ON mcp_version_credential_slots FOR EACH ROW EXECUTE FUNCTION prevent_published_version_child_mutation();

CREATE TRIGGER trg_agent_versions_state_transition BEFORE UPDATE OF workflow_status ON agent_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_skill_versions_state_transition BEFORE UPDATE OF workflow_status ON skill_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_mcp_server_versions_state_transition BEFORE UPDATE OF workflow_status ON mcp_server_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_credential_versions_state_transition BEFORE UPDATE OF status ON credential_versions FOR EACH ROW EXECUTE FUNCTION enforce_shared_asset_version_state_transition();

CREATE TRIGGER trg_agents_generation AFTER UPDATE OF status, current_published_version_id ON agents FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skills_generation AFTER UPDATE OF status, current_published_version_id ON skills FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_mcp_servers_generation AFTER UPDATE OF status, current_published_version_id ON mcp_servers FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_agent_versions_generation AFTER UPDATE OF workflow_status ON agent_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

CREATE TRIGGER trg_skill_versions_generation AFTER UPDATE OF workflow_status ON skill_versions FOR EACH STATEMENT EXECUTE FUNCTION bump_asset_catalog_generation();

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

SELECT ensure_run_events_month_partition(now());

SELECT ensure_run_events_month_partition(now() + INTERVAL '1 month');

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
    CONSTRAINT ck_skill_design_sessions_error CHECK ((status = 'failed' AND error_code IS NOT NULL AND error_message IS NOT NULL) OR (status <> 'failed' AND error_code IS NULL AND error_message IS NULL)),
    CONSTRAINT ck_skill_design_sessions_completion CHECK ((status = 'completed' AND ((created_skill_deleted IS FALSE AND created_skill_id IS NOT NULL AND created_skill_version_id IS NOT NULL) OR (created_skill_deleted IS TRUE AND created_skill_id IS NULL AND created_skill_version_id IS NULL))) OR (status <> 'completed' AND created_skill_deleted IS FALSE AND created_skill_id IS NULL AND created_skill_version_id IS NULL)),
    CONSTRAINT fk_skill_design_sessions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_skill_creator_version FOREIGN KEY(skill_creator_skill_id, skill_creator_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_created_skill_project FOREIGN KEY(project_id, created_skill_id) REFERENCES skills (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_sessions_created_skill_version FOREIGN KEY(created_skill_id, created_skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE RESTRICT,
    CONSTRAINT uq_skill_design_sessions_private_scope UNIQUE (project_id, owner_user_id, id),
    CONSTRAINT uq_skill_design_sessions_thread_scope UNIQUE (project_id, owner_user_id, thread_id),
    CONSTRAINT uq_skill_design_sessions_create_idempotency UNIQUE (project_id, owner_user_id, create_idempotency_key_hash)
);

CREATE INDEX ix_skill_design_sessions_resume ON skill_design_sessions (project_id, owner_user_id, status, updated_at DESC, id DESC);

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
    CONSTRAINT pk_skill_design_operations PRIMARY KEY (id),
    CONSTRAINT ck_skill_design_operations_kind CHECK (operation_kind IN ('turn', 'validate', 'commit', 'cancel')),
    CONSTRAINT ck_skill_design_operations_status CHECK (status IN ('in_progress', 'completed', 'failed')),
    CONSTRAINT ck_skill_design_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
    CONSTRAINT ck_skill_design_operations_completion CHECK ((status = 'in_progress' AND result_revision IS NULL AND public_error_code IS NULL) OR (status = 'completed' AND result_revision IS NOT NULL AND public_error_code IS NULL) OR (status = 'failed' AND result_revision IS NOT NULL AND public_error_code IS NOT NULL)),
    CONSTRAINT fk_skill_design_operations_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_operations_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_skill_design_operations_session FOREIGN KEY(project_id, owner_user_id, session_id) REFERENCES skill_design_sessions (project_id, owner_user_id, id) ON DELETE CASCADE,
    CONSTRAINT uq_skill_design_operations_idempotency UNIQUE (project_id, owner_user_id, operation_kind, idempotency_key_hash)
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
    CONSTRAINT pk_project_skill_credential_bindings PRIMARY KEY (id),
    CONSTRAINT ck_project_skill_credential_bindings_secret_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
    CONSTRAINT ck_project_skill_credential_bindings_revision CHECK (config_revision >= 1),
    CONSTRAINT ck_project_skill_credential_bindings_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_project_skill_credential_bindings_revocation CHECK ((status = 'active' AND revoked_at IS NULL AND revoked_by_user_id IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by_user_id IS NOT NULL)),
    CONSTRAINT fk_project_skill_credential_bindings_config FOREIGN KEY(project_id, skill_id, skill_version_id) REFERENCES project_skill_credential_configs (project_id, skill_id, skill_version_id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_credential_bindings_skill_version FOREIGN KEY(skill_id, skill_version_id) REFERENCES skill_versions (skill_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_project_skill_credential_bindings_credential_version FOREIGN KEY(credential_id, credential_version_id) REFERENCES credential_versions (credential_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_project_credential FOREIGN KEY(project_id, credential_id) REFERENCES credentials (project_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_creator FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT fk_project_skill_credential_bindings_revoker FOREIGN KEY(revoked_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    CONSTRAINT uq_project_skill_credential_bindings_scope_id UNIQUE (project_id, skill_id, skill_version_id, id)
);

CREATE UNIQUE INDEX uq_project_skill_credential_bindings_active_name ON project_skill_credential_bindings (project_id, skill_id, skill_version_id, secret_name) WHERE status = 'active';

CREATE INDEX ix_project_skill_credential_bindings_credential ON project_skill_credential_bindings (credential_id, credential_version_id, status);

CREATE INDEX ix_project_skill_credential_bindings_config ON project_skill_credential_bindings (project_id, skill_id, skill_version_id);

CREATE INDEX ix_project_skill_credential_bindings_skill_version ON project_skill_credential_bindings (skill_id, skill_version_id);

CREATE INDEX ix_project_skill_credential_bindings_project_credential ON project_skill_credential_bindings (project_id, credential_id);

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
    CONSTRAINT pk_run_skill_credential_snapshots PRIMARY KEY (project_id, owner_user_id, run_id, skill_version_id, secret_name),
    CONSTRAINT ck_run_skill_credential_snapshots_secret_name CHECK (secret_name ~ '^[A-Za-z_][A-Za-z0-9_]*$'),
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
    logical_name VARCHAR(128) NOT NULL,
    display_name VARCHAR(120) NOT NULL,
    description TEXT DEFAULT '' NOT NULL,
    status VARCHAR(16) DEFAULT 'suspended' NOT NULL,
    current_version_id UUID,
    revision BIGINT DEFAULT 1 NOT NULL,
    sort_order BIGINT DEFAULT 0 NOT NULL,
    created_by_user_id VARCHAR(36) NOT NULL,
    updated_by_user_id VARCHAR(36) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
    PRIMARY KEY (id),
    CONSTRAINT ck_system_model_configs_status CHECK (status IN ('active', 'suspended')),
    CONSTRAINT ck_system_model_configs_revision CHECK (revision >= 1),
    CONSTRAINT ck_system_model_configs_sort_order CHECK (sort_order >= 0),
    CONSTRAINT uq_system_model_configs_id_current_version UNIQUE (id, current_version_id),
    FOREIGN KEY(created_by_user_id) REFERENCES users (id) ON DELETE RESTRICT,
    FOREIGN KEY(updated_by_user_id) REFERENCES users (id) ON DELETE RESTRICT
);

CREATE INDEX ix_system_model_configs_status_order ON system_model_configs (status, sort_order, id);

CREATE UNIQUE INDEX uq_system_model_configs_logical_name ON system_model_configs (lower(logical_name));

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
    logical_name VARCHAR(128) NOT NULL,
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
    CONSTRAINT ck_system_runtime_policies_section CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas')),
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
    CONSTRAINT ck_system_runtime_policy_versions_section CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas')),
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

-- BEGIN FULL_SCHEMA_V10_WORKFLOWS
CREATE OR REPLACE FUNCTION workflow_profile_digest_array_is_valid(value JSONB)
RETURNS BOOLEAN AS $$
    SELECT jsonb_typeof(value) = 'array'
       AND jsonb_array_length(value) <= 128
       AND NOT jsonb_path_exists(value, '$[*] ? (@.type() != "string")')
       AND NOT EXISTS (
           SELECT 1
             FROM jsonb_array_elements_text(value) AS item(digest)
            WHERE digest !~ '^[0-9a-f]{64}$'
       )
       AND (
           SELECT count(*) = count(DISTINCT digest)
             FROM jsonb_array_elements_text(value) AS item(digest)
       );
$$ LANGUAGE SQL IMMUTABLE STRICT;

CREATE OR REPLACE FUNCTION workflow_http_settled_outcome_is_valid(value JSONB)
RETURNS BOOLEAN AS $$
DECLARE
    outcome_kind TEXT;
    response_value JSONB;
    body_value JSONB;
    headers_value JSONB;
    wire_count JSONB;
    decoded_count JSONB;
    error_value JSONB;
    retained_count NUMERIC;
    retained_body_bytes BIGINT;
BEGIN
    IF jsonb_typeof(value) <> 'object' THEN
        RETURN false;
    END IF;
    outcome_kind := value->>'kind';

    IF outcome_kind IN ('success', 'http_error') THEN
        IF NOT value ?& ARRAY['kind', 'response']
           OR value - ARRAY['kind', 'response'] <> '{}'::jsonb
           OR jsonb_typeof(value->'response') <> 'object' THEN
            RETURN false;
        END IF;
        response_value := value->'response';
        IF NOT response_value ?& ARRAY[
            'status_code', 'headers', 'body', 'duration_ms',
            'wire_byte_count', 'decoded_byte_count',
            'retained_body_byte_count'
        ] OR response_value - ARRAY[
            'status_code', 'headers', 'body', 'duration_ms',
            'wire_byte_count', 'decoded_byte_count',
            'retained_body_byte_count'
        ] <> '{}'::jsonb THEN
            RETURN false;
        END IF;
        IF jsonb_typeof(response_value->'status_code') <> 'number'
           OR response_value->>'status_code' !~ '^(0|[1-9][0-9]*)$'
           OR (response_value->>'status_code')::numeric NOT BETWEEN 100 AND 599
           OR jsonb_typeof(response_value->'duration_ms') <> 'number'
           OR response_value->>'duration_ms' !~ '^(0|[1-9][0-9]*)$'
           OR (response_value->>'duration_ms')::numeric > 9007199254740991
           OR jsonb_typeof(response_value->'retained_body_byte_count') <> 'number'
           OR response_value->>'retained_body_byte_count' !~ '^(0|[1-9][0-9]*)$'
           OR (response_value->>'retained_body_byte_count')::numeric > 2097152 THEN
            RETURN false;
        END IF;

        headers_value := response_value->'headers';
        IF jsonb_typeof(headers_value) <> 'array'
           OR jsonb_array_length(headers_value) > 64
           OR EXISTS (
                SELECT 1
                  FROM jsonb_array_elements(headers_value) AS header(item)
                 WHERE jsonb_typeof(item) <> 'object'
                    OR NOT item ?& ARRAY['name', 'value']
                    OR item - ARRAY['name', 'value'] <> '{}'::jsonb
                    OR jsonb_typeof(item->'name') <> 'string'
                    OR item->>'name' !~ '^[a-z0-9!#$%&''*+.^_`|~-]{1,128}$'
                    OR item->>'name' IN (
                        'authorization', 'proxy-authenticate',
                        'proxy-authorization', 'set-cookie',
                        'www-authenticate', 'location'
                    )
                    OR jsonb_typeof(item->'value') <> 'string'
                    OR octet_length(item->>'value') > 4096
           ) OR EXISTS (
                SELECT 1
                  FROM jsonb_array_elements(headers_value) AS header(item)
                 GROUP BY item->>'name'
                HAVING count(*) > 1
           ) OR COALESCE((
                SELECT sum(
                    octet_length(item->>'name')
                    + octet_length(item->>'value')
                )
                  FROM jsonb_array_elements(headers_value) AS header(item)
           ), 0) > 65536 THEN
            RETURN false;
        END IF;

        body_value := response_value->'body';
        IF jsonb_typeof(body_value) <> 'object' THEN
            RETURN false;
        END IF;
        IF body_value->>'kind' = 'empty' THEN
            IF NOT body_value ? 'kind'
               OR body_value - 'kind' <> '{}'::jsonb THEN
                RETURN false;
            END IF;
            retained_body_bytes := 0;
        ELSIF body_value->>'kind' = 'text' THEN
            IF NOT body_value ?& ARRAY['kind', 'text']
               OR body_value - ARRAY['kind', 'text'] <> '{}'::jsonb
               OR jsonb_typeof(body_value->'text') <> 'string'
               OR octet_length(body_value->>'text') > 2097152 THEN
                RETURN false;
            END IF;
            retained_body_bytes := octet_length(body_value->>'text');
        ELSIF body_value->>'kind' = 'json' THEN
            IF NOT body_value ?& ARRAY['kind', 'value']
               OR body_value - ARRAY['kind', 'value'] <> '{}'::jsonb
               OR octet_length((body_value->'value')::text) > 2162688 THEN
                RETURN false;
            END IF;
            retained_body_bytes := NULL;
        ELSE
            RETURN false;
        END IF;

        wire_count := response_value->'wire_byte_count';
        decoded_count := response_value->'decoded_byte_count';
        IF jsonb_typeof(wire_count) <> 'object'
           OR NOT wire_count ?& ARRAY['value', 'relation']
           OR wire_count - ARRAY['value', 'relation'] <> '{}'::jsonb
           OR jsonb_typeof(wire_count->'value') <> 'number'
           OR wire_count->>'value' !~ '^(0|[1-9][0-9]*)$'
           OR (wire_count->>'value')::numeric > 2097152
           OR wire_count->>'relation' <> 'exact'
           OR jsonb_typeof(decoded_count) <> 'object'
           OR NOT decoded_count ?& ARRAY['value', 'relation']
           OR decoded_count - ARRAY['value', 'relation'] <> '{}'::jsonb
           OR jsonb_typeof(decoded_count->'value') <> 'number'
           OR decoded_count->>'value' !~ '^(0|[1-9][0-9]*)$'
           OR (decoded_count->>'value')::numeric > 2097152
           OR decoded_count->>'relation' <> 'exact' THEN
            RETURN false;
        END IF;
        retained_count := (response_value->>'retained_body_byte_count')::numeric;
        IF retained_body_bytes IS NOT NULL
           AND retained_count <> retained_body_bytes THEN
            RETURN false;
        END IF;
        RETURN true;
    END IF;

    IF outcome_kind = 'response_invalid' THEN
        IF NOT value ?& ARRAY[
            'kind', 'status_code', 'duration_ms', 'wire_byte_count',
            'decoded_byte_count', 'error'
        ] OR value - ARRAY[
            'kind', 'status_code', 'duration_ms', 'wire_byte_count',
            'decoded_byte_count', 'error'
        ] <> '{}'::jsonb
           OR jsonb_typeof(value->'status_code') <> 'number'
           OR value->>'status_code' !~ '^(0|[1-9][0-9]*)$'
           OR (value->>'status_code')::numeric NOT BETWEEN 100 AND 599
           OR jsonb_typeof(value->'duration_ms') <> 'number'
           OR value->>'duration_ms' !~ '^(0|[1-9][0-9]*)$'
           OR (value->>'duration_ms')::numeric > 9007199254740991 THEN
            RETURN false;
        END IF;
        wire_count := value->'wire_byte_count';
        decoded_count := value->'decoded_byte_count';
        IF jsonb_typeof(wire_count) <> 'object'
           OR NOT wire_count ?& ARRAY['value', 'relation']
           OR wire_count - ARRAY['value', 'relation'] <> '{}'::jsonb
           OR jsonb_typeof(wire_count->'value') <> 'number'
           OR wire_count->>'value' !~ '^(0|[1-9][0-9]*)$'
           OR (wire_count->>'value')::numeric > 2097152
           OR wire_count->>'relation' NOT IN ('exact', 'at_least')
           OR jsonb_typeof(decoded_count) <> 'object'
           OR NOT decoded_count ?& ARRAY['value', 'relation']
           OR decoded_count - ARRAY['value', 'relation'] <> '{}'::jsonb
           OR jsonb_typeof(decoded_count->'value') <> 'number'
           OR decoded_count->>'value' !~ '^(0|[1-9][0-9]*)$'
           OR (decoded_count->>'value')::numeric > 2097152
           OR decoded_count->>'relation' NOT IN ('exact', 'at_least') THEN
            RETURN false;
        END IF;

        error_value := value->'error';
        IF jsonb_typeof(error_value) <> 'object'
           OR NOT error_value ?& ARRAY['code', 'safe_message']
           OR error_value - ARRAY['code', 'safe_message', 'line', 'column']
                <> '{}'::jsonb
           OR error_value->>'code' NOT IN (
                'WORKFLOW_HTTP_RESPONSE_LIMIT',
                'WORKFLOW_HTTP_RESPONSE_INVALID'
           )
           OR jsonb_typeof(error_value->'safe_message') <> 'string'
           OR octet_length(error_value->>'safe_message') NOT BETWEEN 1 AND 2048
           OR (
                error_value ? 'line'
                AND error_value->'line' <> 'null'::jsonb
                AND (
                    jsonb_typeof(error_value->'line') <> 'number'
                    OR error_value->>'line' !~ '^[1-9][0-9]*$'
                    OR (error_value->>'line')::numeric > 9007199254740991
                )
           ) OR (
                error_value ? 'column'
                AND error_value->'column' <> 'null'::jsonb
                AND (
                    jsonb_typeof(error_value->'column') <> 'number'
                    OR error_value->>'column' !~ '^[1-9][0-9]*$'
                    OR (error_value->>'column')::numeric > 9007199254740991
                )
           ) THEN
            RETURN false;
        END IF;
        IF error_value->>'code' = 'WORKFLOW_HTTP_RESPONSE_LIMIT'
           AND wire_count->>'relation' <> 'at_least'
           AND decoded_count->>'relation' <> 'at_least' THEN
            RETURN false;
        END IF;
        RETURN true;
    END IF;
    RETURN false;
EXCEPTION
    WHEN OTHERS THEN
        RETURN false;
END;
$$ LANGUAGE plpgsql IMMUTABLE STRICT;

ALTER TABLE worker_nodes
    ADD COLUMN runtime_profile_digests_json JSONB DEFAULT '[]'::jsonb NOT NULL,
    ADD COLUMN workflow_runtime_policy_section VARCHAR(32),
    ADD COLUMN workflow_runtime_policy_version_id UUID,
    ADD COLUMN workflow_runtime_policy_revision BIGINT,
    ADD COLUMN workflow_runtime_policy_schema_version SMALLINT,
    ADD COLUMN workflow_runtime_policy_checksum CHAR(64),
    ADD CONSTRAINT ck_worker_nodes_runtime_profiles_array
        CHECK (workflow_profile_digest_array_is_valid(runtime_profile_digests_json)),
    ADD CONSTRAINT ck_worker_nodes_workflow_runtime_identity
        CHECK (
            (
                workflow_runtime_policy_section IS NULL
                AND workflow_runtime_policy_version_id IS NULL
                AND workflow_runtime_policy_revision IS NULL
                AND workflow_runtime_policy_schema_version IS NULL
                AND workflow_runtime_policy_checksum IS NULL
            ) OR (
                workflow_runtime_policy_section IS NOT NULL
                AND workflow_runtime_policy_section = 'workflow_runtime'
                AND workflow_runtime_policy_version_id IS NOT NULL
                AND workflow_runtime_policy_revision IS NOT NULL
                AND workflow_runtime_policy_revision >= 1
                AND workflow_runtime_policy_schema_version IS NOT NULL
                AND workflow_runtime_policy_schema_version >= 1
                AND workflow_runtime_policy_checksum IS NOT NULL
                AND workflow_runtime_policy_checksum ~ '^[0-9a-f]{64}$'
            )
        );

ALTER TABLE system_runtime_policies
    DROP CONSTRAINT ck_system_runtime_policies_section,
    ADD CONSTRAINT ck_system_runtime_policies_section
        CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas', 'workflow_runtime'));

ALTER TABLE system_runtime_policy_versions
    DROP CONSTRAINT ck_system_runtime_policy_versions_section,
    ADD CONSTRAINT ck_system_runtime_policy_versions_section
        CHECK (section IN ('agent_runtime', 'auth', 'memory_document', 'quotas', 'workflow_runtime')),
    ADD CONSTRAINT uq_system_runtime_policy_versions_revision_exact
        UNIQUE (section, id, version_number, schema_version, payload_checksum);

ALTER TABLE worker_nodes
    ADD CONSTRAINT fk_worker_nodes_workflow_runtime_identity
        FOREIGN KEY (
            workflow_runtime_policy_section,
            workflow_runtime_policy_version_id,
            workflow_runtime_policy_revision,
            workflow_runtime_policy_schema_version,
            workflow_runtime_policy_checksum
        ) REFERENCES system_runtime_policy_versions (
            section,
            id,
            version_number,
            schema_version,
            payload_checksum
        ) MATCH FULL ON DELETE RESTRICT;

CREATE INDEX ix_worker_nodes_workflow_runtime_identity_fresh
    ON worker_nodes (
        workflow_runtime_policy_section,
        workflow_runtime_policy_version_id,
        workflow_runtime_policy_revision,
        workflow_runtime_policy_schema_version,
        workflow_runtime_policy_checksum,
        draining,
        heartbeat_at
    );

ALTER TABLE jobs
    ADD COLUMN workflow_run_id UUID,
    ADD COLUMN workflow_epoch BIGINT,
    ADD COLUMN required_worker_profile_digest CHAR(64),
    ADD COLUMN workflow_profile_key CHAR(64);

CREATE TABLE workflow_definitions (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	name VARCHAR(255) NOT NULL,
	description TEXT DEFAULT '' NOT NULL,
	status VARCHAR(16) DEFAULT 'active' NOT NULL,
	current_published_version_id UUID,
	revision BIGINT DEFAULT 1 NOT NULL,
	created_by VARCHAR(36) NOT NULL,
	updated_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_workflow_definitions_status CHECK (status IN ('active', 'archived')),
	CONSTRAINT ck_workflow_definitions_revision CHECK (revision >= 1),
	CONSTRAINT ck_workflow_definitions_name CHECK (name = btrim(name) AND char_length(name) BETWEEN 1 AND 255),
	CONSTRAINT uq_workflow_definitions_id_project UNIQUE (id, project_id),
	CONSTRAINT fk_workflow_definitions_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_definitions_created_by FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_definitions_updated_by FOREIGN KEY(updated_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_workflow_definitions_list ON workflow_definitions (project_id, status, updated_at DESC, id DESC);
CREATE UNIQUE INDEX uq_workflow_definitions_project_name ON workflow_definitions (project_id, lower(name));
CREATE TABLE workflow_drafts (
	workflow_id UUID NOT NULL,
	project_id UUID NOT NULL,
	revision BIGINT DEFAULT 1 NOT NULL,
	spec_schema_version SMALLINT NOT NULL,
	canvas_schema_version SMALLINT NOT NULL,
	spec_json JSONB NOT NULL,
	canvas_json JSONB NOT NULL,
	draft_checksum CHAR(64) NOT NULL,
	updated_by VARCHAR(36) NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_id),
	CONSTRAINT ck_workflow_drafts_revision CHECK (revision >= 1),
	CONSTRAINT ck_workflow_drafts_schema CHECK (spec_schema_version >= 1 AND canvas_schema_version >= 1),
	CONSTRAINT ck_workflow_drafts_spec_object CHECK (jsonb_typeof(spec_json) = 'object'),
	CONSTRAINT ck_workflow_drafts_canvas_object CHECK (jsonb_typeof(canvas_json) = 'object'),
	CONSTRAINT ck_workflow_drafts_checksum CHECK (draft_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT fk_workflow_drafts_definition FOREIGN KEY(workflow_id, project_id) REFERENCES workflow_definitions (id, project_id) ON DELETE CASCADE,
	CONSTRAINT fk_workflow_drafts_updated_by FOREIGN KEY(updated_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE TABLE workflow_versions (
	id UUID NOT NULL,
	workflow_id UUID NOT NULL,
	project_id UUID NOT NULL,
	version_number BIGINT NOT NULL,
	graph_schema_version SMALLINT DEFAULT 1 NOT NULL,
	canvas_schema_version SMALLINT DEFAULT 1 NOT NULL,
	compiler_contract_version SMALLINT NOT NULL,
	spec_json JSONB NOT NULL,
	canvas_json JSONB NOT NULL,
	semantic_checksum CHAR(64) NOT NULL,
	published_by VARCHAR(36) NOT NULL,
	published_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_workflow_versions_number CHECK (version_number >= 1),
	CONSTRAINT ck_workflow_versions_schema CHECK (graph_schema_version >= 1 AND canvas_schema_version >= 1 AND compiler_contract_version >= 1),
	CONSTRAINT ck_workflow_versions_spec_object CHECK (jsonb_typeof(spec_json) = 'object'),
	CONSTRAINT ck_workflow_versions_canvas_object CHECK (jsonb_typeof(canvas_json) = 'object'),
	CONSTRAINT ck_workflow_versions_checksum CHECK (semantic_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT uq_workflow_versions_number UNIQUE (workflow_id, version_number),
	CONSTRAINT uq_workflow_versions_workflow_id UNIQUE (workflow_id, id),
	CONSTRAINT uq_workflow_versions_id_project UNIQUE (id, project_id),
	CONSTRAINT uq_workflow_versions_scope UNIQUE (id, workflow_id, project_id),
	CONSTRAINT uq_workflow_versions_snapshot_exact UNIQUE (id, project_id, graph_schema_version, compiler_contract_version, semantic_checksum),
	CONSTRAINT uq_workflow_versions_semantic_contract UNIQUE (workflow_id, semantic_checksum, compiler_contract_version),
	CONSTRAINT fk_workflow_versions_definition FOREIGN KEY(workflow_id, project_id) REFERENCES workflow_definitions (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_versions_published_by FOREIGN KEY(published_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE TABLE workflow_control_operations (
	project_id UUID NOT NULL,
	workflow_id UUID NOT NULL,
	operation VARCHAR(32) NOT NULL,
	idempotency_hash CHAR(64) NOT NULL,
	request_digest CHAR(64) NOT NULL,
	result_version_id UUID,
	created_by VARCHAR(36) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	scope_key VARCHAR(512) NOT NULL,
	result_revision BIGINT,
	result_checksum CHAR(64),
	result_slot_id VARCHAR(128),
	result_credential_id UUID,
	result_credential_version_id UUID,
	result_status VARCHAR(16),
	result_deleted BOOLEAN,
	result_created_at TIMESTAMP WITH TIME ZONE,
	result_updated_at TIMESTAMP WITH TIME ZONE,
	result_revoked_at TIMESTAMP WITH TIME ZONE,
	result_name VARCHAR(255),
	result_description VARCHAR(4096),
	result_lifecycle VARCHAR(16),
	result_published_version_id UUID,
	result_published_version_number BIGINT,
	result_draft_revision BIGINT,
	result_draft_checksum CHAR(64),
	result_missing_slot_ids_csv VARCHAR(33000),
	CONSTRAINT pk_workflow_control_operations PRIMARY KEY (project_id, operation, scope_key, idempotency_hash),
	CONSTRAINT ck_workflow_control_operations_operation CHECK (operation IN ('create','update','save_draft','archive','publish','draft_grant_put','draft_grant_delete','version_grant_put','version_grant_delete')),
	CONSTRAINT ck_workflow_control_operations_scope_key CHECK (char_length(scope_key) BETWEEN 1 AND 512 AND scope_key ~ '^[a-z][A-Za-z0-9:._-]*$'),
	CONSTRAINT ck_workflow_control_operations_scope_shape CHECK ((operation = 'create' AND scope_key = 'project:' || project_id::text) OR (operation IN ('update','save_draft','archive','publish') AND scope_key = 'definition:' || workflow_id::text) OR (operation IN ('draft_grant_put','draft_grant_delete') AND scope_key = 'draft-slot:' || workflow_id::text || ':' || result_slot_id) OR (operation IN ('version_grant_put','version_grant_delete') AND scope_key = 'version-slot:' || workflow_id::text || ':' || result_version_id::text || ':' || result_slot_id)),
	CONSTRAINT ck_workflow_control_operations_idempotency_hash CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_control_operations_request_digest CHECK (request_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_control_operations_result_revision CHECK (result_revision IS NULL OR result_revision >= 1),
	CONSTRAINT ck_workflow_control_operations_result_checksum CHECK (result_checksum IS NULL OR result_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_control_operations_result_draft_checksum CHECK (result_draft_checksum IS NULL OR result_draft_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_control_operations_result_slot CHECK (result_slot_id IS NULL OR result_slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'),
	CONSTRAINT ck_workflow_control_operations_result_status CHECK (result_status IS NULL OR result_status IN ('active','revoked')),
	CONSTRAINT ck_workflow_control_operations_result_lifecycle CHECK (result_lifecycle IS NULL OR result_lifecycle IN ('active','archived')),
	CONSTRAINT ck_workflow_control_operations_result_published_version_number CHECK (result_published_version_number IS NULL OR result_published_version_number >= 1),
	CONSTRAINT ck_workflow_control_operations_result_draft_revision CHECK (result_draft_revision IS NULL OR result_draft_revision >= 1),
	CONSTRAINT ck_workflow_control_operations_result_missing_slots CHECK (result_missing_slot_ids_csv IS NULL OR result_missing_slot_ids_csv = '' OR result_missing_slot_ids_csv ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}(,[A-Za-z_][A-Za-z0-9_.:-]{0,127})*$'),
	CONSTRAINT ck_workflow_control_operations_version_shape CHECK ((operation IN ('publish','version_grant_put','version_grant_delete')) = (result_version_id IS NOT NULL)),
	CONSTRAINT ck_workflow_control_operations_slot_shape CHECK ((operation IN ('draft_grant_put','draft_grant_delete','version_grant_put','version_grant_delete')) = (result_slot_id IS NOT NULL)),
	CONSTRAINT ck_workflow_control_operations_credential_shape CHECK ((operation IN ('draft_grant_put','version_grant_put','version_grant_delete') AND result_credential_id IS NOT NULL AND result_credential_version_id IS NOT NULL AND result_checksum IS NOT NULL) OR (operation NOT IN ('draft_grant_put','version_grant_put','version_grant_delete') AND result_credential_id IS NULL AND result_credential_version_id IS NULL AND result_checksum IS NULL)),
	CONSTRAINT ck_workflow_control_operations_delete_shape CHECK ((operation = 'draft_grant_delete' AND result_deleted IS TRUE) OR (operation <> 'draft_grant_delete' AND result_deleted IS NULL)),
	CONSTRAINT ck_workflow_control_operations_definition_shape CHECK ((operation IN ('create','update','archive') AND result_name IS NOT NULL AND result_description IS NOT NULL AND result_lifecycle IS NOT NULL AND result_revision IS NOT NULL AND result_draft_revision IS NOT NULL AND result_draft_checksum IS NOT NULL AND result_created_at IS NOT NULL AND result_updated_at IS NOT NULL) OR (operation NOT IN ('create','update','archive') AND result_name IS NULL AND result_description IS NULL AND result_lifecycle IS NULL AND result_published_version_id IS NULL AND result_published_version_number IS NULL AND result_draft_revision IS NULL)),
	CONSTRAINT ck_workflow_control_operations_lifecycle_shape CHECK ((operation = 'archive' AND result_lifecycle = 'archived') OR (operation IN ('create','update') AND result_lifecycle = 'active') OR (operation NOT IN ('create','update','archive') AND result_lifecycle IS NULL)),
	CONSTRAINT ck_workflow_control_operations_publication_shape CHECK ((result_published_version_id IS NULL AND result_published_version_number IS NULL) OR (result_published_version_id IS NOT NULL AND result_published_version_number IS NOT NULL)),
	CONSTRAINT ck_workflow_control_operations_draft_shape CHECK ((operation IN ('create','update','archive') AND result_draft_revision IS NOT NULL AND result_draft_checksum IS NOT NULL) OR (operation = 'save_draft' AND result_draft_revision IS NULL AND result_draft_checksum IS NOT NULL) OR (operation NOT IN ('create','update','archive','save_draft') AND result_draft_revision IS NULL AND result_draft_checksum IS NULL)),
	CONSTRAINT ck_workflow_control_operations_revision_shape CHECK ((operation IN ('create','update','save_draft','archive','version_grant_put','version_grant_delete') AND result_revision IS NOT NULL) OR (operation NOT IN ('create','update','save_draft','archive','version_grant_put','version_grant_delete') AND result_revision IS NULL)),
	CONSTRAINT ck_workflow_control_operations_status_shape CHECK ((operation = 'version_grant_put' AND result_status IS NOT NULL AND result_status = 'active') OR (operation = 'version_grant_delete' AND result_status IS NOT NULL AND result_status = 'revoked') OR (operation NOT IN ('version_grant_put','version_grant_delete') AND result_status IS NULL)),
	CONSTRAINT ck_workflow_control_operations_created_at_shape CHECK ((operation IN ('create','update','archive','version_grant_put','version_grant_delete') AND result_created_at IS NOT NULL) OR (operation NOT IN ('create','update','archive','version_grant_put','version_grant_delete') AND result_created_at IS NULL)),
	CONSTRAINT ck_workflow_control_operations_updated_at_shape CHECK ((operation IN ('create','update','save_draft','archive','draft_grant_put') AND result_updated_at IS NOT NULL) OR (operation NOT IN ('create','update','save_draft','archive','draft_grant_put') AND result_updated_at IS NULL)),
	CONSTRAINT ck_workflow_control_operations_revoked_at_shape CHECK ((operation = 'version_grant_delete' AND result_revoked_at IS NOT NULL) OR (operation <> 'version_grant_delete' AND result_revoked_at IS NULL)),
	CONSTRAINT ck_workflow_control_operations_publish_shape CHECK ((operation = 'publish' AND result_missing_slot_ids_csv IS NOT NULL) OR (operation <> 'publish' AND result_missing_slot_ids_csv IS NULL)),
	CONSTRAINT fk_workflow_control_operations_definition FOREIGN KEY(workflow_id, project_id) REFERENCES workflow_definitions (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_control_operations_result_version FOREIGN KEY(result_version_id, workflow_id, project_id) REFERENCES workflow_versions (id, workflow_id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_control_operations_published_version FOREIGN KEY(result_published_version_id, workflow_id, project_id) REFERENCES workflow_versions (id, workflow_id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_control_operations_actor FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE INDEX ix_workflow_control_operations_project_created ON workflow_control_operations (project_id, created_at DESC);
CREATE TABLE workflow_version_model_refs (
	workflow_version_id UUID NOT NULL,
	project_id UUID NOT NULL,
	node_id UUID NOT NULL,
	logical_model_name VARCHAR(128) NOT NULL,
	purpose VARCHAR(64) NOT NULL,
	PRIMARY KEY (workflow_version_id, node_id, purpose),
	CONSTRAINT ck_workflow_version_model_refs_purpose CHECK (purpose ~ '^[a-z][a-z0-9._-]{0,63}$'),
	CONSTRAINT ck_workflow_version_model_refs_name CHECK (logical_model_name = btrim(logical_model_name) AND logical_model_name <> ''),
	CONSTRAINT uq_workflow_version_model_refs_exact UNIQUE (workflow_version_id, project_id, node_id, purpose, logical_model_name),
	CONSTRAINT fk_workflow_version_model_refs_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT
);
CREATE TABLE workflow_draft_credential_grant_intents (
	workflow_id UUID NOT NULL,
	project_id UUID NOT NULL,
	slot_id VARCHAR(128) NOT NULL,
	slot_schema_checksum CHAR(64) NOT NULL,
	credential_scope VARCHAR(16) DEFAULT 'project' NOT NULL,
	credential_id UUID NOT NULL,
	expected_credential_version_id UUID NOT NULL,
	updated_by VARCHAR(36) NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_id, slot_id),
	CONSTRAINT ck_workflow_draft_grant_intents_slot CHECK (slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'),
	CONSTRAINT ck_workflow_draft_grant_intents_checksum CHECK (slot_schema_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_draft_grant_intents_scope CHECK (credential_scope = 'project'),
	CONSTRAINT fk_workflow_draft_grant_intents_definition FOREIGN KEY(workflow_id, project_id) REFERENCES workflow_definitions (id, project_id) ON DELETE CASCADE,
	CONSTRAINT fk_workflow_draft_grant_intents_credential_scope FOREIGN KEY(credential_id, credential_scope) REFERENCES credentials (id, scope) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_draft_grant_intents_project_credential FOREIGN KEY(project_id, credential_id) REFERENCES credentials (project_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_draft_grant_intents_credential_version FOREIGN KEY(credential_id, expected_credential_version_id) REFERENCES credential_versions (credential_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_draft_grant_intents_updated_by FOREIGN KEY(updated_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE TABLE workflow_version_credential_slots (
	workflow_version_id UUID NOT NULL,
	project_id UUID NOT NULL,
	slot_id VARCHAR(128) NOT NULL,
	name VARCHAR(120) NOT NULL,
	purpose VARCHAR(64) NOT NULL,
	payload_schema_json JSONB NOT NULL,
	payload_schema_checksum CHAR(64) NOT NULL,
	required BOOLEAN DEFAULT true NOT NULL,
	PRIMARY KEY (workflow_version_id, slot_id),
	CONSTRAINT ck_workflow_version_credential_slots_slot CHECK (slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'),
	CONSTRAINT ck_workflow_version_credential_slots_purpose CHECK (purpose ~ '^[a-z][a-z0-9._-]{0,63}$'),
	CONSTRAINT ck_workflow_version_credential_slots_schema_object CHECK (jsonb_typeof(payload_schema_json) = 'object'),
	CONSTRAINT ck_workflow_version_credential_slots_checksum CHECK (payload_schema_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_version_credential_slots_required CHECK (required),
	CONSTRAINT uq_workflow_version_credential_slots_exact UNIQUE (workflow_version_id, slot_id, payload_schema_checksum),
	CONSTRAINT uq_workflow_version_credential_slots_scope UNIQUE (workflow_version_id, project_id, slot_id),
	CONSTRAINT fk_workflow_version_credential_slots_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT
);
CREATE TABLE workflow_version_code_requirements (
	workflow_version_id UUID NOT NULL,
	project_id UUID NOT NULL,
	node_id UUID NOT NULL,
	runtime_contract VARCHAR(128) NOT NULL,
	PRIMARY KEY (workflow_version_id, node_id),
	CONSTRAINT ck_workflow_version_code_requirements_contract CHECK (runtime_contract = 'python3.12-v1'),
	CONSTRAINT fk_workflow_version_code_requirements_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT
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
	CONSTRAINT ck_workflow_version_http_requirements_method CHECK (method IN ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')),
	CONSTRAINT ck_workflow_version_http_requirements_endpoint CHECK (endpoint_policy_id ~ '^[a-z][a-z0-9._-]{0,127}$'),
	CONSTRAINT ck_workflow_version_http_requirements_injection CHECK (injection_profile_id IS NULL OR injection_profile_id ~ '^[a-z][a-z0-9._-]{0,127}$'),
	CONSTRAINT ck_workflow_version_http_requirements_slot CHECK (credential_slot_id IS NULL OR credential_slot_id ~ '^[A-Za-z_][A-Za-z0-9_.:-]{0,127}$'),
	CONSTRAINT ck_workflow_version_http_requirements_auth_pair CHECK ((injection_profile_id IS NULL) = (credential_slot_id IS NULL)),
	CONSTRAINT fk_workflow_version_http_requirements_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_version_http_requirements_slot FOREIGN KEY(workflow_version_id, project_id, credential_slot_id) REFERENCES workflow_version_credential_slots (workflow_version_id, project_id, slot_id) ON DELETE RESTRICT
);
CREATE TABLE workflow_credential_grants (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	workflow_version_id UUID NOT NULL,
	slot_id VARCHAR(128) NOT NULL,
	credential_scope VARCHAR(16) DEFAULT 'project' NOT NULL,
	credential_id UUID NOT NULL,
	credential_version_id UUID NOT NULL,
	payload_schema_checksum CHAR(64) NOT NULL,
	status VARCHAR(16) DEFAULT 'active' NOT NULL,
	revision BIGINT DEFAULT 1 NOT NULL,
	granted_by VARCHAR(36) NOT NULL,
	revoked_by VARCHAR(36),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	revoked_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_workflow_credential_grants_scope CHECK (credential_scope = 'project'),
	CONSTRAINT ck_workflow_credential_grants_status CHECK (status IN ('active', 'revoked')),
	CONSTRAINT ck_workflow_credential_grants_revision CHECK (revision >= 1),
	CONSTRAINT ck_workflow_credential_grants_checksum CHECK (payload_schema_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_credential_grants_lifecycle CHECK ((status = 'active' AND revoked_at IS NULL AND revoked_by IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)),
	CONSTRAINT uq_workflow_credential_grants_exact UNIQUE (id, project_id, workflow_version_id, slot_id, credential_id, credential_version_id, payload_schema_checksum),
	CONSTRAINT fk_workflow_credential_grants_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_credential_grants_slot FOREIGN KEY(workflow_version_id, slot_id, payload_schema_checksum) REFERENCES workflow_version_credential_slots (workflow_version_id, slot_id, payload_schema_checksum) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_credential_grants_credential_scope FOREIGN KEY(credential_id, credential_scope) REFERENCES credentials (id, scope) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_credential_grants_project_credential FOREIGN KEY(project_id, credential_id) REFERENCES credentials (project_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_credential_grants_credential_version FOREIGN KEY(credential_id, credential_version_id) REFERENCES credential_versions (credential_id, id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_credential_grants_granted_by FOREIGN KEY(granted_by) REFERENCES users (id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_credential_grants_revoked_by FOREIGN KEY(revoked_by) REFERENCES users (id) ON DELETE RESTRICT
);
CREATE UNIQUE INDEX uq_workflow_credential_grants_active_slot ON workflow_credential_grants (workflow_version_id, slot_id) WHERE status = 'active';
CREATE TABLE workflow_runs (
	id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	workflow_id UUID NOT NULL,
	workflow_version_id UUID NOT NULL,
	status VARCHAR(32) NOT NULL,
	input_json JSONB NOT NULL,
	output_json JSONB,
	input_digest CHAR(64) NOT NULL,
	idempotency_hash CHAR(64) NOT NULL,
	admission_request_digest CHAR(64) NOT NULL,
	trigger_kind VARCHAR(16) NOT NULL,
	trigger_ref VARCHAR(128),
	origin_trace_id VARCHAR(512) NOT NULL,
	required_worker_profile_digest CHAR(64),
	worker_profile_key CHAR(64) DEFAULT '0000000000000000000000000000000000000000000000000000000000000000' NOT NULL,
	execution_epoch BIGINT DEFAULT 1 NOT NULL,
	current_job_id UUID,
	retry_of_run_id UUID,
	error_code VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	started_at TIMESTAMP WITH TIME ZONE,
	completed_at TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_workflow_runs_status CHECK (status IN ('queued','running','succeeded','failed','cancelled','side_effect_unknown')),
	CONSTRAINT ck_workflow_runs_trigger CHECK (trigger_kind IN ('manual','api')),
	CONSTRAINT ck_workflow_runs_epoch CHECK (execution_epoch >= 1),
	CONSTRAINT ck_workflow_runs_input_object CHECK (jsonb_typeof(input_json) = 'object'),
	CONSTRAINT ck_workflow_runs_output_object CHECK (output_json IS NULL OR jsonb_typeof(output_json) = 'object'),
	CONSTRAINT ck_workflow_runs_input_digest CHECK (input_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_runs_idempotency CHECK (idempotency_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_runs_admission_request_digest CHECK (admission_request_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_runs_profile_digest CHECK ((required_worker_profile_digest IS NULL AND worker_profile_key = '0000000000000000000000000000000000000000000000000000000000000000') OR (required_worker_profile_digest IS NOT NULL AND required_worker_profile_digest ~ '^[0-9a-f]{64}$' AND worker_profile_key = required_worker_profile_digest)),
	CONSTRAINT ck_workflow_runs_lifecycle CHECK ((status = 'queued' AND started_at IS NULL AND completed_at IS NULL AND output_json IS NULL AND error_code IS NULL) OR (status = 'running' AND started_at IS NOT NULL AND started_at >= created_at AND completed_at IS NULL AND output_json IS NULL AND error_code IS NULL) OR (status = 'succeeded' AND started_at IS NOT NULL AND completed_at IS NOT NULL AND started_at >= created_at AND completed_at >= started_at AND output_json IS NOT NULL AND error_code IS NULL) OR (status IN ('failed','side_effect_unknown') AND started_at IS NOT NULL AND completed_at IS NOT NULL AND error_code IS NOT NULL AND started_at >= created_at AND completed_at >= started_at AND output_json IS NULL AND error_code ~ '^[A-Z][A-Z0-9_]{0,63}$') OR (status = 'cancelled' AND completed_at IS NOT NULL AND completed_at >= COALESCE(started_at, created_at) AND (started_at IS NULL OR started_at >= created_at) AND output_json IS NULL AND error_code IS NULL)),
	CONSTRAINT ck_workflow_runs_retry_self CHECK (retry_of_run_id IS NULL OR retry_of_run_id <> id),
	CONSTRAINT uq_workflow_runs_scope UNIQUE (id, project_id, owner_user_id),
	CONSTRAINT uq_workflow_runs_scope_version UNIQUE (id, project_id, owner_user_id, workflow_version_id),
	CONSTRAINT uq_workflow_runs_retry_scope UNIQUE (id, project_id, owner_user_id, workflow_id, workflow_version_id),
	CONSTRAINT uq_workflow_runs_snapshot_scope UNIQUE (id, project_id, owner_user_id, workflow_version_id, worker_profile_key),
	CONSTRAINT uq_workflow_runs_scope_profile UNIQUE (id, project_id, owner_user_id, worker_profile_key),
	CONSTRAINT uq_workflow_runs_trace_scope UNIQUE (id, project_id, owner_user_id, origin_trace_id),
	CONSTRAINT uq_workflow_runs_epoch UNIQUE (id, execution_epoch),
	CONSTRAINT uq_workflow_runs_epoch_profile UNIQUE (id, execution_epoch, worker_profile_key),
	CONSTRAINT uq_workflow_runs_idempotency UNIQUE (project_id, owner_user_id, workflow_id, idempotency_hash),
	CONSTRAINT fk_workflow_runs_project FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_runs_owner FOREIGN KEY(owner_user_id) REFERENCES users (id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_runs_membership FOREIGN KEY(project_id, owner_user_id) REFERENCES project_memberships (project_id, user_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_runs_definition FOREIGN KEY(workflow_id, project_id) REFERENCES workflow_definitions (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_runs_version FOREIGN KEY(workflow_version_id, workflow_id, project_id) REFERENCES workflow_versions (id, workflow_id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_runs_retry FOREIGN KEY(retry_of_run_id, project_id, owner_user_id, workflow_id, workflow_version_id) REFERENCES workflow_runs (id, project_id, owner_user_id, workflow_id, workflow_version_id) ON DELETE RESTRICT
);
CREATE INDEX ix_workflow_runs_active ON workflow_runs (project_id, owner_user_id, status) WHERE status IN ('queued','running');
CREATE INDEX ix_workflow_runs_history ON workflow_runs (project_id, owner_user_id, created_at DESC, id DESC);

ALTER TABLE workflow_definitions
    ADD CONSTRAINT fk_workflow_definitions_current_version
    FOREIGN KEY (id, current_published_version_id)
    REFERENCES workflow_versions (workflow_id, id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE jobs
    DROP CONSTRAINT ck_jobs_authority_shape,
    DROP CONSTRAINT ck_jobs_type,
    ADD CONSTRAINT ck_jobs_type CHECK (
        job_type IN (
            'private_run', 'automation_run', 'workflow_run',
            'retention_purge', 'mcp_discovery', 'memory_dream', 'memory_seal'
        )
    ),
    ADD CONSTRAINT ck_jobs_workflow_profile CHECK (
        (
            workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
        ) OR (
            workflow_run_id IS NOT NULL
            AND workflow_epoch >= 1
            AND workflow_profile_key ~ '^[0-9a-f]{64}$'
            AND (
                (
                    required_worker_profile_digest IS NULL
                    AND workflow_profile_key = '0000000000000000000000000000000000000000000000000000000000000000'
                ) OR (
                    required_worker_profile_digest IS NOT NULL
                    AND required_worker_profile_digest ~ '^[0-9a-f]{64}$'
                    AND required_worker_profile_digest = workflow_profile_key
                )
            )
        )
    ),
    ADD CONSTRAINT ck_jobs_authority_shape CHECK (
        (
            job_type = 'private_run'
            AND run_id IS NOT NULL
            AND workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
            AND owner_user_id IS NOT NULL
            AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NOT NULL
        ) OR (
            job_type = 'automation_run'
            AND run_id IS NOT NULL
            AND workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
            AND owner_user_id IS NOT NULL
            AND automation_occurrence_id IS NOT NULL
            AND origin_trace_id IS NOT NULL
        ) OR (
            job_type = 'workflow_run'
            AND run_id IS NULL
            AND workflow_run_id IS NOT NULL
            AND workflow_epoch IS NOT NULL
            AND workflow_profile_key IS NOT NULL
            AND owner_user_id IS NOT NULL
            AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NOT NULL
        ) OR (
            job_type = 'retention_purge'
            AND run_id IS NULL
            AND workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
            AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NULL
        ) OR (
            job_type = 'mcp_discovery'
            AND owner_user_id IS NOT NULL
            AND run_id IS NULL
            AND workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
            AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NULL
        ) OR (
            job_type = 'memory_dream'
            AND owner_user_id IS NOT NULL
            AND namespace IS NOT NULL
            AND namespace <> ''
            AND run_id IS NULL
            AND workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
            AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NULL
        ) OR (
            job_type = 'memory_seal'
            AND owner_user_id IS NOT NULL
            AND namespace IS NOT NULL
            AND namespace <> ''
            AND run_id IS NULL
            AND workflow_run_id IS NULL
            AND workflow_epoch IS NULL
            AND required_worker_profile_digest IS NULL
            AND workflow_profile_key IS NULL
            AND automation_occurrence_id IS NULL
            AND origin_trace_id IS NULL
        )
    ),
    ADD CONSTRAINT uq_jobs_workflow_epoch_scope
        UNIQUE (id, project_id, owner_user_id, workflow_run_id, workflow_epoch),
    ADD CONSTRAINT uq_jobs_workflow_epoch_profile_scope
        UNIQUE (
            id, project_id, owner_user_id, workflow_run_id,
            workflow_epoch, workflow_profile_key
        ),
    ADD CONSTRAINT fk_jobs_workflow_run
        FOREIGN KEY (
            workflow_run_id, project_id, owner_user_id, origin_trace_id
        )
        REFERENCES workflow_runs (
            id, project_id, owner_user_id, origin_trace_id
        ) ON DELETE RESTRICT;

CREATE INDEX ix_jobs_workflow_claim
    ON jobs (
        status, job_type, required_worker_profile_digest,
        priority DESC, available_at, created_at, id
    )
    WHERE job_type = 'workflow_run';

ALTER TABLE workflow_runs
    ADD CONSTRAINT fk_workflow_runs_current_job
    FOREIGN KEY (
        current_job_id, project_id, owner_user_id, id,
        execution_epoch, worker_profile_key
    )
    REFERENCES jobs (
        id, project_id, owner_user_id, workflow_run_id,
        workflow_epoch, workflow_profile_key
    ) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE workflow_run_jobs (
	workflow_run_id UUID NOT NULL,
	execution_epoch BIGINT NOT NULL,
	job_id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	worker_profile_key CHAR(64) NOT NULL,
	cause VARCHAR(16) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_run_id, execution_epoch),
	CONSTRAINT ck_workflow_run_jobs_epoch CHECK (execution_epoch >= 1),
	CONSTRAINT ck_workflow_run_jobs_cause CHECK ((cause = 'initial' AND execution_epoch = 1) OR (cause = 'resume' AND execution_epoch >= 2)),
	CONSTRAINT ck_workflow_run_jobs_profile_key CHECK (worker_profile_key ~ '^[0-9a-f]{64}$'),
	CONSTRAINT uq_workflow_run_jobs_job UNIQUE (job_id),
	CONSTRAINT uq_workflow_run_jobs_run_epoch_job UNIQUE (workflow_run_id, execution_epoch, job_id),
	CONSTRAINT fk_workflow_run_jobs_run_scope FOREIGN KEY(workflow_run_id, project_id, owner_user_id, worker_profile_key) REFERENCES workflow_runs (id, project_id, owner_user_id, worker_profile_key) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_run_jobs_job_epoch FOREIGN KEY(job_id, project_id, owner_user_id, workflow_run_id, execution_epoch, worker_profile_key) REFERENCES jobs (id, project_id, owner_user_id, workflow_run_id, workflow_epoch, workflow_profile_key) ON DELETE RESTRICT
);
ALTER TABLE jobs
    ADD CONSTRAINT fk_jobs_workflow_run_mapping
    FOREIGN KEY (workflow_run_id, workflow_epoch, id)
    REFERENCES workflow_run_jobs (workflow_run_id, execution_epoch, job_id)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE workflow_runs
    ADD CONSTRAINT fk_workflow_runs_epoch_mapping
    FOREIGN KEY (id, execution_epoch)
    REFERENCES workflow_run_jobs (workflow_run_id, execution_epoch)
    ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED;
CREATE TABLE workflow_run_snapshots (
	workflow_run_id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	workflow_version_id UUID NOT NULL,
	graph_schema_version SMALLINT NOT NULL,
	compiler_contract_version SMALLINT NOT NULL,
	semantic_checksum CHAR(64) NOT NULL,
	catalog_generation CHAR(64) NOT NULL,
	required_worker_profile_digest CHAR(64),
	worker_profile_key CHAR(64) NOT NULL,
	snapshot_checksum CHAR(64) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_run_id),
	CONSTRAINT ck_workflow_run_snapshots_schema CHECK (graph_schema_version >= 1 AND compiler_contract_version >= 1),
	CONSTRAINT ck_workflow_run_snapshots_generation CHECK (catalog_generation ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_snapshots_checksums CHECK (semantic_checksum ~ '^[0-9a-f]{64}$' AND snapshot_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_snapshots_profile_digest CHECK ((required_worker_profile_digest IS NULL AND worker_profile_key = '0000000000000000000000000000000000000000000000000000000000000000') OR (required_worker_profile_digest IS NOT NULL AND required_worker_profile_digest ~ '^[0-9a-f]{64}$' AND worker_profile_key = required_worker_profile_digest)),
	CONSTRAINT fk_workflow_run_snapshots_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id, workflow_version_id, worker_profile_key) REFERENCES workflow_runs (id, project_id, owner_user_id, workflow_version_id, worker_profile_key) ON DELETE CASCADE,
	CONSTRAINT fk_workflow_run_snapshots_version FOREIGN KEY(workflow_version_id, project_id, graph_schema_version, compiler_contract_version, semantic_checksum) REFERENCES workflow_versions (id, project_id, graph_schema_version, compiler_contract_version, semantic_checksum) ON DELETE RESTRICT
);
CREATE TABLE workflow_run_runtime_policy_snapshots (
	workflow_run_id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	section VARCHAR(32) DEFAULT 'workflow_runtime' NOT NULL,
	policy_version_id UUID NOT NULL,
	revision BIGINT NOT NULL,
	schema_version SMALLINT NOT NULL,
	payload_checksum CHAR(64) NOT NULL,
	value_json JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_run_id),
	CONSTRAINT ck_workflow_run_runtime_policy_snapshots_section CHECK (section = 'workflow_runtime'),
	CONSTRAINT ck_workflow_run_runtime_policy_snapshots_versions CHECK (revision >= 1 AND schema_version >= 1),
	CONSTRAINT ck_workflow_run_runtime_policy_snapshots_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_runtime_policy_snapshots_value CHECK (jsonb_typeof(value_json) = 'object'),
	CONSTRAINT fk_workflow_run_runtime_policy_snapshots_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id) REFERENCES workflow_runs (id, project_id, owner_user_id) ON DELETE CASCADE,
	CONSTRAINT fk_workflow_run_runtime_policy_snapshots_exact_policy FOREIGN KEY(section, policy_version_id, revision, schema_version, payload_checksum) REFERENCES system_runtime_policy_versions (section, id, version_number, schema_version, payload_checksum) ON DELETE RESTRICT
);
CREATE TABLE workflow_run_model_snapshots (
	workflow_run_id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	workflow_version_id UUID NOT NULL,
	node_id UUID NOT NULL,
	purpose VARCHAR(64) NOT NULL,
	logical_model_name VARCHAR(128) NOT NULL,
	model_config_id UUID NOT NULL,
	model_config_version_id UUID NOT NULL,
	payload_checksum CHAR(64) NOT NULL,
	credential_id UUID,
	credential_version_id UUID,
	credential_env_key VARCHAR(255),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_run_id, node_id, purpose),
	CONSTRAINT ck_workflow_run_model_snapshots_purpose CHECK (purpose ~ '^[a-z][a-z0-9._-]{0,63}$'),
	CONSTRAINT ck_workflow_run_model_snapshots_checksum CHECK (payload_checksum ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_model_snapshots_credential_group CHECK ((credential_id IS NULL AND credential_version_id IS NULL AND credential_env_key IS NULL) OR (credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND credential_env_key IS NOT NULL)),
	CONSTRAINT fk_workflow_run_model_snapshots_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id, workflow_version_id) REFERENCES workflow_runs (id, project_id, owner_user_id, workflow_version_id) ON DELETE CASCADE,
	CONSTRAINT fk_workflow_run_model_snapshots_model_ref FOREIGN KEY(workflow_version_id, project_id, node_id, purpose, logical_model_name) REFERENCES workflow_version_model_refs (workflow_version_id, project_id, node_id, purpose, logical_model_name) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_run_model_snapshots_exact_model FOREIGN KEY(model_config_id, model_config_version_id, payload_checksum) REFERENCES system_model_config_versions (model_config_id, id, payload_checksum) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_run_model_snapshots_credential_closure FOREIGN KEY(model_config_id, model_config_version_id, payload_checksum, credential_id, credential_version_id, credential_env_key) REFERENCES system_model_config_versions (model_config_id, id, payload_checksum, credential_id, credential_version_id, credential_env_key) ON DELETE RESTRICT
);
CREATE TABLE workflow_run_code_snapshots (
	workflow_run_id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	node_id UUID NOT NULL,
	runtime_name VARCHAR(32) NOT NULL,
	runner_contract_version SMALLINT NOT NULL,
	image_digest VARCHAR(71) NOT NULL,
	isolation_policy_checksum CHAR(64) NOT NULL,
	profile_digest CHAR(64) NOT NULL,
	timeout_ms BIGINT NOT NULL,
	max_output_bytes BIGINT NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_run_id, node_id),
	CONSTRAINT ck_workflow_run_code_snapshots_runtime CHECK (runtime_name = 'python3.12'),
	CONSTRAINT ck_workflow_run_code_snapshots_contract CHECK (runner_contract_version >= 1),
	CONSTRAINT ck_workflow_run_code_snapshots_image CHECK (image_digest ~ '^sha256:[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_code_snapshots_digests CHECK (isolation_policy_checksum ~ '^[0-9a-f]{64}$' AND profile_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_code_snapshots_limits CHECK (timeout_ms BETWEEN 1 AND 31536000000 AND max_output_bytes BETWEEN 1 AND 2147483648),
	CONSTRAINT fk_workflow_run_code_snapshots_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id) REFERENCES workflow_runs (id, project_id, owner_user_id) ON DELETE CASCADE
);
CREATE TABLE workflow_run_http_snapshots (
	workflow_run_id UUID NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	workflow_version_id UUID NOT NULL,
	node_id UUID NOT NULL,
	http_method VARCHAR(6) NOT NULL,
	normalized_origin TEXT NOT NULL,
	endpoint_policy_revision BIGINT NOT NULL,
	endpoint_policy_checksum CHAR(64) NOT NULL,
	injection_profile_revision BIGINT NOT NULL,
	injection_profile_checksum CHAR(64) NOT NULL,
	egress_profile_digest CHAR(64) NOT NULL,
	timeout_ms BIGINT NOT NULL,
	max_request_bytes BIGINT NOT NULL,
	max_response_bytes BIGINT NOT NULL,
	credential_slot_id VARCHAR(128),
	credential_grant_id UUID,
	credential_id UUID,
	credential_version_id UUID,
	payload_schema_checksum CHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (workflow_run_id, node_id),
	CONSTRAINT ck_workflow_run_http_snapshots_method CHECK (http_method IN ('GET','HEAD','POST','PUT','PATCH','DELETE')),
	CONSTRAINT ck_workflow_run_http_snapshots_origin CHECK (normalized_origin ~ '^https://[^/?#]+$'),
	CONSTRAINT ck_workflow_run_http_snapshots_revisions CHECK (endpoint_policy_revision >= 1 AND injection_profile_revision >= 1),
	CONSTRAINT ck_workflow_run_http_snapshots_digests CHECK (endpoint_policy_checksum ~ '^[0-9a-f]{64}$' AND injection_profile_checksum ~ '^[0-9a-f]{64}$' AND egress_profile_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_run_http_snapshots_limits CHECK (timeout_ms BETWEEN 1 AND 31536000000 AND max_request_bytes BETWEEN 0 AND 2147483648 AND max_response_bytes BETWEEN 1 AND 2097152),
	CONSTRAINT ck_workflow_run_http_snapshots_credential_group CHECK ((credential_slot_id IS NULL AND credential_grant_id IS NULL AND credential_id IS NULL AND credential_version_id IS NULL AND payload_schema_checksum IS NULL) OR (credential_slot_id IS NOT NULL AND credential_grant_id IS NOT NULL AND credential_id IS NOT NULL AND credential_version_id IS NOT NULL AND payload_schema_checksum IS NOT NULL AND payload_schema_checksum ~ '^[0-9a-f]{64}$')),
	CONSTRAINT fk_workflow_run_http_snapshots_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id, workflow_version_id) REFERENCES workflow_runs (id, project_id, owner_user_id, workflow_version_id) ON DELETE CASCADE,
	CONSTRAINT fk_workflow_run_http_snapshots_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_run_http_snapshots_slot FOREIGN KEY(workflow_version_id, credential_slot_id, payload_schema_checksum) REFERENCES workflow_version_credential_slots (workflow_version_id, slot_id, payload_schema_checksum) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_run_http_snapshots_grant FOREIGN KEY(credential_grant_id, project_id, workflow_version_id, credential_slot_id, credential_id, credential_version_id, payload_schema_checksum) REFERENCES workflow_credential_grants (id, project_id, workflow_version_id, slot_id, credential_id, credential_version_id, payload_schema_checksum) ON DELETE RESTRICT
);
CREATE TABLE workflow_code_sandbox_leases (
	id UUID NOT NULL,
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
	cleanup_deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	cleanup_handoff_at TIMESTAMP WITH TIME ZONE,
	cleanup_owner_worker_id UUID,
	cleanup_lease_token_hash CHAR(64),
	cleanup_lease_expires_at TIMESTAMP WITH TIME ZONE,
	cleanup_attempt INTEGER DEFAULT 0 NOT NULL,
	destroyed_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_workflow_code_leases_activation_attempt UNIQUE (workflow_run_id, node_id, activation_id, activation_attempt),
	CONSTRAINT fk_workflow_code_leases_scope FOREIGN KEY(workflow_run_id, project_id, owner_user_id) REFERENCES workflow_runs (id, project_id, owner_user_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_code_leases_job_attempt FOREIGN KEY(job_id, job_attempt_number, worker_id) REFERENCES job_attempts (job_id, attempt_number, worker_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_code_leases_job_scope FOREIGN KEY(job_id, project_id, owner_user_id, workflow_run_id, workflow_epoch, profile_digest) REFERENCES jobs (id, project_id, owner_user_id, workflow_run_id, workflow_epoch, workflow_profile_key) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_code_leases_run_job_mapping FOREIGN KEY(workflow_run_id, workflow_epoch, job_id) REFERENCES workflow_run_jobs (workflow_run_id, execution_epoch, job_id) ON DELETE RESTRICT,
	CONSTRAINT ck_workflow_code_leases_shape CHECK (activation_attempt >= 1 AND workflow_epoch >= 1 AND job_attempt_number >= 1 AND cleanup_attempt >= 0 AND reconciliation_key_hash ~ '^[0-9a-f]{64}$' AND profile_digest ~ '^[0-9a-f]{64}$' AND ((state = 'provisioning' AND execution_lease_token_hash IS NOT NULL AND execution_lease_token_hash ~ '^[0-9a-f]{64}$' AND cleanup_locator_ciphertext IS NULL AND cleanup_handoff_at IS NULL AND cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL AND cleanup_lease_expires_at IS NULL AND destroyed_at IS NULL) OR (state = 'running' AND execution_lease_token_hash IS NOT NULL AND execution_lease_token_hash ~ '^[0-9a-f]{64}$' AND cleanup_locator_ciphertext IS NOT NULL AND octet_length(cleanup_locator_ciphertext) > 0 AND cleanup_handoff_at IS NULL AND cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL AND cleanup_lease_expires_at IS NULL AND destroyed_at IS NULL) OR (state = 'cleanup_pending' AND execution_lease_token_hash IS NULL AND cleanup_handoff_at IS NOT NULL AND destroyed_at IS NULL AND ((cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL AND cleanup_lease_expires_at IS NULL) OR (cleanup_owner_worker_id IS NOT NULL AND cleanup_lease_token_hash IS NOT NULL AND cleanup_lease_token_hash ~ '^[0-9a-f]{64}$' AND cleanup_lease_expires_at IS NOT NULL))) OR (state = 'destroyed' AND execution_lease_token_hash IS NULL AND cleanup_locator_ciphertext IS NULL AND cleanup_handoff_at IS NULL AND cleanup_owner_worker_id IS NULL AND cleanup_lease_token_hash IS NULL AND cleanup_lease_expires_at IS NULL AND destroyed_at IS NOT NULL)))
);
CREATE INDEX ix_workflow_code_leases_cleanup_claim ON workflow_code_sandbox_leases (state, cleanup_lease_expires_at, created_at, id) WHERE state IN ('provisioning','running','cleanup_pending');
CREATE UNIQUE INDEX uq_workflow_code_leases_open_activation ON workflow_code_sandbox_leases (workflow_run_id, node_id, activation_id) WHERE state <> 'destroyed';
CREATE TABLE workflow_node_effects (
	id UUID NOT NULL,
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
	dispatch_attempt INTEGER,
	dispatch_owner_id UUID,
	dispatch_lease_token_hash CHAR(64),
	dispatch_started_at TIMESTAMP WITH TIME ZONE,
	outcome_json JSONB,
	outcome_digest CHAR(64),
	safe_error_code VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_workflow_node_effects_method CHECK (http_method IN ('POST','PUT','PATCH','DELETE')),
	CONSTRAINT ck_workflow_node_effects_status CHECK (status IN ('prepared','dispatching','settled','failed_safe','unknown')),
	CONSTRAINT ck_workflow_node_effects_revision CHECK (revision >= 1),
	CONSTRAINT ck_workflow_node_effects_request_hmac CHECK (request_hmac ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_node_effects_operation_key CHECK (operation_key ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_node_effects_provider_key CHECK (provider_idempotency_key ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_node_effects_epoch_attempt CHECK ((dispatch_execution_epoch IS NULL OR dispatch_execution_epoch >= 1) AND (dispatch_attempt IS NULL OR dispatch_attempt >= 1)),
	CONSTRAINT ck_workflow_node_effects_lease_hash CHECK (dispatch_lease_token_hash IS NULL OR dispatch_lease_token_hash ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_node_effects_outcome_digest CHECK (outcome_digest IS NULL OR outcome_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_workflow_node_effects_safe_error CHECK (safe_error_code IS NULL OR safe_error_code ~ '^[A-Z][A-Z0-9_]{0,63}$'),
	CONSTRAINT ck_workflow_node_effects_state_shape CHECK ((status = 'prepared' AND dispatch_job_id IS NULL AND dispatch_execution_epoch IS NULL AND dispatch_attempt IS NULL AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL AND dispatch_started_at IS NULL AND outcome_json IS NULL AND outcome_digest IS NULL AND safe_error_code IS NULL) OR (status = 'dispatching' AND dispatch_job_id IS NOT NULL AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL AND dispatch_owner_id IS NOT NULL AND dispatch_lease_token_hash IS NOT NULL AND dispatch_started_at IS NOT NULL AND outcome_json IS NULL AND outcome_digest IS NULL AND safe_error_code IS NULL) OR (status = 'settled' AND dispatch_job_id IS NOT NULL AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL AND dispatch_started_at IS NOT NULL AND outcome_json IS NOT NULL AND workflow_http_settled_outcome_is_valid(outcome_json) AND outcome_digest IS NOT NULL AND safe_error_code IS NULL) OR (status = 'failed_safe' AND dispatch_job_id IS NOT NULL AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL AND dispatch_started_at IS NOT NULL AND outcome_json IS NULL AND outcome_digest IS NULL AND safe_error_code IS NOT NULL AND safe_error_code <> 'SIDE_EFFECT_STATE_UNKNOWN') OR (status = 'unknown' AND dispatch_job_id IS NOT NULL AND dispatch_execution_epoch IS NOT NULL AND dispatch_attempt IS NOT NULL AND dispatch_owner_id IS NULL AND dispatch_lease_token_hash IS NULL AND dispatch_started_at IS NOT NULL AND outcome_json IS NULL AND outcome_digest IS NULL AND safe_error_code IS NOT NULL AND safe_error_code = 'SIDE_EFFECT_STATE_UNKNOWN')),
	CONSTRAINT uq_workflow_node_effects_operation UNIQUE (workflow_run_id, node_id, activation_key, operation_key),
	CONSTRAINT uq_workflow_node_effects_activation UNIQUE (workflow_run_id, node_id, activation_key),
	CONSTRAINT fk_workflow_node_effects_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id) REFERENCES workflow_runs (id, project_id, owner_user_id) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_node_effects_dispatch_job FOREIGN KEY(dispatch_job_id, project_id, owner_user_id, workflow_run_id, dispatch_execution_epoch) REFERENCES jobs (id, project_id, owner_user_id, workflow_run_id, workflow_epoch) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_node_effects_dispatch_attempt FOREIGN KEY(dispatch_job_id, dispatch_attempt) REFERENCES job_attempts (job_id, attempt_number) ON DELETE RESTRICT,
	CONSTRAINT fk_workflow_node_effects_dispatch_worker FOREIGN KEY(dispatch_job_id, dispatch_attempt, dispatch_owner_id) REFERENCES job_attempts (job_id, attempt_number, worker_id) ON DELETE RESTRICT
);
CREATE TABLE workflow_run_event_partition_state (
	singleton BOOLEAN DEFAULT true NOT NULL,
	retained_from TIMESTAMP WITH TIME ZONE,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (singleton),
	CONSTRAINT ck_workflow_run_event_partition_state_singleton CHECK (singleton)
);
CREATE TABLE workflow_run_event_invariants (
	id BIGINT NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	workflow_run_id UUID NOT NULL,
	workflow_version_id UUID NOT NULL,
	seq BIGINT NOT NULL,
	is_terminal BOOLEAN DEFAULT false NOT NULL,
	node_id UUID,
	activation_id VARCHAR(128),
	scope_path_hash CHAR(64),
	iteration_path INTEGER[] DEFAULT '{}'::integer[] NOT NULL,
	attempt INTEGER,
	PRIMARY KEY (id),
	CONSTRAINT ck_workflow_run_event_invariants_seq CHECK (seq >= 1),
	CONSTRAINT ck_workflow_run_event_invariants_activation CHECK ((node_id IS NULL AND activation_id IS NULL AND scope_path_hash IS NULL AND attempt IS NULL AND cardinality(iteration_path) = 0) OR (node_id IS NOT NULL AND activation_id IS NOT NULL AND activation_id ~ '^[A-Za-z0-9._:-]+$' AND scope_path_hash IS NOT NULL AND scope_path_hash ~ '^[0-9a-f]{64}$' AND attempt IS NOT NULL AND attempt >= 1 AND cardinality(iteration_path) <= 16 AND array_position(iteration_path, NULL) IS NULL AND 0 < ALL(iteration_path))),
	CONSTRAINT uq_workflow_run_events_private_seq UNIQUE (project_id, owner_user_id, workflow_run_id, seq),
	CONSTRAINT fk_workflow_run_event_invariants_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id, workflow_version_id) REFERENCES workflow_runs (id, project_id, owner_user_id, workflow_version_id) ON DELETE CASCADE
);
CREATE INDEX ix_workflow_run_event_invariants_activation_attempt ON workflow_run_event_invariants (workflow_run_id, node_id, activation_id, scope_path_hash, iteration_path, attempt) WHERE activation_id IS NOT NULL;
CREATE INDEX ix_workflow_run_event_invariants_occurred_at ON workflow_run_event_invariants (occurred_at);
CREATE UNIQUE INDEX uq_workflow_run_events_terminal ON workflow_run_event_invariants (project_id, owner_user_id, workflow_run_id) WHERE is_terminal;
CREATE TABLE workflow_run_events (
	id BIGSERIAL NOT NULL,
	project_id UUID NOT NULL,
	owner_user_id VARCHAR(36) NOT NULL,
	workflow_run_id UUID NOT NULL,
	workflow_version_id UUID NOT NULL,
	seq BIGINT NOT NULL,
	event_type VARCHAR(64) NOT NULL,
	node_id UUID,
	activation_id VARCHAR(128),
	scope_path_hash CHAR(64),
	iteration_path INTEGER[] DEFAULT '{}'::integer[] NOT NULL,
	attempt INTEGER,
	payload JSONB DEFAULT '{}'::jsonb NOT NULL,
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id, occurred_at),
	CONSTRAINT ck_workflow_run_events_seq CHECK (seq >= 1),
	CONSTRAINT ck_workflow_run_events_type CHECK (event_type IN ('workflow.run.started','workflow.node.queued','workflow.node.started','workflow.node.delta','workflow.node.log','workflow.node.completed','workflow.node.failed','workflow.run.completed','workflow.run.failed','workflow.run.cancelled','workflow.run.side_effect_unknown')),
	CONSTRAINT ck_workflow_run_events_payload CHECK (jsonb_typeof(payload) = 'object'),
	CONSTRAINT ck_workflow_run_events_iteration_path CHECK (cardinality(iteration_path) <= 16 AND array_position(iteration_path, NULL) IS NULL AND 0 < ALL(iteration_path)),
	CONSTRAINT ck_workflow_run_events_activation CHECK ((event_type LIKE 'workflow.node.%' AND node_id IS NOT NULL AND activation_id IS NOT NULL AND activation_id ~ '^[A-Za-z0-9._:-]+$' AND scope_path_hash IS NOT NULL AND scope_path_hash ~ '^[0-9a-f]{64}$' AND attempt IS NOT NULL AND attempt >= 1) OR (event_type LIKE 'workflow.run.%' AND node_id IS NULL AND activation_id IS NULL AND scope_path_hash IS NULL AND attempt IS NULL AND cardinality(iteration_path) = 0)),
	CONSTRAINT fk_workflow_run_events_run FOREIGN KEY(workflow_run_id, project_id, owner_user_id, workflow_version_id) REFERENCES workflow_runs (id, project_id, owner_user_id, workflow_version_id) ON DELETE CASCADE
)
 PARTITION BY RANGE (occurred_at);
CREATE INDEX ix_workflow_run_events_replay ON workflow_run_events (workflow_run_id, seq);
CREATE INDEX ix_workflow_run_events_scope_time ON workflow_run_events (project_id, owner_user_id, occurred_at DESC, id DESC);

INSERT INTO workflow_run_event_partition_state (singleton) VALUES (true);

CREATE OR REPLACE FUNCTION populate_workflow_snapshot_profile_key()
RETURNS TRIGGER AS $$
DECLARE
    expected_profile_key CHAR(64);
BEGIN
    expected_profile_key := COALESCE(
        NEW.required_worker_profile_digest,
        '0000000000000000000000000000000000000000000000000000000000000000'
    );
    IF NEW.worker_profile_key IS NULL THEN
        NEW.worker_profile_key := expected_profile_key;
    ELSIF NEW.worker_profile_key IS DISTINCT FROM expected_profile_key THEN
        RAISE EXCEPTION 'workflow snapshot profile key mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION validate_workflow_run_runtime_policy_snapshot()
RETURNS TRIGGER AS $$
DECLARE
    expected_value JSONB;
BEGIN
    SELECT version.value
      INTO expected_value
      FROM system_runtime_policy_versions AS version
     WHERE version.section = NEW.section
       AND version.id = NEW.policy_version_id
       AND version.version_number = NEW.revision
       AND version.schema_version = NEW.schema_version
       AND version.payload_checksum = NEW.payload_checksum;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow runtime policy snapshot exact version unavailable'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.value_json IS DISTINCT FROM expected_value THEN
        RAISE EXCEPTION 'workflow runtime policy snapshot value mismatch'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION reject_direct_workflow_snapshot_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'workflow run snapshots are immutable'
            USING ERRCODE = '55000';
    END IF;

    PERFORM 1 FROM workflow_runs WHERE id = OLD.workflow_run_id;
    IF FOUND THEN
        RAISE EXCEPTION 'workflow run snapshots cannot be directly deleted'
            USING ERRCODE = '55000';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_workflow_run_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status IN (
        'succeeded', 'failed', 'cancelled', 'side_effect_unknown'
    ) THEN
        RAISE EXCEPTION 'terminal workflow Run is immutable'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        NEW.id, NEW.project_id, NEW.owner_user_id, NEW.workflow_id,
        NEW.workflow_version_id, NEW.input_json, NEW.input_digest,
        NEW.idempotency_hash, NEW.admission_request_digest,
        NEW.trigger_kind, NEW.trigger_ref,
        NEW.origin_trace_id, NEW.required_worker_profile_digest,
        NEW.worker_profile_key, NEW.retry_of_run_id, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.project_id, OLD.owner_user_id, OLD.workflow_id,
        OLD.workflow_version_id, OLD.input_json, OLD.input_digest,
        OLD.idempotency_hash, OLD.admission_request_digest,
        OLD.trigger_kind, OLD.trigger_ref,
        OLD.origin_trace_id, OLD.required_worker_profile_digest,
        OLD.worker_profile_key, OLD.retry_of_run_id, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'workflow Run admission authority is immutable'
            USING ERRCODE = '55000';
    END IF;

    IF NOT (
        NEW.status = OLD.status
        OR (OLD.status = 'queued' AND NEW.status IN ('running', 'cancelled'))
        OR (
            OLD.status = 'running'
            AND NEW.status IN (
                'succeeded', 'failed', 'cancelled', 'side_effect_unknown'
            )
        )
    ) THEN
        RAISE EXCEPTION 'invalid workflow Run status transition'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.execution_epoch < OLD.execution_epoch
       OR NEW.execution_epoch > OLD.execution_epoch + 1
       OR (
            OLD.status IN (
                'succeeded', 'failed', 'cancelled', 'side_effect_unknown'
            )
            AND NEW.execution_epoch <> OLD.execution_epoch
       ) THEN
        RAISE EXCEPTION 'invalid workflow Run execution epoch transition'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION enforce_workflow_run_current_job()
RETURNS TRIGGER AS $$
DECLARE
    current_row workflow_runs%ROWTYPE;
BEGIN
    SELECT * INTO current_row
      FROM workflow_runs
     WHERE id = NEW.id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    IF (current_row.status IN ('queued', 'running'))
       IS DISTINCT FROM (current_row.current_job_id IS NOT NULL) THEN
        RAISE EXCEPTION 'workflow Run current Job does not match active status'
            USING ERRCODE = '23514';
    END IF;
    IF current_row.current_job_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
          FROM jobs AS job
          JOIN workflow_run_jobs AS mapping
            ON mapping.job_id = job.id
           AND mapping.workflow_run_id = current_row.id
           AND mapping.execution_epoch = current_row.execution_epoch
         WHERE job.id = current_row.current_job_id
           AND job.job_type = 'workflow_run'
           AND job.project_id = current_row.project_id
           AND job.owner_user_id = current_row.owner_user_id
           AND job.workflow_run_id = current_row.id
           AND job.workflow_epoch = current_row.execution_epoch
           AND job.workflow_profile_key = current_row.worker_profile_key
           AND job.status IN ('queued', 'leased', 'running', 'retry_wait')
    ) THEN
        RAISE EXCEPTION 'workflow Run current Job authority is invalid'
            USING ERRCODE = '23514';
    END IF;
    IF current_row.status IN (
        'succeeded', 'failed', 'cancelled', 'side_effect_unknown'
    ) AND NOT EXISTS (
        SELECT 1
          FROM workflow_run_jobs AS mapping
          JOIN jobs AS terminal_job ON terminal_job.id = mapping.job_id
         WHERE mapping.workflow_run_id = current_row.id
           AND mapping.execution_epoch = current_row.execution_epoch
           AND terminal_job.status NOT IN (
                'queued', 'leased', 'running', 'retry_wait'
           )
    ) THEN
        RAISE EXCEPTION 'terminal workflow Run requires a terminal epoch Job'
            USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE'
       AND OLD.current_job_id IS NOT NULL
       AND current_row.current_job_id IS DISTINCT FROM OLD.current_job_id
       AND EXISTS (
            SELECT 1
              FROM jobs AS previous_job
             WHERE previous_job.id = OLD.current_job_id
               AND previous_job.status IN (
                    'queued', 'leased', 'running', 'retry_wait'
               )
       ) THEN
        RAISE EXCEPTION 'workflow Run cannot detach an active current Job'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_workflow_runs_current_job
AFTER INSERT OR UPDATE OF status, current_job_id, execution_epoch, worker_profile_key
ON workflow_runs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION enforce_workflow_run_current_job();

CREATE OR REPLACE FUNCTION enforce_workflow_job_current_run()
RETURNS TRIGGER AS $$
DECLARE
    current_job jobs%ROWTYPE;
    current_run workflow_runs%ROWTYPE;
BEGIN
    SELECT * INTO current_job
      FROM jobs
     WHERE id = NEW.id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow Job disappeared before deferred validation'
            USING ERRCODE = '23503';
    END IF;
    IF current_job.job_type <> 'workflow_run' THEN
        RETURN NULL;
    END IF;
    SELECT * INTO current_run
      FROM workflow_runs
     WHERE id = current_job.workflow_run_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow Job Run authority is missing'
            USING ERRCODE = '23503';
    END IF;

    IF current_job.status IN ('queued', 'leased', 'running', 'retry_wait') THEN
        IF current_run.status NOT IN ('queued', 'running')
           OR current_run.current_job_id IS DISTINCT FROM current_job.id
           OR current_job.project_id IS DISTINCT FROM current_run.project_id
           OR current_job.owner_user_id IS DISTINCT FROM current_run.owner_user_id
           OR current_job.workflow_epoch IS DISTINCT FROM current_run.execution_epoch
           OR current_job.workflow_profile_key IS DISTINCT FROM current_run.worker_profile_key
           OR NOT EXISTS (
            SELECT 1
              FROM workflow_run_jobs AS mapping
             WHERE mapping.job_id = current_job.id
               AND mapping.workflow_run_id = current_run.id
               AND mapping.execution_epoch = current_run.execution_epoch
           ) THEN
            RAISE EXCEPTION 'active workflow Job does not match its current Run'
                USING ERRCODE = '23514';
        END IF;
    ELSIF current_run.current_job_id = current_job.id THEN
        RAISE EXCEPTION 'terminal workflow Job cannot remain current'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER trg_jobs_workflow_current_run
AFTER INSERT OR UPDATE OF
    job_type, status, project_id, owner_user_id, workflow_run_id,
    workflow_epoch, workflow_profile_key
ON jobs
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION enforce_workflow_job_current_run();

CREATE OR REPLACE FUNCTION enforce_workflow_credential_grant_transition()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'workflow Credential grants cannot be deleted'
            USING ERRCODE = '55000';
    END IF;
    IF ROW(
        NEW.id, NEW.project_id, NEW.workflow_version_id, NEW.slot_id,
        NEW.credential_scope, NEW.credential_id, NEW.credential_version_id,
        NEW.payload_schema_checksum, NEW.granted_by, NEW.created_at
    ) IS DISTINCT FROM ROW(
        OLD.id, OLD.project_id, OLD.workflow_version_id, OLD.slot_id,
        OLD.credential_scope, OLD.credential_id, OLD.credential_version_id,
        OLD.payload_schema_checksum, OLD.granted_by, OLD.created_at
    ) OR OLD.status <> 'active'
       OR NEW.status <> 'revoked'
       OR NEW.revision <> OLD.revision + 1
       OR NEW.revoked_by IS NULL
       OR NEW.revoked_at IS NULL THEN
        RAISE EXCEPTION 'invalid workflow Credential grant transition'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_workflow_definitions_updated_at
BEFORE UPDATE ON workflow_definitions
FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_workflow_drafts_updated_at
BEFORE UPDATE ON workflow_drafts
FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_workflow_runs_updated_at
BEFORE UPDATE ON workflow_runs
FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_workflow_runs_transition_guard
BEFORE UPDATE ON workflow_runs
FOR EACH ROW EXECUTE FUNCTION enforce_workflow_run_transition();

CREATE TRIGGER trg_workflow_code_leases_updated_at
BEFORE UPDATE ON workflow_code_sandbox_leases
FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_workflow_node_effects_updated_at
BEFORE UPDATE ON workflow_node_effects
FOR EACH ROW EXECUTE FUNCTION set_m7_updated_at();

CREATE TRIGGER trg_workflow_versions_immutable
BEFORE UPDATE OR DELETE ON workflow_versions
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_control_operations_immutable
BEFORE UPDATE OR DELETE ON workflow_control_operations
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_run_jobs_immutable
BEFORE UPDATE OR DELETE ON workflow_run_jobs
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_model_refs_immutable
BEFORE UPDATE OR DELETE ON workflow_version_model_refs
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_credential_slots_immutable
BEFORE UPDATE OR DELETE ON workflow_version_credential_slots
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_code_requirements_immutable
BEFORE UPDATE OR DELETE ON workflow_version_code_requirements
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_http_requirements_immutable
BEFORE UPDATE OR DELETE ON workflow_version_http_requirements
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_credential_grants_transition
BEFORE UPDATE OR DELETE ON workflow_credential_grants
FOR EACH ROW EXECUTE FUNCTION enforce_workflow_credential_grant_transition();

CREATE TRIGGER trg_workflow_run_snapshots_immutable
BEFORE UPDATE OR DELETE ON workflow_run_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_direct_workflow_snapshot_mutation();

CREATE TRIGGER trg_workflow_run_snapshots_profile_key
BEFORE INSERT ON workflow_run_snapshots
FOR EACH ROW EXECUTE FUNCTION populate_workflow_snapshot_profile_key();

CREATE TRIGGER trg_workflow_run_runtime_policy_snapshots_validate
BEFORE INSERT ON workflow_run_runtime_policy_snapshots
FOR EACH ROW EXECUTE FUNCTION validate_workflow_run_runtime_policy_snapshot();

CREATE TRIGGER trg_workflow_run_runtime_policy_snapshots_immutable
BEFORE UPDATE OR DELETE ON workflow_run_runtime_policy_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_direct_workflow_snapshot_mutation();

CREATE TRIGGER trg_workflow_run_model_snapshots_validate
BEFORE INSERT ON workflow_run_model_snapshots
FOR EACH ROW EXECUTE FUNCTION enforce_run_model_snapshot_credential_closure();

CREATE TRIGGER trg_workflow_run_model_snapshots_immutable
BEFORE UPDATE OR DELETE ON workflow_run_model_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_direct_workflow_snapshot_mutation();

CREATE TRIGGER trg_workflow_run_code_snapshots_immutable
BEFORE UPDATE OR DELETE ON workflow_run_code_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_direct_workflow_snapshot_mutation();

CREATE TRIGGER trg_workflow_run_http_snapshots_immutable
BEFORE UPDATE OR DELETE ON workflow_run_http_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_direct_workflow_snapshot_mutation();

CREATE OR REPLACE FUNCTION enforce_workflow_run_event_invariants()
RETURNS TRIGGER AS $$
DECLARE
    expected_seq BIGINT;
    event_is_terminal BOOLEAN;
BEGIN
    PERFORM 1
      FROM workflow_runs
     WHERE id = NEW.workflow_run_id
       AND project_id = NEW.project_id
       AND owner_user_id = NEW.owner_user_id
       AND workflow_version_id = NEW.workflow_version_id
     FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow event Run authority is missing'
            USING ERRCODE = '23503';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM workflow_run_event_invariants AS invariants
         WHERE invariants.workflow_run_id = NEW.workflow_run_id
           AND invariants.is_terminal
    ) THEN
        RAISE EXCEPTION 'workflow run already has a terminal event'
            USING ERRCODE = '23514';
    END IF;

    SELECT COALESCE(max(seq), 0) + 1
      INTO expected_seq
      FROM workflow_run_event_invariants
     WHERE workflow_run_id = NEW.workflow_run_id;
    IF NEW.seq <> expected_seq THEN
        RAISE EXCEPTION 'workflow event sequence is not contiguous'
            USING ERRCODE = '23514';
    END IF;

    IF NEW.node_id IS NOT NULL AND EXISTS (
        SELECT 1
          FROM workflow_run_event_invariants
         WHERE workflow_run_id = NEW.workflow_run_id
           AND node_id = NEW.node_id
           AND activation_id = NEW.activation_id
           AND scope_path_hash = NEW.scope_path_hash
           AND iteration_path = NEW.iteration_path
           AND attempt > NEW.attempt
    ) THEN
        RAISE EXCEPTION 'workflow activation attempt cannot move backward'
            USING ERRCODE = '23514';
    END IF;

    event_is_terminal := NEW.event_type IN (
        'workflow.run.completed',
        'workflow.run.failed',
        'workflow.run.cancelled',
        'workflow.run.side_effect_unknown'
    );
    INSERT INTO workflow_run_event_invariants (
        id, occurred_at, project_id, owner_user_id, workflow_run_id,
        workflow_version_id, seq, is_terminal, node_id, activation_id,
        scope_path_hash, iteration_path, attempt
    ) VALUES (
        NEW.id, NEW.occurred_at, NEW.project_id, NEW.owner_user_id,
        NEW.workflow_run_id, NEW.workflow_version_id, NEW.seq, event_is_terminal,
        NEW.node_id, NEW.activation_id, NEW.scope_path_hash,
        NEW.iteration_path, NEW.attempt
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_workflow_run_events_invariants
BEFORE INSERT ON workflow_run_events
FOR EACH ROW EXECUTE FUNCTION enforce_workflow_run_event_invariants();

CREATE TRIGGER trg_workflow_run_events_append_only
BEFORE UPDATE OR DELETE ON workflow_run_events
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE OR REPLACE FUNCTION ensure_workflow_run_events_month_partition(
    target_at TIMESTAMP WITH TIME ZONE
)
RETURNS VOID AS $$
DECLARE
    month_start TIMESTAMP WITH TIME ZONE;
    month_end TIMESTAMP WITH TIME ZONE;
    partition_name TEXT;
    retained TIMESTAMP WITH TIME ZONE;
BEGIN
    IF target_at IS NULL THEN
        RAISE EXCEPTION 'workflow event partition target is required'
            USING ERRCODE = '22023';
    END IF;
    month_start := date_trunc('month', target_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    month_end := month_start + INTERVAL '1 month';
    partition_name := 'workflow_run_events_' || to_char(month_start AT TIME ZONE 'UTC', 'YYYYMM');

    SELECT retained_from INTO retained
      FROM workflow_run_event_partition_state
     WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow event partition state is missing'
            USING ERRCODE = '55000';
    END IF;
    IF retained IS NOT NULL AND month_start < retained THEN
        RAISE EXCEPTION 'workflow event month is below the retention watermark'
            USING ERRCODE = '55000';
    END IF;
    IF to_regclass(partition_name) IS NOT NULL THEN
        RETURN;
    END IF;

    LOCK TABLE workflow_run_events IN SHARE UPDATE EXCLUSIVE MODE;
    SELECT retained_from INTO retained
      FROM workflow_run_event_partition_state
     WHERE singleton
     FOR UPDATE;
    IF retained IS NOT NULL AND month_start < retained THEN
        RAISE EXCEPTION 'workflow event month is below the retention watermark'
            USING ERRCODE = '55000';
    END IF;
    IF to_regclass(partition_name) IS NULL THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF workflow_run_events FOR VALUES FROM (%L) TO (%L)',
            partition_name, month_start, month_end
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION drop_workflow_run_event_partitions_before(
    cutoff_at TIMESTAMP WITH TIME ZONE
)
RETURNS INTEGER AS $$
DECLARE
    cutoff_month TIMESTAMP WITH TIME ZONE;
    child RECORD;
    child_month TIMESTAMP WITH TIME ZONE;
    dropped INTEGER := 0;
BEGIN
    IF cutoff_at IS NULL THEN
        RAISE EXCEPTION 'workflow event retention cutoff must be a non-future UTC month boundary'
            USING ERRCODE = '22023';
    END IF;
    cutoff_month := date_trunc('month', cutoff_at AT TIME ZONE 'UTC') AT TIME ZONE 'UTC';
    IF cutoff_at <> cutoff_month OR cutoff_month > now() THEN
        RAISE EXCEPTION 'workflow event retention cutoff must be a non-future UTC month boundary'
            USING ERRCODE = '22023';
    END IF;

    LOCK TABLE workflow_run_events IN ACCESS EXCLUSIVE MODE;
    UPDATE workflow_run_event_partition_state
       SET retained_from = GREATEST(COALESCE(retained_from, cutoff_month), cutoff_month),
           updated_at = now()
     WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'workflow event partition state is missing'
            USING ERRCODE = '55000';
    END IF;

    DELETE FROM workflow_run_event_invariants WHERE occurred_at < cutoff_month;
    FOR child IN
        SELECT child.relname AS name
          FROM pg_inherits
          JOIN pg_class parent ON parent.oid = inhparent
          JOIN pg_class child ON child.oid = inhrelid
         WHERE parent.oid = 'workflow_run_events'::regclass
    LOOP
        IF child.name ~ '^workflow_run_events_[0-9]{6}$' THEN
            child_month := to_date(
                substring(child.name from '([0-9]{6})$'),
                'YYYYMM'
            )::timestamp AT TIME ZONE 'UTC';
            IF child_month < cutoff_month THEN
                EXECUTE format('DROP TABLE %I', child.name);
                dropped := dropped + 1;
            END IF;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$$ LANGUAGE plpgsql;

SELECT ensure_workflow_run_events_month_partition(now());
SELECT ensure_workflow_run_events_month_partition(now() + INTERVAL '1 month');

-- END FULL_SCHEMA_V10_WORKFLOWS

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

INSERT INTO system_runtime_policy_catalog_state (id, revision) VALUES (1, 1);

INSERT INTO alembic_version (version_num) VALUES ('full_schema_v12');

COMMIT;
