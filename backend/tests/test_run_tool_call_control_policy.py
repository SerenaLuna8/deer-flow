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
from deerflow.vision.dispatch import MAX_VISION_CALLS_PER_RUN


def _research_kwargs() -> dict[str, object]:
    return {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name="research"),
            EffectiveRunWorkloadProfile(name="research"),
        )
    }


@pytest.mark.parametrize("workload_profile", ["interactive", "research"])
@pytest.mark.parametrize("role", ["lead", "subagent"])
def test_runtime_policy_defaults_match_harness_caller_defaults(
    workload_profile: str,
    role: str,
) -> None:
    kwargs = {
        RUN_WORKLOAD_PROFILE_KWARG: persisted_run_workload_profile(
            RequestedRunWorkloadProfile(name=workload_profile),  # type: ignore[arg-type]
            EffectiveRunWorkloadProfile(name=workload_profile),  # type: ignore[arg-type]
        )
    }
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=4,
            value=AgentRuntimePolicyValue(),
        ),
        kwargs,
    )
    expected_profile = default_graph_tool_call_control_profile(  # type: ignore[arg-type]
        workload_profile,
    )
    actual = getattr(resolved, role)
    expected = getattr(expected_profile, role)

    assert actual.repeated_calls == expected.repeated_calls
    assert actual.tool_budget.default == expected.tool_budget.default
    for tool_name in (
        "web_search",
        "web_fetch",
        "recall_memory",
        "inspect_image",
        "write_file",
    ):
        assert actual.tool_budget.limit_for(tool_name) == (expected.tool_budget.limit_for(tool_name))


def test_v4_materialization_selects_one_profile_and_role_before_harness() -> None:
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=4,
            value=AgentRuntimePolicyValue(),
        ),
        _research_kwargs(),
    )

    assert resolved.workload_profile == EffectiveRunWorkloadProfile(name="research")
    assert resolved.lead.tool_budget.limit_for("web_search").warn_threshold == 20
    assert resolved.lead.tool_budget.limit_for("web_search").hard_limit == 30
    assert resolved.subagent.tool_budget.limit_for("web_search").warn_threshold == 12
    assert resolved.subagent.tool_budget.limit_for("web_search").hard_limit == 20
    assert resolved.max_concurrent_subagents == 3
    assert resolved.max_total_subagents == 9
    assert "tool_call_budget" not in resolved.app_config_policy
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
    assert resolved.lead.tool_budget.limit_for("web_search").hard_limit == 10
    assert resolved.max_total_subagents == 6


def test_materialization_clamps_inspect_image_to_the_technical_cap() -> None:
    runtime_policy = AgentRuntimePolicyValue.model_validate(
        {
            **AgentRuntimePolicyValue().model_dump(mode="python"),
            "tool_call_budget": {
                "profiles": {
                    profile_name: {
                        role: {
                            "default": {"warn": 30, "hard_limit": 50},
                            "tools": {
                                "inspect_image": {
                                    "warn": 12,
                                    "hard_limit": 20,
                                }
                            },
                        }
                        for role in ("lead", "subagent")
                    }
                    for profile_name in ("interactive", "research")
                }
            },
        }
    )
    resolved = resolve_run_tool_call_control_policy(
        MaterializedAgentRuntimePolicy(
            schema_version=4,
            value=runtime_policy,
        ),
        _research_kwargs(),
    )

    inspect_limit = resolved.lead.tool_budget.limit_for("inspect_image")
    assert inspect_limit.warn_threshold == MAX_VISION_CALLS_PER_RUN
    assert inspect_limit.hard_limit == MAX_VISION_CALLS_PER_RUN


def test_v4_materialization_fails_closed_without_the_admitted_selector() -> None:
    with pytest.raises(RunWorkloadProfileUnsupported):
        resolve_run_tool_call_control_policy(
            MaterializedAgentRuntimePolicy(
                schema_version=4,
                value=AgentRuntimePolicyValue(),
            ),
            {},
        )
