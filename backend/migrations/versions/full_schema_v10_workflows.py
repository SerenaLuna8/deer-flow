# ruff: noqa: E501
"""Install the first production standalone Workflow schema.

This revision is explicit and frozen: it must not import live ORM/application
models.  Worker execution remains disabled until the later G32 boundary.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "full_schema_v10"
down_revision = "full_schema_v9"
branch_labels = None
depends_on = None

_WORKFLOW_RUNTIME_POLICY_VERSION_ID = "ddf23bac-aa2c-5d9a-aa41-f014a66914a0"
_WORKFLOW_RUNTIME_POLICY_CHECKSUM = "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"
_BOOTSTRAP_PRINCIPAL_ID = "00000000-0000-0000-0000-000000000008"
_WORKFLOW_RUNTIME_POLICY_JSON = r"""{"admission_enabled":false,"catalog":{"allowed_type_versions":[{"type":"start","versions":[1]},{"type":"llm","versions":[1]},{"type":"condition","versions":[1]},{"type":"transform","versions":[1]},{"type":"variable_aggregate","versions":[1]},{"type":"loop","versions":[1]},{"type":"http_request","versions":[1]},{"type":"python_code","versions":[1]},{"type":"end","versions":[1]}]},"code":{"dns_policy":"deny_all","enabled":false,"execution_profile_id":null,"hard_limits":{"allow_credentials":false,"allow_host_environment":false,"allow_mounts":false,"allow_runtime_sockets":false,"cpu_millicores":1000,"max_pids":32,"max_result_bytes":524288,"max_source_bytes":65536,"max_stderr_bytes":65536,"max_stdout_bytes":65536,"max_total_log_bytes":262144,"memory_bytes":268435456,"read_only_root_filesystem":true,"tmpfs_bytes":67108864,"wall_timeout_ms":30000},"image_digest":null,"isolation_profile":null,"network_policy":"deny_all","provider_adapter_key":null,"runtime_contract":"python3.12-v1"},"enabled":false,"execution_limits":{"max_code_activations":0,"max_code_duration_ms":300000,"max_event_preview_bytes":65536,"max_file_bytes":0,"max_files":0,"max_http_calls":0,"max_http_request_bytes":1048576,"max_http_response_bytes":1048576,"max_http_total_bytes":8388608,"max_human_wait_timeout_ms":86400000,"max_input_bytes":1048576,"max_llm_calls":100,"max_llm_tokens_per_call":32768,"max_mcp_calls":0,"max_node_timeout_ms":30000,"max_output_bytes":524288,"max_retry_attempts":3,"max_run_timeout_ms":300000,"max_state_bytes":4194304,"retry_backoff_initial_ms":100,"retry_backoff_max_ms":5000},"future":{"agent_enabled":false,"automation_enabled":false,"chatflow_enabled":false,"human_input_enabled":false,"iteration_enabled":false,"mcp_enabled":false,"subworkflow_enabled":false,"tool_enabled":false},"graph_limits":{"max_aggregate_candidates":64,"max_aggregate_groups":32,"max_depth":20,"max_edges":200,"max_fan_out":16,"max_loop_body_edges":64,"max_loop_body_nodes":32,"max_loop_iterations":100,"max_loops":8,"max_nodes":100,"max_parallelism":8,"max_recursion_depth":2000,"max_total_activations":2000,"max_total_iterations":500,"max_total_steps":1000},"http":{"egress_profile_digest":null,"egress_profile_id":null,"enabled":false,"endpoint_policies":[],"injection_profiles":[],"transport":{"connect_timeout_ms":5000,"cookie_jar":false,"follow_redirects":false,"max_decompressed_response_bytes":2097152,"max_header_name_bytes":128,"max_header_value_bytes":4096,"max_headers":64,"max_json_depth":32,"max_request_bytes":1048576,"max_retries":3,"max_retry_after_ms":30000,"max_wire_response_bytes":1048576,"read_timeout_ms":30000,"retry_backoff_initial_ms":100,"retry_backoff_max_ms":5000,"tls_verify":true,"total_timeout_ms":60000,"trust_env":false,"write_timeout_ms":30000},"write_enabled":false},"retention":{"destroyed_code_lease_days":7,"event_days":30,"http_effect_days":30,"terminal_run_days":30},"schema_version":1}"""

_WORKFLOW_SCHEMA_SQL = r"""
-- BEGIN FULL_SCHEMA_V10_WORKFLOWS
ALTER TABLE job_attempts
    ADD CONSTRAINT uq_job_attempts_job_number_worker
        UNIQUE (job_id, attempt_number, worker_id);

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
	CONSTRAINT ck_workflow_draft_grant_intents_slot CHECK (slot_id ~ '^[a-z][a-z0-9._-]{0,127}$'), 
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
	CONSTRAINT ck_workflow_version_credential_slots_slot CHECK (slot_id ~ '^[a-z][a-z0-9._-]{0,127}$'), 
	CONSTRAINT ck_workflow_version_credential_slots_purpose CHECK (purpose ~ '^[a-z][a-z0-9._-]{0,63}$'), 
	CONSTRAINT ck_workflow_version_credential_slots_schema_object CHECK (jsonb_typeof(payload_schema_json) = 'object'), 
	CONSTRAINT ck_workflow_version_credential_slots_checksum CHECK (payload_schema_checksum ~ '^[0-9a-f]{64}$'), 
	CONSTRAINT ck_workflow_version_credential_slots_required CHECK (required), 
	CONSTRAINT uq_workflow_version_credential_slots_exact UNIQUE (workflow_version_id, slot_id, payload_schema_checksum), 
	CONSTRAINT fk_workflow_version_credential_slots_version FOREIGN KEY(workflow_version_id, project_id) REFERENCES workflow_versions (id, project_id) ON DELETE RESTRICT
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

