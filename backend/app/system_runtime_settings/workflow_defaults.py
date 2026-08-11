"""Frozen, secret-free default for the PostgreSQL Workflow runtime policy."""

from __future__ import annotations

from app.workflows.runtime_policy import (
    WorkflowRuntimePolicyV1,
    workflow_runtime_policy_checksum,
)

WORKFLOW_RUNTIME_DEFAULT_POLICY_CHECKSUM = "4ca136425002aa3a3a2426b4687f2e8091b6e4c23bf1d4db88b952730e1431e4"


def default_workflow_runtime_policy() -> WorkflowRuntimePolicyV1:
    """Build the complete disabled v1 policy without consulting files or env."""

    policy = WorkflowRuntimePolicyV1.model_validate(
        {
            "schema_version": 1,
            "enabled": False,
            "admission_enabled": False,
            "catalog": {
                "allowed_type_versions": [
                    {"type": "start", "versions": [1]},
                    {"type": "llm", "versions": [1]},
                    {"type": "condition", "versions": [1]},
                    {"type": "transform", "versions": [1]},
                    {"type": "variable_aggregate", "versions": [1]},
                    {"type": "loop", "versions": [1]},
                    {"type": "http_request", "versions": [1]},
                    {"type": "python_code", "versions": [1]},
                    {"type": "end", "versions": [1]},
                ]
            },
            "graph_limits": {
                "max_nodes": 100,
                "max_edges": 200,
                "max_depth": 20,
                "max_total_steps": 1_000,
                "max_recursion_depth": 2_000,
                "max_parallelism": 8,
                "max_fan_out": 16,
                "max_loops": 8,
                "max_loop_body_nodes": 32,
                "max_loop_body_edges": 64,
                "max_loop_iterations": 100,
                "max_total_iterations": 500,
                "max_total_activations": 2_000,
                "max_aggregate_groups": 32,
                "max_aggregate_candidates": 64,
            },
            "execution_limits": {
                "max_node_timeout_ms": 30_000,
                "max_run_timeout_ms": 300_000,
                "max_human_wait_timeout_ms": 86_400_000,
                "max_input_bytes": 1_048_576,
                "max_state_bytes": 4_194_304,
                "max_output_bytes": 524_288,
                "max_event_preview_bytes": 65_536,
                "max_retry_attempts": 3,
                "retry_backoff_initial_ms": 100,
                "retry_backoff_max_ms": 5_000,
                "max_llm_tokens_per_call": 32_768,
                "max_llm_calls": 100,
                "max_code_activations": 0,
                "max_code_duration_ms": 300_000,
                "max_http_calls": 0,
                "max_http_request_bytes": 1_048_576,
                "max_http_response_bytes": 1_048_576,
                "max_http_total_bytes": 8_388_608,
                "max_mcp_calls": 0,
                "max_files": 0,
                "max_file_bytes": 0,
            },
            "code": {
                "enabled": False,
                "provider_adapter_key": None,
                "execution_profile_id": None,
                "runtime_contract": "python3.12-v1",
                "image_digest": None,
                "isolation_profile": None,
                "network_policy": "deny_all",
                "dns_policy": "deny_all",
                "hard_limits": {
                    "cpu_millicores": 1_000,
                    "memory_bytes": 268_435_456,
                    "max_pids": 32,
                    "tmpfs_bytes": 67_108_864,
                    "wall_timeout_ms": 30_000,
                    "max_source_bytes": 65_536,
                    "max_stdout_bytes": 65_536,
                    "max_stderr_bytes": 65_536,
                    "max_result_bytes": 524_288,
                    "max_total_log_bytes": 262_144,
                    "read_only_root_filesystem": True,
                    "allow_mounts": False,
                    "allow_host_environment": False,
                    "allow_credentials": False,
                    "allow_runtime_sockets": False,
                },
            },
            "http": {
                "enabled": False,
                "write_enabled": False,
                "egress_profile_id": None,
                "egress_profile_digest": None,
                "endpoint_policies": [],
                "injection_profiles": [],
                "transport": {
                    "connect_timeout_ms": 5_000,
                    "read_timeout_ms": 30_000,
                    "write_timeout_ms": 30_000,
                    "total_timeout_ms": 60_000,
                    "max_headers": 64,
                    "max_header_name_bytes": 128,
                    "max_header_value_bytes": 4_096,
                    "max_request_bytes": 1_048_576,
                    "max_wire_response_bytes": 1_048_576,
                    "max_decompressed_response_bytes": 2_097_152,
                    "max_json_depth": 32,
                    "max_retries": 3,
                    "retry_backoff_initial_ms": 100,
                    "retry_backoff_max_ms": 5_000,
                    "max_retry_after_ms": 30_000,
                    "tls_verify": True,
                    "follow_redirects": False,
                    "cookie_jar": False,
                    "trust_env": False,
                },
            },
            "retention": {
                "terminal_run_days": 30,
                "event_days": 30,
                "http_effect_days": 30,
                "destroyed_code_lease_days": 7,
            },
            "future": {
                "human_input_enabled": False,
                "agent_enabled": False,
                "tool_enabled": False,
                "mcp_enabled": False,
                "iteration_enabled": False,
                "subworkflow_enabled": False,
                "automation_enabled": False,
                "chatflow_enabled": False,
            },
        }
    )
    if workflow_runtime_policy_checksum(policy) != WORKFLOW_RUNTIME_DEFAULT_POLICY_CHECKSUM:
        raise RuntimeError("WORKFLOW_RUNTIME_DEFAULT_POLICY_DRIFT")
    return policy


__all__ = [
    "WORKFLOW_RUNTIME_DEFAULT_POLICY_CHECKSUM",
    "default_workflow_runtime_policy",
]
