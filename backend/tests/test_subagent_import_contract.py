"""Import contracts for the production Sub-Agent Task modules."""

from __future__ import annotations

import inspect
from types import ModuleType


def test_graph_runner_is_internal_and_legacy_lifecycle_api_is_deleted() -> None:
    """Only the lifecycle owns task scheduling, registry, and cancellation."""

    import deerflow.subagents as subagents_package
    import deerflow.subagents.executor as executor_module

    assert isinstance(executor_module, ModuleType)
    assert executor_module._SubagentGraphRunner.__module__ == ("deerflow.subagents.executor")
    assert not hasattr(subagents_package, "SubagentExecutor")
    assert not hasattr(subagents_package, "SubagentResult")
    assert not hasattr(executor_module._SubagentGraphRunner, "execute")
    assert not hasattr(executor_module._SubagentGraphRunner, "execute_async")
    for legacy_name in (
        "request_cancel_background_task",
        "get_background_task_result",
        "list_background_tasks",
        "cleanup_background_task",
    ):
        assert not hasattr(executor_module, legacy_name)


def test_graph_runner_accepts_one_delegated_context_instead_of_raw_authority() -> None:
    """Parent-to-child projection is the runner's only runtime-context input."""

    from deerflow.subagents.executor import _SubagentGraphRunner

    parameters = inspect.signature(_SubagentGraphRunner).parameters
    assert "delegated_context" in parameters
    assert {
        "app_config",
        "thread_id",
        "user_id",
        "user_role",
        "oauth_provider",
        "oauth_id",
        "run_id",
        "guardrail_attribution",
        "private_scope",
        "file_authority",
        "authorization_boundary",
        "authorization_checker",
        "run_read_only_mounts",
        "channel_user_id",
        "channel_identity_present",
        "deerflow_trace_id",
        "runtime_skills",
        "agent_prompt_bundle",
        "skill_scoped_secrets",
        "skill_secret_provider",
        "host_execution_approval_port",
        "host_execution_agent_path",
    }.isdisjoint(parameters)