CREATE TRIGGER trg_workflow_run_jobs_immutable
BEFORE UPDATE OR DELETE ON workflow_run_jobs
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_model_refs_immutable
BEFORE UPDATE OR DELETE ON workflow_version_model_refs
FOR EACH ROW EXECUTE FUNCTION reject_m7_append_only_mutation();

CREATE TRIGGER trg_workflow_version_credential_slots_immutable
BEFORE UPDATE OR DELETE ON workflow_version_credential_slots
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

"""

_RUNTIME_POLICY_SEED_SQL = rf"""
SET CONSTRAINTS ALL DEFERRED;
DO $$
DECLARE
    policy_count BIGINT;
    version_count BIGINT;
    valid_pointer_count BIGINT;
    bootstrap_principal_count BIGINT;
    bootstrap_membership_count BIGINT;
BEGIN
    SELECT count(*) INTO policy_count FROM system_runtime_policies;
    SELECT count(*) INTO version_count FROM system_runtime_policy_versions;

    IF policy_count = 0 AND version_count = 0 THEN
        -- Schema-only setup/parity databases are completed by the normal
        -- post-schema system-runtime bootstrap.
        NULL;
    ELSIF policy_count = 4 AND version_count >= 4 THEN
        IF (
            SELECT array_agg(section ORDER BY section)
              FROM system_runtime_policies
        ) IS DISTINCT FROM ARRAY[
            'agent_runtime', 'auth', 'memory_document', 'quotas'
        ]::varchar[] THEN
            RAISE EXCEPTION 'runtime policy catalog section set is invalid before v10';
        END IF;

        SELECT count(*)
          INTO valid_pointer_count
          FROM system_runtime_policies AS policy
          JOIN system_runtime_policy_versions AS version
            ON version.section = policy.section
           AND version.id = policy.current_version_id
           AND version.version_number = policy.revision
         WHERE policy.revision >= 1
           AND version.schema_version >= 1
           AND version.payload_checksum ~ '^[0-9a-f]{{64}}$';
        IF valid_pointer_count <> 4 THEN
            RAISE EXCEPTION 'runtime policy catalog pointer is invalid before v10';
        END IF;

        SELECT count(*)
          INTO bootstrap_principal_count
          FROM users
         WHERE id = '{_BOOTSTRAP_PRINCIPAL_ID}'
           AND email = 'builtin-models@deerflow.invalid'
           AND password_hash IS NULL
           AND system_role = 'user'
           AND oauth_provider IS NULL
           AND oauth_id IS NULL
           AND needs_setup = false
           AND token_version = 0;
        SELECT count(*)
          INTO bootstrap_membership_count
          FROM project_memberships
         WHERE user_id = '{_BOOTSTRAP_PRINCIPAL_ID}';
        IF bootstrap_principal_count <> 1 OR bootstrap_membership_count <> 0 THEN
            RAISE EXCEPTION 'runtime policy bootstrap principal is invalid before v10';
        END IF;
        IF EXISTS (
            SELECT 1 FROM system_runtime_policies
             WHERE section = 'workflow_runtime'
        ) OR EXISTS (
            SELECT 1 FROM system_runtime_policy_versions
             WHERE id = '{_WORKFLOW_RUNTIME_POLICY_VERSION_ID}'::uuid
                OR section = 'workflow_runtime'
        ) THEN
            RAISE EXCEPTION 'workflow runtime policy already exists before v10';
        END IF;

        INSERT INTO system_runtime_policies (
            section, current_version_id, revision, updated_by_user_id
        ) VALUES (
            'workflow_runtime',
            '{_WORKFLOW_RUNTIME_POLICY_VERSION_ID}'::uuid,
            1,
            '{_BOOTSTRAP_PRINCIPAL_ID}'
        );
        INSERT INTO system_runtime_policy_versions (
            id, section, version_number, schema_version, value,
            payload_checksum, supersedes_version_id, created_by_user_id
        ) VALUES (
            '{_WORKFLOW_RUNTIME_POLICY_VERSION_ID}'::uuid,
            'workflow_runtime',
            1,
            1,
            '{_WORKFLOW_RUNTIME_POLICY_JSON}'::jsonb,
            '{_WORKFLOW_RUNTIME_POLICY_CHECKSUM}',
            NULL,
            '{_BOOTSTRAP_PRINCIPAL_ID}'
        );
        UPDATE system_runtime_policy_catalog_state
           SET revision = revision + 1,
               updated_by_user_id = '{_BOOTSTRAP_PRINCIPAL_ID}',
               updated_at = now()
         WHERE id = 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'runtime policy catalog state is missing before v10';
        END IF;
    ELSE
        RAISE EXCEPTION 'runtime policy catalog is incomplete before v10';
    END IF;
END;
$$;
"""


def _execute_frozen_sql(payload: str) -> None:
    if op.get_context().as_sql:
        # TextClause otherwise treats JSON ``:false``/``:null`` fragments as
        # bind names.  Escaped colons render back to literal colons offline.
        op.execute(sa.text(payload.replace(":", r"\:")))
    else:
        # psycopg scans literal percent signs as DBAPI placeholders.  ``%%``
        # is the DBAPI literal escape, so PostgreSQL still receives the exact
        # regexes and ``format('%I', ...)`` templates from the frozen batch.
        op.get_bind().exec_driver_sql(payload.replace("%", "%%"))


def upgrade() -> None:
    _execute_frozen_sql(_WORKFLOW_SCHEMA_SQL)
    _execute_frozen_sql(_RUNTIME_POLICY_SEED_SQL)


def downgrade() -> None:
    raise RuntimeError(
        "ActWeave migrations do not support downgrade; restore from the pre-upgrade backup instead",
    )
