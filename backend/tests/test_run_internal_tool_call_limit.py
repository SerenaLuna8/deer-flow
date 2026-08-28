from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.private_work.workload_profile import (
    RUN_WORKLOAD_PROFILE_KWARG,
    EffectiveRunWorkloadProfile,
    RequestedRunWorkloadProfile,
    persisted_run_workload_profile,
)
from app.reliability.run_execution.tool_call_control_policy import (
    resolve_run_tool_call_control_policy,
)
from app.system_runtime_settings.app_config_projection import (
    project_agent_runtime_app_config_policy,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    InternalToolCallLimitsPolicy,
    MaterializedAgentRuntimePolicy,
)
from deerflow.agents.middlewares.tool_call_control import (
    RunToolCallLimitAuthority,
)


def _research_kwargs() -> dict[str, object]:
    return {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name="research"),
            EffectiveRunWorkloadProfile(name="research"),
        )
    }


def test_internal_tool_call_limits_do_not_leak_into_legacy_app_config() -> None:
    projected = project_agent_runtime_app_config_policy(
        AgentRuntimePolicyValue(),
        max_total_subagents=9,
    )

    assert "internal_tool_call_limits" not in projected


def test_runtime_policy_exposes_strict_role_scoped_internal_tool_call_limits() -> None:
    value = AgentRuntimePolicyValue()

    assert value.internal_tool_call_limits.model_dump(mode="json") == {
        "lead_per_run": 200,
        "subagent_per_task": 50,
    }
    assert "internal_tool_call_limit" not in value.model_dump(mode="python")
    assert "tool_call_budget" not in value.model_dump(mode="python")

    with pytest.raises(ValidationError):
        AgentRuntimePolicyValue.model_validate(
            {
                **value.model_dump(mode="python"),
                "internal_tool_call_limit": 200,
            }
        )

    with pytest.raises(ValidationError):
        AgentRuntimePolicyValue.model_validate(
            {
                **value.model_dump(mode="python"),
                "internal_tool_call_limits": 200,
            }
        )

    with pytest.raises(ValidationError):
        AgentRuntimePolicyValue.model_validate(
            {
                **value.model_dump(mode="python"),
                "internal_tool_call_limits": {
                    **value.internal_tool_call_limits.model_dump(mode="python"),
                    "shared_per_run": 200,
                },
            }
        )


def test_run_policy_resolves_independent_role_limits() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=1,
            value=AgentRuntimePolicyValue(
                internal_tool_call_limits=InternalToolCallLimitsPolicy(
                    lead_per_run=17,
                    subagent_per_task=7,
                ),
            ),
        ),
        _research_kwargs(),
    )

    assert resolved.lead.internal_tool_call_limit == 17
    assert resolved.subagent.internal_tool_call_limit == 7
    assert resolved.max_total_subagents == 9


def test_one_run_authority_is_shared_and_batch_admission_is_prefix_based() -> None:
    authority = RunToolCallLimitAuthority(hard_limit=3)

    lead = authority.reserve_batch(
        scope_id="run-1",
        proposal_receipt="lead-1",
        proposed=2,
        baseline=0,
    )
    subagent = authority.reserve_batch(
        scope_id="run-1",
        proposal_receipt="subagent-1",
        proposed=2,
        baseline=0,
    )

    assert (lead.count_before, lead.admitted, lead.rejected, lead.count_after) == (
        0,
        2,
        0,
        2,
    )
    assert (
        subagent.count_before,
        subagent.admitted,
        subagent.rejected,
        subagent.count_after,
    ) == (2, 1, 1, 3)
    assert subagent.admitted_indices == (0,)


def test_authority_replay_is_idempotent_and_another_run_starts_at_zero() -> None:
    authority = RunToolCallLimitAuthority(hard_limit=2)

    first = authority.reserve_batch(
        scope_id="run-1",
        proposal_receipt="proposal-1",
        proposed=2,
        baseline=0,
    )
    replay = authority.reserve_batch(
        scope_id="run-1",
        proposal_receipt="proposal-1",
        proposed=2,
        baseline=0,
    )
    another_run = authority.reserve_batch(
        scope_id="run-2",
        proposal_receipt="proposal-1",
        proposed=1,
        baseline=0,
    )

    assert replay == first
    assert another_run.count_before == 0
    assert another_run.count_after == 1
