"""Exact middleware parity across every lead-agent assembly path.

Middleware order is behavior: first registered is outermost, ``after_model``
dispatches in reverse, and Clarification must stay at the tail. These tests
therefore build the four concrete paths — private lead, non-private lead, SDK,
and embedded client — instead of inferring parity from imports alone.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import tool
from pydantic import SecretStr

import deerflow.agents.factory as factory_module
import deerflow.agents.lead_agent.agent as lead_agent_module
from deerflow.agents.factory import _assemble_from_features
from deerflow.agents.features import RuntimeFeatures
from deerflow.agents.lead_agent.agent import (
    TrustedLeadAgentExtension,
    build_middlewares,
)
from deerflow.agents.middlewares.assembly import (
    build_lead_runtime_middlewares,
    build_subagent_runtime_middlewares,
)
from deerflow.agents.middlewares.manifest import (
    MiddlewareHook,
    middleware_dispatch_order,
)
from deerflow.agents.middlewares.tool_call_control import (
    FixedToolCallControlScope,
    ToolCallControl,
    ToolCallControlBinding,
    ToolCallControlRole,
    build_tool_call_control,
    default_graph_tool_call_control_profile,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.tools.mcp_metadata import tag_mcp_routing, tag_mcp_tool

GOLDEN_MODEL = "golden-model"


@tool
def golden_remote_tool(query: str) -> str:
    """Return one deterministic remote-tool result for assembly tests."""

    return query


tag_mcp_tool(golden_remote_tool)
tag_mcp_routing(
    golden_remote_tool,
    {"mode": "prefer", "priority": 100, "keywords": ["golden"]},
)


def _full_feature_app_config() -> AppConfig:
    """One explicit config that materializes every conditional lead layer."""

    return AppConfig(
        models=[
            ModelConfig(
                name=GOLDEN_MODEL,
                display_name="Golden",
                description="",
                use="langchain_openai:ChatOpenAI",
                model=GOLDEN_MODEL,
                max_input_tokens=64_000,
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
        tool_search={"enabled": True, "auto_promote_top_k": 3},
        guardrails={
            "enabled": True,
            "provider": {
                "use": "deerflow.guardrails.builtin:AllowlistProvider",
                "config": {},
            },
        },
    )


def _names(middlewares: list[AgentMiddleware]) -> list[str]:
    return [type(middleware).__name__ for middleware in middlewares]


def _projection(names: list[str], shared: set[str]) -> list[str]:
    return [name for name in names if name in shared]


def _assert_load_bearing_order(names: list[str]) -> None:
    """Apply all conditional order invariants to one materialized chain."""

    if "FinalProviderRequestGuard" in names:
        assert names[-2:] == [
            "ClarificationMiddleware",
            "FinalProviderRequestGuard",
        ]
    else:
        assert names[-1] == "ClarificationMiddleware"
    error_index = names.index("ToolErrorHandlingMiddleware")
    if "ToolProgressMiddleware" in names:
        assert names.index("ToolProgressMiddleware") < error_index
    guardrails = [index for index, name in enumerate(names) if name in {"GuardrailMiddleware", "_CustomGuardrail"}]
    assert len(guardrails) == 1
    assert guardrails[0] < error_index
    routing_present = "McpRoutingMiddleware" in names
    deferred_present = "DeferredToolFilterMiddleware" in names
    assert routing_present == deferred_present
    if routing_present:
        assert names.index("McpRoutingMiddleware") < names.index("DeferredToolFilterMiddleware")


NON_PRIVATE_LEAD_GOLDEN_CHAIN = [
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
    "GuardrailMiddleware",
    "ToolErrorHandlingMiddleware",
    "DynamicContextMiddleware",
    "SkillActivationMiddleware",
    "DurableContextMiddleware",
    "DeerFlowSummarizationMiddleware",
    "TodoMiddleware",
    "TokenUsageMiddleware",
    "TitleMiddleware",
    "ViewImageMiddleware",
    "McpRoutingMiddleware",
    "DeferredToolFilterMiddleware",
    "SystemMessageCoalescingMiddleware",
    "ToolCallControl",
    "SubagentLimitMiddleware",
    "TokenBudgetMiddleware",
    "SafetyFinishReasonMiddleware",
    "ClarificationMiddleware",
    "FinalProviderRequestGuard",
]

PRIVATE_LEAD_GOLDEN_CHAIN = [
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
    "GuardrailMiddleware",
    "ToolErrorHandlingMiddleware",
    "DynamicContextMiddleware",
    "SkillActivationMiddleware",
    "SkillToolPolicyMiddleware",
    "DurableContextMiddleware",
    "DeerFlowSummarizationMiddleware",
    "TodoMiddleware",
    "OutputLimitRecoveryMiddleware",
    "TokenUsageMiddleware",
    "TitleMiddleware",
    "ViewImageMiddleware",
    "McpRoutingMiddleware",
    "DeferredToolFilterMiddleware",
    "SystemMessageCoalescingMiddleware",
    "ToolCallControl",
    "SubagentLimitMiddleware",
    "TokenBudgetMiddleware",
    "SafetyFinishReasonMiddleware",
    "ClarificationMiddleware",
    "FinalProviderRequestGuard",
]

PRIVATE_LEAD_HOOK_GOLDEN = {
    MiddlewareHook.BEFORE_AGENT: (
        "ThreadDataMiddleware",
        "UploadsMiddleware",
        "SandboxMiddleware",
        "ToolProgressMiddleware",
        "DynamicContextMiddleware",
        "TodoMiddleware",
        "ToolCallControl",
        "TokenBudgetMiddleware",
        "FinalProviderRequestGuard",
    ),
    MiddlewareHook.BEFORE_MODEL: (
        "DynamicContextMiddleware",
        "DurableContextMiddleware",
        "DeerFlowSummarizationMiddleware",
        "TodoMiddleware",
        "ViewImageMiddleware",
        "McpRoutingMiddleware",
    ),
    MiddlewareHook.WRAP_MODEL_CALL: (
        "InputSanitizationMiddleware",
        "ToolOutputBudgetMiddleware",
        "DanglingToolCallMiddleware",
        "LLMErrorHandlingMiddleware",
        "ToolProgressMiddleware",
        "ToolErrorHandlingMiddleware",
        "SkillActivationMiddleware",
        "SkillToolPolicyMiddleware",
        "DurableContextMiddleware",
        "TodoMiddleware",
        "OutputLimitRecoveryMiddleware",
        "ViewImageMiddleware",
        "DeferredToolFilterMiddleware",
        "SystemMessageCoalescingMiddleware",
        "ToolCallControl",
        "TokenBudgetMiddleware",
        "FinalProviderRequestGuard",
    ),
    MiddlewareHook.WRAP_TOOL_CALL: (
        "ToolOutputBudgetMiddleware",
        "ToolResultSanitizationMiddleware",
        "SandboxMiddleware",
        "SandboxAuditMiddleware",
        "ReadBeforeWriteMiddleware",
        "ToolProgressMiddleware",
        "GuardrailMiddleware",
        "ToolErrorHandlingMiddleware",
        "SkillToolPolicyMiddleware",
        "DeferredToolFilterMiddleware",
        "ClarificationMiddleware",
    ),
    MiddlewareHook.AFTER_MODEL: (
        "SafetyFinishReasonMiddleware",
        "TokenBudgetMiddleware",
        "SubagentLimitMiddleware",
        "ToolCallControl",
        "TitleMiddleware",
        "TokenUsageMiddleware",
        "OutputLimitRecoveryMiddleware",
        "TodoMiddleware",
        "DurableContextMiddleware",
    ),
    MiddlewareHook.AFTER_AGENT: (
        "TokenBudgetMiddleware",
        "TokenUsageMiddleware",
        "TodoMiddleware",
        "SandboxMiddleware",
    ),
}


def _build_production_lead_chain(
    *,
    private: bool,
    custom_middlewares: tuple[AgentMiddleware, ...] = (),
) -> list[AgentMiddleware]:
    """Run the real lead factory and capture what it gives LangChain."""

    app_config = _full_feature_app_config()
    private_runtime = None
    if private:
        private_runtime = SimpleNamespace(
            model_ref=GOLDEN_MODEL,
            model_settings=None,
            tool_groups=("task",),
            skills=(),
            safe_manifest=SimpleNamespace(skills=()),
            mcp_tools=(golden_remote_tool,),
            skill_root=Path("/tmp/deerflow-u4-golden-skills"),
            prompt_bundle=None,
            agent_catalog=None,
            soul="",
        )

    def available_tools(**kwargs):
        # Private Runs receive project MCP tools from their immutable runtime,
        # while the non-private lead discovers the same probe normally.
        return [] if kwargs.get("include_mcp") is False else [golden_remote_tool]

    with (
        patch.object(
            lead_agent_module,
            "frozen_checkpoint_channel_mode",
            return_value=None,
        ),
        patch.object(
            lead_agent_module,
            "freeze_checkpoint_channel_mode",
            side_effect=lambda value: value,
        ),
        patch.object(
            lead_agent_module,
            "freeze_checkpoint_snapshot_frequency",
            side_effect=lambda value: value,
        ),
        patch.object(lead_agent_module, "inject_checkpoint_mode"),
        patch.object(lead_agent_module, "load_agent_config", return_value=None),
        patch.object(lead_agent_module, "build_tracing_callbacks", return_value=[]),
        patch.object(
            lead_agent_module.ModelRuntime,
            "build_chat_model",
            return_value=MagicMock(),
        ),
        patch.object(
            lead_agent_module,
            "create_agent",
            return_value=MagicMock(),
        ) as create,
        patch.object(
            lead_agent_module,
            "apply_prompt_template",
            return_value="golden prompt",
        ),
        patch(
            "deerflow.runtime.user_context.get_effective_user_id",
            return_value=None,
        ),
        patch("deerflow.tools.get_available_tools", side_effect=available_tools),
    ):
        lead_agent_module._make_lead_agent(
            {
                "configurable": {
                    "is_plan_mode": True,
                    "subagent_enabled": True,
                    "max_concurrent_subagents": 3,
                }
            },
            app_config=app_config,
            private_runtime=private_runtime,
            trusted_extension=TrustedLeadAgentExtension(
                custom_middlewares=custom_middlewares,
            ),
        )

    return create.call_args.kwargs["middleware"]


def _build_non_private_lead_chain() -> list[AgentMiddleware]:
    return _build_production_lead_chain(private=False)


def _build_private_lead_chain() -> list[AgentMiddleware]:
    return _build_production_lead_chain(private=True)


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
    "ToolCallControl",
    "SubagentLimitMiddleware",
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


def _build_embedded_chain() -> list[AgentMiddleware]:
    app_config = _full_feature_app_config()
    with (
        patch("deerflow.client.get_app_config", return_value=app_config),
        patch(
            "deerflow.client.ModelRuntime.build_chat_model",
            return_value=MagicMock(),
        ),
        patch("deerflow.client.create_agent", return_value=MagicMock()) as create,
        patch("deerflow.client.apply_prompt_template", return_value="golden prompt"),
        patch("deerflow.client.get_effective_user_id", return_value=None),
    ):
        from deerflow.client import DeerFlowClient

        client = DeerFlowClient(
            model_name=GOLDEN_MODEL,
            subagent_enabled=True,
            plan_mode=True,
        )
        with patch.object(
            client,
            "_get_tools",
            return_value=[golden_remote_tool],
        ):
            client._ensure_agent(client._get_runnable_config("golden-thread"))
    return create.call_args.kwargs["middleware"]


def _control(*, role: ToolCallControlRole = "lead") -> ToolCallControl:
    profile = default_graph_tool_call_control_profile()
    policy = profile.lead if role == "lead" else profile.subagent
    return build_tool_call_control(
        policy,
        ToolCallControlBinding(
            role=role,
            scope=FixedToolCallControlScope(f"golden-{role}-scope"),
        ),
    )


def _build_subagent_chain() -> list[AgentMiddleware]:
    return build_subagent_runtime_middlewares(
        app_config=_full_feature_app_config(),
        model_name=GOLDEN_MODEL,
        tool_call_control=_control(role="subagent"),
    )


EMBEDDED_GOLDEN_CHAIN = [name for name in NON_PRIVATE_LEAD_GOLDEN_CHAIN if name != "FinalProviderRequestGuard"]


@pytest.mark.parametrize(
    ("builder", "golden"),
    [
        (_build_non_private_lead_chain, NON_PRIVATE_LEAD_GOLDEN_CHAIN),
        (_build_private_lead_chain, PRIVATE_LEAD_GOLDEN_CHAIN),
        (lambda: _build_sdk_chain()[0], SDK_GOLDEN_CHAIN),
        (_build_embedded_chain, EMBEDDED_GOLDEN_CHAIN),
    ],
    ids=["non-private-lead", "private-lead", "sdk", "embedded"],
)
def test_each_assembly_path_matches_its_exact_golden(builder, golden) -> None:
    assert _names(builder()) == golden


def test_lead_middleware_builder_binds_summarization_to_the_lead_context_model() -> None:
    lead_model = MagicMock()
    summary_model = MagicMock()
    summary_model.profile = {"max_input_tokens": 200_000}
    with patch.object(
        lead_agent_module.ModelRuntime,
        "build_chat_model",
        return_value=summary_model,
    ):
        chain = build_middlewares(
            {"configurable": {}},
            model_name=GOLDEN_MODEL,
            app_config=_full_feature_app_config(),
            context_model=lead_model,
        )

    summarization = next(middleware for middleware in chain if type(middleware).__name__ == "DeerFlowSummarizationMiddleware")
    assert summarization._context_model is lead_model


@pytest.mark.parametrize(
    "builder",
    [
        _build_non_private_lead_chain,
        _build_private_lead_chain,
        lambda: _build_sdk_chain()[0],
        _build_embedded_chain,
        _build_subagent_chain,
    ],
    ids=["configured-lead", "private-lead", "sdk", "embedded", "subagent"],
)
def test_each_auto_assembled_graph_profile_has_exactly_one_tool_call_control(
    builder,
) -> None:
    assert sum(isinstance(middleware, ToolCallControl) for middleware in builder()) == 1


@pytest.mark.parametrize("private", [False, True], ids=["configured-lead", "private-lead"])
def test_lead_auto_assembly_rejects_a_custom_second_tool_call_control(
    private: bool,
) -> None:
    with pytest.raises(
        ValueError,
        match=("tool_call_control_configuration_invalid: auto assembly accepts at most one ToolCallControl"),
    ):
        _build_production_lead_chain(
            private=private,
            custom_middlewares=(_control(),),
        )


def test_sdk_auto_assembly_rejects_an_extra_second_tool_call_control() -> None:
    with (
        patch.object(factory_module, "create_agent") as create,
        pytest.raises(
            ValueError,
            match=("tool_call_control_configuration_invalid: auto assembly accepts at most one ToolCallControl"),
        ),
    ):
        factory_module.create_deerflow_agent(
            MagicMock(),
            features=RuntimeFeatures(loop_detection=True),
            extra_middleware=[_control()],
        )
    create.assert_not_called()


def test_embedded_auto_assembly_rejects_a_custom_second_tool_call_control() -> None:
    app_config = _full_feature_app_config()
    with (
        patch("deerflow.client.get_app_config", return_value=app_config),
        patch(
            "deerflow.client.ModelRuntime.build_chat_model",
            return_value=MagicMock(),
        ),
        patch("deerflow.client.create_agent", return_value=MagicMock()),
        patch("deerflow.client.apply_prompt_template", return_value="golden prompt"),
        patch("deerflow.client.get_effective_user_id", return_value=None),
    ):
        from deerflow.client import DeerFlowClient

        client = DeerFlowClient(
            model_name=GOLDEN_MODEL,
            middlewares=[_control()],
        )
        with (
            patch.object(client, "_get_tools", return_value=[]),
            pytest.raises(
                ValueError,
                match=("tool_call_control_configuration_invalid: auto assembly accepts at most one ToolCallControl"),
            ),
        ):
            client._ensure_agent(client._get_runnable_config("golden-thread"))


def test_subagent_auto_assembly_rejects_an_extra_second_tool_call_control() -> None:
    with pytest.raises(
        ValueError,
        match=("tool_call_control_configuration_invalid: auto assembly accepts at most one ToolCallControl"),
    ):
        _assemble_from_features(
            RuntimeFeatures(loop_detection=True),
            extra_middleware=[_control(role="subagent")],
            delegated=True,
            tool_call_control=_control(role="subagent"),
        )


def test_sdk_explicit_control_exceptions_remain_single_owner() -> None:
    explicitly_disabled, _ = _assemble_from_features(
        RuntimeFeatures(loop_detection=False),
        extra_middleware=[_control()],
    )
    assert sum(isinstance(middleware, ToolCallControl) for middleware in explicitly_disabled) == 1

    caller_control = _control()
    captured: dict[str, object] = {}
    with patch.object(
        factory_module,
        "create_agent",
        side_effect=lambda **kwargs: captured.update(kwargs) or MagicMock(),
    ):
        factory_module.create_deerflow_agent(
            MagicMock(),
            middleware=[caller_control],
        )
    assert captured["middleware"] == [caller_control]


def test_private_lead_hook_dispatch_matches_exact_golden() -> None:
    chain = _build_private_lead_chain()

    assert {hook: middleware_dispatch_order(chain, hook) for hook in MiddlewareHook} == PRIVATE_LEAD_HOOK_GOLDEN


@pytest.mark.parametrize(
    "builder",
    [
        _build_non_private_lead_chain,
        _build_private_lead_chain,
        lambda: _build_sdk_chain()[0],
        _build_embedded_chain,
    ],
    ids=["non-private-lead", "private-lead", "sdk", "embedded"],
)
def test_each_assembly_path_keeps_load_bearing_order(builder) -> None:
    names = _names(builder())
    _assert_load_bearing_order(names)
    if "InputSanitizationMiddleware" in names:
        assert names[0] == "InputSanitizationMiddleware"
        assert names.index("ToolCallControl") < names.index("SubagentLimitMiddleware")
        assert names.index("SubagentLimitMiddleware") < names.index("TokenBudgetMiddleware")
        assert names.index("TokenBudgetMiddleware") < names.index("SafetyFinishReasonMiddleware")
        assert names.index("SystemMessageCoalescingMiddleware") > names.index("DeerFlowSummarizationMiddleware")


def test_sdk_factory_injects_the_feature_tools() -> None:
    _chain, tools = _build_sdk_chain()
    assert [tool.name for tool in tools] == [
        "view_image",
        "task",
        "ask_clarification",
    ]


def test_all_four_chains_agree_on_the_shared_spine() -> None:
    chains = [
        _names(_build_non_private_lead_chain()),
        _names(_build_private_lead_chain()),
        _names(_build_sdk_chain()[0]),
        _names(_build_embedded_chain()),
    ]
    shared = set.intersection(*(set(chain) for chain in chains))
    assert shared, "the four assembly paths no longer share any middleware"
    expected_projection = _projection(chains[0], shared)
    assert all(_projection(chain, shared) == expected_projection for chain in chains[1:])
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
        "ToolCallControl",
        "TokenBudgetMiddleware",
        "ClarificationMiddleware",
    } <= shared


LEAD_ONLY_MIDDLEWARES = {
    "InputSanitizationMiddleware",
    "ToolOutputBudgetMiddleware",
    "ToolResultSanitizationMiddleware",
    "LLMErrorHandlingMiddleware",
    "SandboxAuditMiddleware",
    "ReadBeforeWriteMiddleware",
    "ToolProgressMiddleware",
    "GuardrailMiddleware",
    "DynamicContextMiddleware",
    "SkillActivationMiddleware",
    "DurableContextMiddleware",
    "SystemMessageCoalescingMiddleware",
    "SafetyFinishReasonMiddleware",
    "TokenUsageMiddleware",
    "DeerFlowSummarizationMiddleware",
    "McpRoutingMiddleware",
    "DeferredToolFilterMiddleware",
    "FinalProviderRequestGuard",
}


def test_sdk_gaps_are_documented_deliberate_differences() -> None:
    lead = set(_names(_build_non_private_lead_chain()))
    sdk = set(_names(_build_sdk_chain()[0]))
    assert lead - sdk == LEAD_ONLY_MIDDLEWARES
    assert sdk - lead == {
        "_CustomGuardrail",
        "_CustomSummarization",
        "_CustomMemory",
    }


def test_private_lead_only_adds_exact_skill_policy_to_non_private_lead() -> None:
    private = _names(_build_private_lead_chain())
    non_private = _names(_build_non_private_lead_chain())
    assert [name for name in private if name not in {"SkillToolPolicyMiddleware", "OutputLimitRecoveryMiddleware"}] == non_private
    assert private.count("SkillToolPolicyMiddleware") == 1


def test_embedded_client_imports_the_exact_lead_builder() -> None:
    import deerflow.client as client_module

    assert client_module.build_middlewares is build_middlewares


def test_sdk_factory_delegates_to_shared_runtime_and_phase_builders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature mapping stays thin; shared builders own sequence and invariants."""

    runtime_marker = _CustomGuardrail()
    calls: dict[str, dict] = {}

    def build_runtime(**kwargs):
        calls["runtime"] = kwargs
        return [runtime_marker]

    def assemble(**kwargs):
        calls["assembly"] = kwargs
        return [*kwargs["runtime"], kwargs["clarification"]]

    monkeypatch.setattr(factory_module, "build_runtime_middlewares", build_runtime)
    monkeypatch.setattr(factory_module, "assemble_agent_middlewares", assemble)

    chain, _tools = factory_module._assemble_from_features(
        RuntimeFeatures(
            sandbox=False,
            loop_detection=False,
            token_budget=False,
        )
    )

    assert chain[0] is runtime_marker
    assert calls["runtime"] == {
        "app_config": None,
        "include_uploads": True,
        "include_dangling_tool_call_patch": True,
        "include_security_wrappers": False,
        "sandbox": False,
        "guardrail_middleware": None,
    }
    assert calls["assembly"]["runtime"] == (runtime_marker,)
    assert isinstance(
        calls["assembly"]["clarification"],
        factory_module.ClarificationMiddleware,
    )


def test_subagent_runtime_chain_is_the_lead_runtime_minus_uploads() -> None:
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
        "TokenBudgetMiddleware",
        "SafetyFinishReasonMiddleware",
    ]
