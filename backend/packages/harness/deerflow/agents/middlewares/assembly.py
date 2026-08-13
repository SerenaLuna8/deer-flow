"""Canonical middleware assembly for lead, subagent, and SDK agents.

Concrete middleware behavior remains in each middleware module. This module
owns composition, optional phases, and the relative-order contracts whose
meaning depends on LangChain's outer-to-inner registration order.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware

from deerflow.config.app_config import AppConfig

if TYPE_CHECKING:
    from deerflow.tools.builtins.tool_search import DeferredToolSetup


@dataclass(frozen=True, slots=True)
class _MiddlewareOrderInvariant:
    """One optional relative registration-order contract.

    ``None`` entries represent disabled phases and are ignored. ``reverse_hook``
    documents hooks such as ``after_model`` whose runtime dispatch order is the
    reverse of registration; the required registration sequence remains the
    sequence declared here.
    """

    name: str
    registration_order: tuple[AgentMiddleware | None, ...]
    reverse_hook: str | None = None


def _identity_index(
    middlewares: Sequence[AgentMiddleware],
    target: AgentMiddleware,
) -> int | None:
    return next(
        (index for index, middleware in enumerate(middlewares) if middleware is target),
        None,
    )


def _validate_middleware_invariants(
    middlewares: Sequence[AgentMiddleware],
    invariants: Sequence[_MiddlewareOrderInvariant],
) -> None:
    """Fail construction when an enabled relative-order contract is violated."""

    for invariant in invariants:
        enabled = tuple(middleware for middleware in invariant.registration_order if middleware is not None)
        if len(enabled) < 2:
            continue

        positions: list[int] = []
        for middleware in enabled:
            position = _identity_index(middlewares, middleware)
            if position is None:
                raise RuntimeError(f"Middleware invariant '{invariant.name}' references an enabled {type(middleware).__name__} that is absent from the chain")
            positions.append(position)

        if any(left >= right for left, right in pairwise(positions)):
            expected = " -> ".join(type(middleware).__name__ for middleware in enabled)
            hook_note = f"; {invariant.reverse_hook} dispatches in reverse registration order" if invariant.reverse_hook is not None else ""
            raise RuntimeError(f"Middleware invariant '{invariant.name}' violated: expected relative registration order {expected}{hook_note}")


def build_sandbox_infrastructure(
    *,
    lazy_init: bool = True,
    include_uploads: bool = True,
) -> list[AgentMiddleware]:
    """Thread-scoped sandbox base shared by the lead, subagent, and SDK chains.

    Order is behavior: ThreadData must precede Sandbox so ``thread_id`` exists
    before sandbox setup, and Uploads sits between them because it reads the
    thread identity and feeds workspace files the sandbox mounts. Every
    assembly path takes this trio from here so the ordering cannot fork.
    """
    from deerflow.agents.middlewares.thread_data_middleware import ThreadDataMiddleware
    from deerflow.sandbox.middleware import SandboxMiddleware

    middlewares: list[AgentMiddleware] = [ThreadDataMiddleware(lazy_init=lazy_init)]
    if include_uploads:
        from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware

        middlewares.append(UploadsMiddleware())
    middlewares.append(SandboxMiddleware(lazy_init=lazy_init))
    return middlewares


def assemble_agent_middlewares(
    *,
    runtime: Sequence[AgentMiddleware],
    before_summarization: Sequence[AgentMiddleware] = (),
    summarization: AgentMiddleware | None = None,
    planning: AgentMiddleware | None = None,
    output_limit_recovery: AgentMiddleware | None = None,
    token_usage: AgentMiddleware | None = None,
    title: AgentMiddleware | None = None,
    after_title: Sequence[AgentMiddleware] = (),
    vision: AgentMiddleware | None = None,
    routing: Sequence[AgentMiddleware] = (),
    system_message: AgentMiddleware | None = None,
    subagent: AgentMiddleware | None = None,
    loop_detection: AgentMiddleware | None = None,
    token_budget: AgentMiddleware | None = None,
    custom: Sequence[AgentMiddleware] = (),
    safety: AgentMiddleware | None = None,
    clarification: AgentMiddleware,
) -> list[AgentMiddleware]:
    """Compose the lead/SDK middleware phases in one canonical order.

    Callers own feature resolution; this builder owns sequence. The SDK keeps
    its documented omissions by passing empty phases, while production lead
    assembly supplies its private-context and hardening phases. Keeping the
    final append here makes ``ClarificationMiddleware`` a structural tail
    invariant rather than a convention duplicated by each caller.
    """

    middlewares = [*runtime, *before_summarization]
    for middleware in (
        summarization,
        planning,
        output_limit_recovery,
        token_usage,
        title,
    ):
        if middleware is not None:
            middlewares.append(middleware)
    middlewares.extend(after_title)
    if vision is not None:
        middlewares.append(vision)
    middlewares.extend(routing)
    for middleware in (
        system_message,
        subagent,
        loop_detection,
        token_budget,
    ):
        if middleware is not None:
            middlewares.append(middleware)
    middlewares.extend(custom)
    if safety is not None:
        middlewares.append(safety)
    middlewares.append(clarification)

    invariants = [
        _MiddlewareOrderInvariant(
            name="safety cleanup before loop accounting",
            registration_order=(loop_detection, safety),
            reverse_hook="after_model",
        )
    ]
    if output_limit_recovery is not None:
        invariants.append(
            _MiddlewareOrderInvariant(
                name="output-limit recovery final dispatch",
                registration_order=(
                    planning,
                    output_limit_recovery,
                    token_usage,
                    loop_detection,
                    token_budget,
                    safety,
                ),
                reverse_hook="after_model",
            )
        )
    _validate_middleware_invariants(middlewares, invariants)
    return middlewares


def build_runtime_middlewares(
    *,
    app_config: AppConfig | None,
    include_uploads: bool,
    include_dangling_tool_call_patch: bool,
    include_security_wrappers: bool = True,
    sandbox: bool | AgentMiddleware = True,
    guardrail_middleware: AgentMiddleware | None = None,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    """Build the shared runtime spine for lead, subagent, and SDK paths.

    ``include_security_wrappers=False`` is the SDK profile: it retains the
    sandbox/dangling/error spine and an explicitly supplied custom guardrail,
    but omits private-Run wrappers that require a materialized ``AppConfig``.
    """

    if include_security_wrappers and app_config is None:
        raise ValueError("Security runtime middlewares require AppConfig")

    # Layer 1 — outermost wrap_model_call wrappers (listed outer→inner).
    # InputSanitizationMiddleware is first so it becomes the outermost
    # wrapper — sanitised messages are what every inner middleware sees.
    # ToolResultSanitizationMiddleware mirrors that guardrail for the other
    # untrusted-content entry point: remote tool results (web_fetch /
    # web_search) get the same framework/injection-tag neutralization. It sits
    # inner of ToolOutputBudgetMiddleware (listed after it) so it neutralizes
    # the raw tool output first; the budget wrapper then truncates the already
    # neutralized text.
    outer_wrappers: list[AgentMiddleware] = []
    if include_security_wrappers:
        from deerflow.agents.middlewares.input_sanitization_middleware import (
            InputSanitizationMiddleware,
        )
        from deerflow.agents.middlewares.tool_output_budget_middleware import (
            ToolOutputBudgetMiddleware,
        )
        from deerflow.agents.middlewares.tool_result_sanitization_middleware import (
            ToolResultSanitizationMiddleware,
        )

        assert app_config is not None
        outer_wrappers.extend(
            [
                InputSanitizationMiddleware(),
                ToolOutputBudgetMiddleware.from_app_config(app_config),
                ToolResultSanitizationMiddleware(),
            ]
        )

    if sandbox is False:
        thread_hooks: list[AgentMiddleware] = []
    elif isinstance(sandbox, AgentMiddleware):
        thread_hooks = [sandbox]
    elif sandbox is True:
        thread_hooks = build_sandbox_infrastructure(
            lazy_init=lazy_init,
            include_uploads=include_uploads,
        )
    else:
        raise TypeError("sandbox must be a boolean or AgentMiddleware instance")

    tail: list[AgentMiddleware] = []
    if include_dangling_tool_call_patch:
        from deerflow.agents.middlewares.dangling_tool_call_middleware import (
            DanglingToolCallMiddleware,
        )

        tail.append(DanglingToolCallMiddleware())
    if include_security_wrappers:
        from deerflow.agents.middlewares.llm_error_handling_middleware import (
            LLMErrorHandlingMiddleware,
        )

        assert app_config is not None
        tail.append(LLMErrorHandlingMiddleware(app_config=app_config))

    if include_security_wrappers and guardrail_middleware is None:
        from deerflow.guardrails.middleware import GuardrailMiddleware
        from deerflow.reflection import resolve_variable

        assert app_config is not None
        guardrails_config = app_config.guardrails
        if guardrails_config.enabled and guardrails_config.provider:
            provider_cls = resolve_variable(guardrails_config.provider.use)
            provider_kwargs = dict(guardrails_config.provider.config) if guardrails_config.provider.config else {}
            if "framework" not in provider_kwargs:
                try:
                    sig = inspect.signature(provider_cls.__init__)
                    if "framework" in sig.parameters or any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in sig.parameters.values()):
                        provider_kwargs["framework"] = "deerflow"
                except (ValueError, TypeError):
                    pass
            provider = provider_cls(**provider_kwargs)
            guardrail_middleware = GuardrailMiddleware(
                provider,
                fail_closed=guardrails_config.fail_closed,
                passport=guardrails_config.passport,
            )

    sandbox_audit_middleware: AgentMiddleware | None = None
    if include_security_wrappers:
        from deerflow.agents.middlewares.sandbox_audit_middleware import (
            SandboxAuditMiddleware,
        )

        sandbox_audit_middleware = SandboxAuditMiddleware()
        tail.append(sandbox_audit_middleware)

    read_before_write_middleware: AgentMiddleware | None = None
    if include_security_wrappers and app_config is not None and app_config.read_before_write.enabled:
        from deerflow.agents.middlewares.read_before_write_middleware import (
            ReadBeforeWriteMiddleware,
        )

        read_before_write_middleware = ReadBeforeWriteMiddleware()
        tail.append(read_before_write_middleware)

    tool_progress_middleware: AgentMiddleware | None = None
    if include_security_wrappers and app_config is not None and app_config.tool_progress.enabled:
        from deerflow.agents.middlewares.tool_progress_middleware import (
            ToolProgressMiddleware,
        )

        tool_progress_middleware = ToolProgressMiddleware.from_config(app_config.tool_progress)
        tail.append(tool_progress_middleware)

    if guardrail_middleware is not None:
        tail.append(guardrail_middleware)

    # Delay this import so importing ``assembly`` directly cannot cycle through
    # the legacy module while it re-exports these builders.
    from deerflow.agents.middlewares.tool_error_handling_middleware import (
        ToolErrorHandlingMiddleware,
    )

    tool_error_middleware = ToolErrorHandlingMiddleware(app_config=app_config)
    tail.append(tool_error_middleware)
    middlewares = [*outer_wrappers, *thread_hooks, *tail]

    _validate_middleware_invariants(
        middlewares,
        (
            _MiddlewareOrderInvariant(
                name="private tool-call boundary",
                registration_order=(
                    sandbox_audit_middleware,
                    read_before_write_middleware,
                    tool_progress_middleware,
                    guardrail_middleware,
                    tool_error_middleware,
                ),
            ),
        ),
    )
    return middlewares


def build_lead_runtime_middlewares(
    *,
    app_config: AppConfig,
    lazy_init: bool = True,
) -> list[AgentMiddleware]:
    """Middlewares shared by lead agent runtime before lead-only middlewares."""
    return build_runtime_middlewares(
        app_config=app_config,
        include_uploads=True,
        include_dangling_tool_call_patch=True,
        lazy_init=lazy_init,
    )


def build_subagent_runtime_middlewares(
    *,
    app_config: AppConfig | None = None,
    model_name: str | None = None,
    lazy_init: bool = True,
    deferred_setup: DeferredToolSetup | None = None,
    mcp_routing_middleware: AgentMiddleware | None = None,
    agent_name: str | None = None,
) -> list[AgentMiddleware]:
    """Middlewares shared by subagent runtime before subagent-only middlewares."""
    if app_config is None:
        from deerflow.config import get_app_config

        app_config = get_app_config()

    middlewares = build_runtime_middlewares(
        app_config=app_config,
        include_uploads=False,
        include_dangling_tool_call_patch=True,
        lazy_init=lazy_init,
    )

    if model_name is None and app_config.models:
        model_name = app_config.models[0].name

    model_config = app_config.get_model_config(model_name) if model_name else None
    from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

    middlewares.append(ViewImageMiddleware(enable_injection=bool(model_config is not None and model_config.supports_vision)))

    if mcp_routing_middleware is not None:
        middlewares.append(mcp_routing_middleware)

    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import (
            DeferredToolFilterMiddleware,
        )

        middlewares.append(
            DeferredToolFilterMiddleware(
                deferred_setup.deferred_names,
                deferred_setup.catalog_hash,
            )
        )
        from deerflow.agents.middlewares.mcp_routing_middleware import (
            assert_mcp_routing_before_deferred_filter,
        )

        assert_mcp_routing_before_deferred_filter(middlewares)

    loop_detection_middleware: AgentMiddleware | None = None
    loop_detection_config = app_config.loop_detection
    if loop_detection_config.enabled:
        from deerflow.agents.middlewares.loop_detection_middleware import (
            LoopDetectionMiddleware,
        )

        loop_detection_middleware = LoopDetectionMiddleware.from_config(loop_detection_config)
        middlewares.append(loop_detection_middleware)

    token_budget_config = app_config.subagents.get_token_budget_for(agent_name) if agent_name is not None else app_config.subagents.token_budget
    if token_budget_config.enabled:
        from deerflow.agents.middlewares.token_budget_middleware import (
            TokenBudgetMiddleware,
        )

        middlewares.append(TokenBudgetMiddleware.from_config(token_budget_config))

    safety_middleware: AgentMiddleware | None = None
    safety_config = app_config.safety_finish_reason
    if safety_config.enabled:
        from deerflow.agents.middlewares.safety_finish_reason_middleware import (
            SafetyFinishReasonMiddleware,
        )

        safety_middleware = SafetyFinishReasonMiddleware.from_config(safety_config)
        middlewares.append(safety_middleware)

    _validate_middleware_invariants(
        middlewares,
        (
            _MiddlewareOrderInvariant(
                name="subagent safety cleanup before loop accounting",
                registration_order=(
                    loop_detection_middleware,
                    safety_middleware,
                ),
                reverse_hook="after_model",
            ),
        ),
    )
    return middlewares


__all__ = [
    "assemble_agent_middlewares",
    "build_lead_runtime_middlewares",
    "build_runtime_middlewares",
    "build_sandbox_infrastructure",
    "build_subagent_runtime_middlewares",
]
