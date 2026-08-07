"""U4 — golden middleware-chain consistency across every agent assembly path.

ActWeave assembles "the same" logical agent through three entry points:

1. ``build_middlewares`` — the production lead-agent chain used by
   ``make_lead_agent`` (Worker private Runs and LangGraph server mode).
2. ``create_deerflow_agent`` / ``_assemble_from_features`` — the SDK factory.
3. ``DeerFlowClient`` — the embedded client, which must import the exact
   ``build_middlewares`` function rather than fork its own chain.

Middleware order is behavior: sanitization must wrap everything, tool
progress must enclose error stamping, and Clarification must stay last.
These tests freeze each chain as an explicit golden fingerprint plus a
shared-spine projection, so any insertion, removal, or reorder fails here
and forces a deliberate, reviewed decision instead of silent drift.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from pydantic import SecretStr

from deerflow.agents.factory import _assemble_from_features
from deerflow.agents.features import RuntimeFeatures
from deerflow.agents.lead_agent.agent import build_middlewares
from deerflow.agents.middlewares.tool_error_handling_middleware import (
    build_lead_runtime_middlewares,
    build_subagent_runtime_middlewares,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig

GOLDEN_MODEL = "golden-model"


def _full_feature_app_config() -> AppConfig:
    """One explicit full-featured config so golden lists cover every branch."""

    return AppConfig(
        models=[
            ModelConfig(
                name=GOLDEN_MODEL,
                display_name="Golden",
                description="",
                use="langchain_openai:ChatOpenAI",
                model=GOLDEN_MODEL,
                api_key=SecretStr("unit-test-key"),
                base_url="https://example.invalid/v1",
                supports_thinking=False,
                supports_vision=True,
            )
        ],
        sandbox={"use": "deerflow.sandbox.local:LocalSandboxProvider"},
        database={"url": "postgresql://localhost/golden"},
        summarization={"enabled": True, "model_name": GOLDEN_MODEL},
        token_usage={"enabled": True},
        loop_detection={"enabled": True},
        token_budget={"enabled": True},
        safety_finish_reason={"enabled": True},
        tool_progress={"enabled": True},
        read_before_write={"enabled": True},
    )


def _names(middlewares: list[AgentMiddleware]) -> list[str]:
    return [type(middleware).__name__ for middleware in middlewares]


def _projection(names: list[str], shared: set[str]) -> list[str]:
    return [name for name in names if name in shared]


# ---------------------------------------------------------------------------
# Path 1 — production lead chain
# ---------------------------------------------------------------------------

LEAD_GOLDEN_CHAIN = [
    # build_lead_runtime_middlewares — security wrappers outermost.
    "InputSanitizationMiddleware",
    "ToolOutputBudgetMiddleware",
    "ToolResultSanitizationMiddleware",
    "ThreadDataMiddleware",
    "UploadsMiddleware",
    "SandboxMiddleware",
    "DanglingToolCallMiddleware",
    "LLMErrorHandlingMiddleware",
    "SandboxAuditMiddleware",
    "ReadBeforeWriteMiddleware",
    "ToolProgressMiddleware",
    "ToolErrorHandlingMiddleware",
    # build_middlewares — lead-only composition.
    "DynamicContextMiddleware",
    "SkillActivationMiddleware",
    "DurableContextMiddleware",
    "DeerFlowSummarizationMiddleware",
    "TodoMiddleware",
    "TokenUsageMiddleware",
    "TitleMiddleware",
    "ViewImageMiddleware",
    "SystemMessageCoalescingMiddleware",
    "SubagentLimitMiddleware",
    "LoopDetectionMiddleware",
    "TokenBudgetMiddleware",
    "SafetyFinishReasonMiddleware",
    "ClarificationMiddleware",
]


def _build_lead_chain() -> list[AgentMiddleware]:
    return build_middlewares(
        {"configurable": {"is_plan_mode": True}},
        model_name=GOLDEN_MODEL,
        agent_name=None,
        app_config=_full_feature_app_config(),
        resolved_subagent_enabled=True,
        resolved_max_concurrent_subagents=3,
    )


def test_lead_build_middlewares_matches_the_golden_fingerprint() -> None:
    assert _names(_build_lead_chain()) == LEAD_GOLDEN_CHAIN


def test_lead_chain_keeps_load_bearing_order_invariants() -> None:
    names = _names(_build_lead_chain())
    # Input sanitization is the outermost wrapper of every model call.
    assert names[0] == "InputSanitizationMiddleware"
    # Clarification interception must stay last.
    assert names[-1] == "ClarificationMiddleware"
    # ToolProgress (outer) must enclose ToolErrorHandling (inner stamping).
    assert names.index("ToolProgressMiddleware") < names.index("ToolErrorHandlingMiddleware")
    # Safety strip runs before loop accounting (reverse after_model dispatch).
    assert names.index("LoopDetectionMiddleware") < names.index("SafetyFinishReasonMiddleware")
    # System messages are coalesced only after all prompt-producing middlewares.
    assert names.index("SystemMessageCoalescingMiddleware") > names.index("DeerFlowSummarizationMiddleware")


# ---------------------------------------------------------------------------
# Path 2 — SDK factory chain
# ---------------------------------------------------------------------------


class _CustomMemory(AgentMiddleware):
    pass


class _CustomSummarization(AgentMiddleware):
    pass


class _CustomGuardrail(AgentMiddleware):
    pass


SDK_GOLDEN_CHAIN = [
    "ThreadDataMiddleware",
    "UploadsMiddleware",
    "SandboxMiddleware",
    "DanglingToolCallMiddleware",
    "_CustomGuardrail",
    "ToolErrorHandlingMiddleware",
    "_CustomSummarization",
    "TodoMiddleware",
    "TitleMiddleware",
    "_CustomMemory",
    "ViewImageMiddleware",
    "SubagentLimitMiddleware",
    "LoopDetectionMiddleware",
    "TokenBudgetMiddleware",
    "ClarificationMiddleware",
]


def _build_sdk_chain() -> tuple[list[AgentMiddleware], list]:
    return _assemble_from_features(
        RuntimeFeatures(
            sandbox=True,
            memory=_CustomMemory(),
            summarization=_CustomSummarization(),
            subagent=True,
            vision=True,
            auto_title=True,
            guardrail=_CustomGuardrail(),
            loop_detection=True,
            token_budget=True,
        ),
        plan_mode=True,
    )


def test_sdk_factory_matches_the_golden_fingerprint() -> None:
    chain, _tools = _build_sdk_chain()
    assert _names(chain) == SDK_GOLDEN_CHAIN


def test_sdk_factory_injects_the_feature_tools() -> None:
    _chain, tools = _build_sdk_chain()
    assert [tool.name for tool in tools] == ["view_image", "task", "ask_clarification"]


# ---------------------------------------------------------------------------
# Cross-path consistency
# ---------------------------------------------------------------------------


def test_lead_and_sdk_chains_agree_on_the_shared_spine() -> None:
    """The middlewares present in both paths must keep the same relative order.

    The two chains legitimately differ in coverage (the SDK path has no
    Dynamic/Skill/Durable context and no runtime security wrappers yet), but
    for every middleware class they both include, relative order is a shared
    behavioral contract and may not diverge.
    """

    lead = _names(_build_lead_chain())
    sdk = _names(_build_sdk_chain()[0])
    shared = set(lead) & set(sdk)
    assert shared, "the two assembly paths no longer share any middleware"
    assert _projection(lead, shared) == _projection(sdk, shared)
    # The spine itself must not silently shrink: these classes exist in both
    # paths today and each removal must be a reviewed decision.
    assert {
        "ThreadDataMiddleware",
        "UploadsMiddleware",
        "SandboxMiddleware",
        "DanglingToolCallMiddleware",
        "ToolErrorHandlingMiddleware",
        "TodoMiddleware",
        "TitleMiddleware",
        "ViewImageMiddleware",
        "SubagentLimitMiddleware",
        "LoopDetectionMiddleware",
        "TokenBudgetMiddleware",
        "ClarificationMiddleware",
    } <= shared


LEAD_ONLY_MIDDLEWARES = {
    # Runtime security wrappers assuming the private Run authorization runtime.
    "InputSanitizationMiddleware",
    "ToolOutputBudgetMiddleware",
    "ToolResultSanitizationMiddleware",
    "LLMErrorHandlingMiddleware",
    "SandboxAuditMiddleware",
    "ReadBeforeWriteMiddleware",
    "ToolProgressMiddleware",
    # Private-context composition.
    "DynamicContextMiddleware",
    "SkillActivationMiddleware",
    "DurableContextMiddleware",
    # Provider/runtime hardening.
    "SystemMessageCoalescingMiddleware",
    "SafetyFinishReasonMiddleware",
    "TokenUsageMiddleware",
    # The lead builds summarization from config; the SDK requires a custom
    # instance (represented below by the _CustomSummarization stand-in).
    "DeerFlowSummarizationMiddleware",
}


def test_sdk_gaps_are_documented_deliberate_differences() -> None:
    """Lead-only middlewares form a reviewed list, not accidental drift.

    ``deerflow/agents/factory.py``'s module docstring documents the same
    deliberate differences; a middleware may only move between the two chains
    together with this list and that docstring.
    """

    lead = set(_names(_build_lead_chain()))
    sdk = set(_names(_build_sdk_chain()[0]))
    assert lead - sdk == LEAD_ONLY_MIDDLEWARES
    # The SDK adds no built-in middleware the lead chain lacks — its extras
    # here are the custom feature-instance stand-ins used by this test.
    assert sdk - lead == {"_CustomGuardrail", "_CustomSummarization", "_CustomMemory"}


def test_embedded_client_imports_the_exact_lead_builder() -> None:
    """DeerFlowClient must reuse ``build_middlewares``, never fork it."""

    import deerflow.client as client_module

    assert client_module.build_middlewares is build_middlewares


def test_subagent_runtime_chain_is_the_lead_runtime_minus_uploads() -> None:
    """Lead and subagent share one runtime security spine by construction.

    ``build_subagent_runtime_middlewares`` must stay a projection of the lead
    runtime base (no uploads middleware) plus its own tail — never a fork
    with different sanitization or error-handling order.
    """

    app_config = _full_feature_app_config()
    lead_base = _names(build_lead_runtime_middlewares(app_config=app_config))
    subagent = _names(
        build_subagent_runtime_middlewares(
            app_config=app_config,
            model_name=GOLDEN_MODEL,
        )
    )

    expected_base = [name for name in lead_base if name != "UploadsMiddleware"]
    assert subagent[: len(expected_base)] == expected_base
    assert subagent[len(expected_base) :] == [
        "ViewImageMiddleware",
        "LoopDetectionMiddleware",
        "TokenBudgetMiddleware",
        "SafetyFinishReasonMiddleware",
    ]
