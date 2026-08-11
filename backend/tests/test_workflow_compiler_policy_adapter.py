from __future__ import annotations

import json
from dataclasses import asdict, fields
from pathlib import Path

from app.workflows.compiler_policy import workflow_compilation_limits_from_graph_policy
from app.workflows.runtime_policy import WorkflowRuntimePolicyV1
from deerflow.workflows.validation import WorkflowCompilationLimits

_POLICY_FIXTURE = Path(__file__).resolve().parents[2] / "frontend/tests/fixtures/workflows/workflow-runtime-policy-v1.json"


def test_graph_policy_adapter_maps_every_field_exactly_once() -> None:
    fixture = json.loads(_POLICY_FIXTURE.read_text(encoding="utf-8"))
    policy = WorkflowRuntimePolicyV1.model_validate(fixture["policy"])
    graph_payload = policy.graph_limits.model_dump(mode="python")

    limits = workflow_compilation_limits_from_graph_policy(policy.graph_limits)

    assert {item.name for item in fields(WorkflowCompilationLimits)} == set(graph_payload)
    assert asdict(limits) == graph_payload
