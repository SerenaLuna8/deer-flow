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
    InternalToolCallLimitsPolicy,
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
            schema_version=1,
            value=AgentRuntimePolicyValue(),
        ),
        kwargs,
    )
    expected_profile = default_graph_tool_call_control_profile(  # type: ignore[arg-type]
        workload_profile,
    )
    assert resolved.lead == expected_profile.lead
    assert resolved.subagent == expected_profile.subagent


def test_schema_v1_materialization_uses_independent_limits_for_each_role() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=1,
            value=AgentRuntimePolicyValue(
                internal_tool_call_limits=InternalToolCallLimitsPolicy(
                    lead_per_run=37,
                    subagent_per_task=11,
                ),
            ),
        ),
        _research_kwargs(),
    )

    assert resolved.workload_profile == EffectiveRunWorkloadProfile(name="research")
    assert resolved.lead.internal_tool_call_limit == 37
    assert resolved.subagent.internal_tool_call_limit == 11
    assert resolved.max_concurrent_subagents == 3
    assert resolved.max_total_subagents == 9
    assert "internal_tool_call_limits" not in resolved.app_config_policy
    assert resolved.app_config_policy["loop_detection"] == {"enabled": True}
    assert resolved.app_config_policy["subagents"] == {"max_total_per_run": 9}


@pytest.mark.parametrize("schema_version", [2, 3, 4, 5, 6, 7])
def test_retired_policy_schema_numbers_fail_closed(schema_version: int) -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        resolve_run_tool_call_control_policy(
            MaterializedAgentRuntimePolicy(
                schema_version=schema_version,
                value=AgentRuntimePolicyValue(),
            ),
            _research_kwargs(),
        )


def test_schema_v1_fails_closed_without_the_admitted_selector() -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        resolve_run_tool_call_control_policy(
            MaterializedAgentRuntimePolicy(
                schema_version=1,
                value=AgentRuntimePolicyValue(),
            ),
            {},
        )
