"""Resolve one admitted Run policy before crossing into Harness."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.private_work.workload_profile import (
    EffectiveRunWorkloadProfile,
    effective_run_workload_profile_from_kwargs,
)
from app.system_runtime_settings.app_config_projection import (
    project_agent_runtime_app_config_policy,
)
from app.system_runtime_settings.models import (
    MaterializedAgentRuntimePolicy,
)
from deerflow.agents.middlewares.tool_call_control import (
    RepeatedCallPolicy,
    ResolvedGraphToolCallControlProfile,
    ResolvedToolCallControlPolicy,
)


@dataclass(frozen=True, slots=True)
class ResolvedRunToolCallControlPolicy:
    """One Run's selected policies and legacy config projection."""

    graph_profile: ResolvedGraphToolCallControlProfile
    max_concurrent_subagents: int
    max_total_subagents: int
    app_config_policy: Mapping[str, object]

    @property
    def workload_profile(self) -> EffectiveRunWorkloadProfile:
        return EffectiveRunWorkloadProfile(
            name=self.graph_profile.workload_profile,
        )

    @property
    def lead(self) -> ResolvedToolCallControlPolicy:
        return self.graph_profile.lead

    @property
    def subagent(self) -> ResolvedToolCallControlPolicy:
        return self.graph_profile.subagent


def resolve_run_tool_call_control_policy(
    materialized: MaterializedAgentRuntimePolicy,
    run_kwargs: Mapping[str, object],
) -> ResolvedRunToolCallControlPolicy:
    if type(materialized) is not MaterializedAgentRuntimePolicy:
        raise TypeError("MaterializedAgentRuntimePolicy is required")
    workload = effective_run_workload_profile_from_kwargs(
        run_kwargs,
        policy_schema_version=materialized.schema_version,
    )
    value = materialized.value
    identical_calls = value.loop_detection.identical_calls
    repeated = RepeatedCallPolicy(
        warn_threshold=identical_calls.warn_threshold,
        hard_limit=identical_calls.hard_limit,
        window_size=identical_calls.window_size,
        enabled=value.loop_detection.enabled,
    )

    max_total_subagents = getattr(
        value.subagents.max_total_per_run_by_workload,
        workload.name,
    )
    limits = value.internal_tool_call_limits
    lead = ResolvedToolCallControlPolicy(
        repeated_calls=repeated,
        internal_tool_call_limit=limits.lead_per_run,
    )
    subagent = ResolvedToolCallControlPolicy(
        repeated_calls=repeated,
        internal_tool_call_limit=limits.subagent_per_task,
    )
    return ResolvedRunToolCallControlPolicy(
        graph_profile=ResolvedGraphToolCallControlProfile(
            workload_profile=workload.name,
            accounting_mode=("shared_run" if materialized.schema_version <= 5 else "lead_run_subagent_task"),
            lead=lead,
            subagent=subagent,
        ),
        max_concurrent_subagents=value.subagents.max_concurrent,
        max_total_subagents=max_total_subagents,
        app_config_policy=project_agent_runtime_app_config_policy(
            value,
            max_total_subagents=max_total_subagents,
        ),
    )


__all__ = [
    "ResolvedRunToolCallControlPolicy",
    "resolve_run_tool_call_control_policy",
]
