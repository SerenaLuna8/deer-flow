"""Import, order, and copy-semantics contracts for middleware assembly."""

from __future__ import annotations

import copy
import importlib
import subprocess
import sys

import pytest
from langchain.agents.middleware import AgentMiddleware

_BUILDER_EXPORTS = (
    "build_sandbox_infrastructure",
    "assemble_agent_middlewares",
    "build_runtime_middlewares",
    "build_lead_runtime_middlewares",
    "build_subagent_runtime_middlewares",
)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            "deerflow.agents.middlewares.assembly",
            "deerflow.agents.middlewares.tool_error_handling_middleware",
        ),
        (
            "deerflow.agents.middlewares.tool_error_handling_middleware",
            "deerflow.agents.middlewares.assembly",
        ),
    ],
)
def test_assembly_supports_cold_imports_and_legacy_reexports(
    first: str,
    second: str,
) -> None:
    export_names = repr(_BUILDER_EXPORTS)
    code = (
        "import importlib; "
        f"first = importlib.import_module({first!r}); "
        f"second = importlib.import_module({second!r}); "
        "assembly = importlib.import_module('deerflow.agents.middlewares.assembly'); "
        "legacy = importlib.import_module('deerflow.agents.middlewares.tool_error_handling_middleware'); "
        f"names = {export_names}; "
        "assert all(getattr(legacy, name) is getattr(assembly, name) for name in names); "
        "assert legacy.ToolErrorHandlingMiddleware.__module__.endswith('tool_error_handling_middleware')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_production_factories_import_builders_from_assembly() -> None:
    assembly = importlib.import_module(
        "deerflow.agents.middlewares.assembly",
    )
    factory = importlib.import_module("deerflow.agents.factory")
    lead = importlib.import_module("deerflow.agents.lead_agent.agent")

    assert factory.build_runtime_middlewares is assembly.build_runtime_middlewares
    assert factory.assemble_agent_middlewares is assembly.assemble_agent_middlewares
    assert lead.build_lead_runtime_middlewares is assembly.build_lead_runtime_middlewares
    assert lead.assemble_agent_middlewares is assembly.assemble_agent_middlewares


def test_declarative_relative_order_validator_rejects_reversal() -> None:
    assembly = importlib.import_module(
        "deerflow.agents.middlewares.assembly",
    )
    outer = AgentMiddleware()
    inner = AgentMiddleware()
    invariant = assembly._MiddlewareOrderInvariant(
        name="probe order",
        registration_order=(outer, inner),
        reverse_hook="after_model",
    )

    with pytest.raises(RuntimeError, match="probe order"):
        assembly._validate_middleware_invariants(
            [inner, outer],
            (invariant,),
        )


def test_declarative_validator_ignores_disabled_phases_and_absolute_positions() -> None:
    assembly = importlib.import_module(
        "deerflow.agents.middlewares.assembly",
    )
    first = AgentMiddleware()
    second = AgentMiddleware()
    invariant = assembly._MiddlewareOrderInvariant(
        name="optional relative order",
        registration_order=(None, first, None, second),
    )

    assembly._validate_middleware_invariants(
        [AgentMiddleware(), first, AgentMiddleware(), second, AgentMiddleware()],
        (invariant,),
    )


def test_runtime_builder_declares_the_optional_private_boundary_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembly = importlib.import_module(
        "deerflow.agents.middlewares.assembly",
    )
    from deerflow.config.app_config import AppConfig

    app_config = AppConfig(
        database={"url": "postgresql://localhost/assembly-test"},
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        read_before_write={"enabled": True},
        tool_progress={"enabled": True},
    )
    guardrail = AgentMiddleware()
    captured = []

    def capture(_middlewares, invariants):
        captured.extend(invariants)

    monkeypatch.setattr(assembly, "_validate_middleware_invariants", capture)
    assembly.build_runtime_middlewares(
        app_config=app_config,
        include_uploads=False,
        include_dangling_tool_call_patch=True,
        sandbox=False,
        guardrail_middleware=guardrail,
    )

    assert len(captured) == 1
    invariant = captured[0]
    assert invariant.name == "private tool-call boundary"
    enabled = [middleware for middleware in invariant.registration_order if middleware is not None]
    assert [type(middleware).__name__ for middleware in enabled] == [
        "SandboxAuditMiddleware",
        "ReadBeforeWriteMiddleware",
        "ToolProgressMiddleware",
        "AgentMiddleware",
        "ToolErrorHandlingMiddleware",
    ]
    assert enabled[-2] is guardrail


def test_phase_builder_declares_reverse_after_model_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assembly = importlib.import_module(
        "deerflow.agents.middlewares.assembly",
    )
    loop_detection = AgentMiddleware()
    safety = AgentMiddleware()
    captured = []

    def capture(_middlewares, invariants):
        captured.extend(invariants)

    monkeypatch.setattr(assembly, "_validate_middleware_invariants", capture)
    assembly.assemble_agent_middlewares(
        runtime=(),
        loop_detection=loop_detection,
        safety=safety,
        clarification=AgentMiddleware(),
    )

    assert len(captured) == 1
    invariant = captured[0]
    assert invariant.registration_order == (loop_detection, safety)
    assert invariant.reverse_hook == "after_model"


def test_output_limit_recovery_is_between_todo_and_token_usage() -> None:
    assembly = importlib.import_module(
        "deerflow.agents.middlewares.assembly",
    )
    planning = AgentMiddleware()
    recovery = AgentMiddleware()
    token_usage = AgentMiddleware()
    loop_detection = AgentMiddleware()
    token_budget = AgentMiddleware()
    safety = AgentMiddleware()

    chain = assembly.assemble_agent_middlewares(
        runtime=(),
        planning=planning,
        output_limit_recovery=recovery,
        token_usage=token_usage,
        loop_detection=loop_detection,
        token_budget=token_budget,
        safety=safety,
        clarification=AgentMiddleware(),
    )

    assert [
        chain.index(item)
        for item in (
            planning,
            recovery,
            token_usage,
            loop_detection,
            token_budget,
            safety,
        )
    ] == sorted(
        chain.index(item)
        for item in (
            planning,
            recovery,
            token_usage,
            loop_detection,
            token_budget,
            safety,
        )
    )


def test_delta_schema_shallow_copy_shares_mutable_middleware_state() -> None:
    """Characterize current sharing; lock-bearing middleware cannot be deep-copied."""

    from deerflow.agents.middlewares.token_budget_middleware import (
        TokenBudgetMiddleware,
    )
    from deerflow.agents.thread_state import normalize_middleware_state_schemas
    from deerflow.config.token_budget_config import TokenBudgetConfig

    original = TokenBudgetMiddleware.from_config(TokenBudgetConfig())
    normalized = normalize_middleware_state_schemas([original], "delta")[0]

    assert normalized is not original
    assert normalized._lock is original._lock
    assert normalized._seen_messages is original._seen_messages
    assert normalized._seen_subagent_receipts is original._seen_subagent_receipts
    assert normalized._seen_subagent_conflicts is original._seen_subagent_conflicts
    assert normalized._pending_warnings is original._pending_warnings
    assert normalized._cumulative_usage is original._cumulative_usage

    with pytest.raises(TypeError):
        copy.deepcopy(original)
