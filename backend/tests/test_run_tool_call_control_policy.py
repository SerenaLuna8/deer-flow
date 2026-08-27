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
            schema_version=6,
            value=AgentRuntimePolicyValue(),
        ),
        kwargs,
    )
    expected_profile = default_graph_tool_call_control_profile(  # type: ignore[arg-type]
        workload_profile,
    )
    assert resolved.graph_profile.accounting_mode == "lead_run_subagent_task"
    assert resolved.lead == expected_profile.lead
    assert resolved.subagent == expected_profile.subagent


def test_v6_materialization_uses_independent_limits_for_each_role() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=6,
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
    assert resolved.graph_profile.accounting_mode == "lead_run_subagent_task"
    assert resolved.lead.internal_tool_call_limit == 37
    assert resolved.subagent.internal_tool_call_limit == 11
    assert resolved.max_concurrent_subagents == 3
    assert resolved.max_total_subagents == 9
    assert "internal_tool_call_limits" not in resolved.app_config_policy
    assert resolved.app_config_policy["loop_detection"] == {"enabled": True}
    assert resolved.app_config_policy["subagents"] == {"max_total_per_run": 9}


def test_legacy_materialization_uses_shared_accounting_without_a_frozen_selector() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=3,
            value=AgentRuntimePolicyValue(
                internal_tool_call_limits=InternalToolCallLimitsPolicy(
                    lead_per_run=200,
                    subagent_per_task=200,
                ),
            ),
        ),
        {},
    )

    assert resolved.workload_profile.name == "interactive"
    assert resolved.graph_profile.accounting_mode == "shared_run"
    assert resolved.lead == resolved.subagent
    assert resolved.lead.internal_tool_call_limit == 200
    assert resolved.max_total_subagents == 6


def test_v5_materialization_keeps_legacy_shared_run_accounting() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=5,
            value=AgentRuntimePolicyValue(
                internal_tool_call_limits=InternalToolCallLimitsPolicy(
                    lead_per_run=37,
                    subagent_per_task=37,
                ),
            ),
        ),
        _research_kwargs(),
    )

    assert resolved.graph_profile.accounting_mode == "shared_run"
    assert resolved.lead == resolved.subagent
    assert resolved.lead.internal_tool_call_limit == 37


def test_v4_materialization_fails_closed_without_the_admitted_selector() -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        resolve_run_tool_call_control_policy(
            MaterializedAgentRuntimePolicy(
                schema_version=4,
                value=AgentRuntimePolicyValue(),
            ),
            {},
        )
