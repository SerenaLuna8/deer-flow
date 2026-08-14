from __future__ import annotations

import ast
import inspect
import textwrap

from app.gateway.deps import gateway_platform_runtime
from app.reliability.run_execution.executor import RunAgentPrivateExecutor
from app.worker.app import run_worker


def _call_keywords(owner: object, function_name: str) -> set[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(owner)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name) and callee.id == function_name:
            return {keyword.arg for keyword in node.keywords if keyword.arg is not None}
    raise AssertionError(f"{function_name} is not composed by run_worker")


def test_worker_routes_execution_approval_ttl_to_job_handler() -> None:
    executor_keywords = _call_keywords(run_worker, "RunAgentPrivateExecutor")
    handler_keywords = _call_keywords(run_worker, "PrivateRunJobHandler")

    assert "execution_approval_ttl_seconds" not in executor_keywords
    assert "execution_approval_ttl_seconds" in handler_keywords


def test_worker_and_gateway_compose_the_provider_policy_snapshot() -> None:
    port_keywords = _call_keywords(
        RunAgentPrivateExecutor._execute_with_trace,
        "WorkerHostExecutionApprovalPort",
    )
    service_keywords = _call_keywords(
        gateway_platform_runtime,
        "ExecutionApprovalService",
    )

    assert "provider_policy" in port_keywords
    assert "execution_domain" in port_keywords
    assert "provider_policy" in service_keywords
    assert "quota" in service_keywords
    assert "run_audit" in service_keywords
