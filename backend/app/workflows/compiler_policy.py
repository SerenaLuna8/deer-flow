"""Explicit application-policy adapter for the authority-free Workflow compiler."""

from __future__ import annotations

from dataclasses import fields

from app.workflows.runtime_policy import WorkflowGraphLimitsV1
from deerflow.workflows.validation import WorkflowCompilationLimits


def workflow_compilation_limits_from_graph_policy(
    graph_limits: WorkflowGraphLimitsV1,
) -> WorkflowCompilationLimits:
    """Map the complete frozen graph policy into compiler limits fail-closed."""

    if type(graph_limits) is not WorkflowGraphLimitsV1:
        raise TypeError("graph_limits must be a validated WorkflowGraphLimitsV1")
    payload = graph_limits.model_dump(mode="python")
    compiler_fields = {item.name for item in fields(WorkflowCompilationLimits)}
    if set(payload) != compiler_fields:
        raise RuntimeError("Workflow graph policy and compiler limits are not isomorphic")
    return WorkflowCompilationLimits(**payload)


__all__ = ["workflow_compilation_limits_from_graph_policy"]
