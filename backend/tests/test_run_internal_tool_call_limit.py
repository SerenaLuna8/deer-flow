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
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
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


def test_runtime_policy_exposes_only_one_internal_tool_call_limit() -> None:
    value = AgentRuntimePolicyValue()

    assert value.internal_tool_call_limit == 200
    assert "tool_call_budget" not in value.model_dump(mode="python")

    with pytest.raises(ValidationError):
        AgentRuntimePolicyValue.model_validate(
            {
                **value.model_dump(mode="python"),
                "tool_call_budget": {
                    "profiles": {},
                },
            }
        )


def test_run_policy_does_not_select_tool_limit_by_workload_or_role() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=5,
            value=AgentRuntimePolicyValue(internal_tool_call_limit=17),
        ),
        _research_kwargs(),
    )

    assert resolved.graph_profile.policy.internal_tool_call_limit == 17
    assert resolved.lead is resolved.subagent
    assert resolved.lead is resolved.graph_profile.policy
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
