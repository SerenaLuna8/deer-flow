from __future__ import annotations

import pytest

from app.private_work.workload_profile import (
    RUN_WORKLOAD_PROFILE_KWARG,
    EffectiveRunWorkloadProfile,
    RequestedRunWorkloadProfile,
    RunWorkloadProfileUnsupported,
    persisted_run_workload_profile,
)
from app.reliability.run_execution.tool_call_control_policy import (
    resolve_run_tool_call_control_policy,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    MaterializedAgentRuntimePolicy,
)
from deerflow.agents.middlewares.tool_call_control import (
    default_graph_tool_call_control_profile,
)


def _research_kwargs() -> dict[str, object]:
    return {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name="research"),
            EffectiveRunWorkloadProfile(name="research"),
        )
    }


@pytest.mark.parametrize("workload_profile", ["interactive", "research"])
def test_runtime_policy_defaults_match_harness_caller_defaults(
    workload_profile: str,
) -> None:
    kwargs = {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name=workload_profile),  # type: ignore[arg-type]
            EffectiveRunWorkloadProfile(name=workload_profile),  # type: ignore[arg-type]
        )
    }
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=5,
            value=AgentRuntimePolicyValue(),
        ),
        kwargs,
    )
    expected_profile = default_graph_tool_call_control_profile(  # type: ignore[arg-type]
        workload_profile,
    )
    actual = resolved.graph_profile.policy
    expected = expected_profile.policy

    assert actual.repeated_calls == expected.repeated_calls
    assert actual.internal_tool_call_limit == expected.internal_tool_call_limit
    assert resolved.lead is resolved.subagent


def test_v5_materialization_uses_one_limit_for_both_roles() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=5,
            value=AgentRuntimePolicyValue(internal_tool_call_limit=37),
        ),
        _research_kwargs(),
    )

    assert resolved.workload_profile == EffectiveRunWorkloadProfile(name="research")
    assert resolved.lead is resolved.subagent
    assert resolved.lead.internal_tool_call_limit == 37
    assert resolved.max_concurrent_subagents == 3
    assert resolved.max_total_subagents == 9
    assert "internal_tool_call_limit" not in resolved.app_config_policy
    assert resolved.app_config_policy["loop_detection"] == {"enabled": True}
    assert resolved.app_config_policy["subagents"] == {"max_total_per_run": 9}


def test_legacy_materialization_is_interactive_without_a_frozen_selector() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=3,
            value=AgentRuntimePolicyValue(),
        ),
        {},
    )

    assert resolved.workload_profile.name == "interactive"
    assert resolved.lead.internal_tool_call_limit == 200
    assert resolved.max_total_subagents == 6


def test_v4_materialization_fails_closed_without_the_admitted_selector() -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        resolve_run_tool_call_control_policy(
            MaterializedAgentRuntimePolicy(
                schema_version=4,
                value=AgentRuntimePolicyValue(),
            ),
            {},
        )
