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

from deerflow.agents.middlewares.manifest import (
    MiddlewareDispatchConstraint,
    MiddlewareHook,
    MiddlewarePhase,
    assign_middleware_layer,
    validate_middleware_dispatch_constraints,
    validate_middleware_phase_ladder,
)
from deerflow.config.app_config import AppConfig

if TYPE_CHECKING:
    from deerflow.agents.middlewares.provider_request_usage import (
        FinalProviderRequestGuard,
    )
    from deerflow.tools.builtins.tool_search import DeferredToolSetup


_TOOL_CALL_CONTROL_CONFIGURATION_ERROR = (
    "tool_call_control_configuration_invalid: auto assembly accepts at most "
    "one ToolCallControl; remove the duplicate custom/extra middleware, set "
    "RuntimeFeatures.loop_detection=False to provide one explicitly, or use "
    "middleware= for full takeover"
)


def _validate_tool_call_control_cardinality(
    middlewares: Sequence[AgentMiddleware | None],
) -> None:
    """Reject ambiguous ownership before an auto-assembled graph is built."""

    from deerflow.agents.middlewares.tool_call_control import ToolCallControl

    if sum(isinstance(middleware, ToolCallControl) for middleware in middlewares) > 1:
        raise ValueError(_TOOL_CALL_CONTROL_CONFIGURATION_ERROR)


def _layer(
    middleware: AgentMiddleware,
    *,
    layer_id: str,
    phase: MiddlewarePhase,
    slot: int,
    why: str,
) -> AgentMiddleware:
    return assign_middleware_layer(
        middleware,
        layer_id=layer_id,
        phase=phase,
        slot=slot,
        why=why,
    )


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


def append_final_provider_request_guard(
    middlewares: Sequence[AgentMiddleware],
    guard: FinalProviderRequestGuard,
) -> list[AgentMiddleware]:
    """Append the one innermost read-only provider request meter.

    The guard is constructed only after the final task-bound tool set exists,
    so it cannot participate in the earlier generic assembly function.  This
    seam preserves Clarification's existing relative tail while making the
    provider guard structurally last across every wrap-model-call shaper.
    """

    from deerflow.agents.middlewares.provider_request_usage import (
        FinalProviderRequestGuard,
    )

    if not isinstance(guard, FinalProviderRequestGuard):
        raise TypeError("final provider request guard has an invalid type")
    result = list(middlewares)
    if any(isinstance(middleware, FinalProviderRequestGuard) for middleware in result):
        raise ValueError("final provider request guard is already installed")
    result.append(
        _layer(
            guard,
            layer_id="final_provider_request_guard",
            phase=MiddlewarePhase.FINAL_PROVIDER_REQUEST,
            slot=10,
            why="The final shaped request is measured immediately before provider invocation.",
        )
    )
    validate_middleware_phase_ladder(result)
    return result


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

    middlewares: list[AgentMiddleware] = [
        _layer(
            ThreadDataMiddleware(lazy_init=lazy_init),
            layer_id="thread_data",
            phase=MiddlewarePhase.THREAD_INFRA,
            slot=10,
            why="Thread identity must exist before uploads and sandbox setup.",
        )
    ]
    if include_uploads:
        from deerflow.agents.middlewares.uploads_middleware import UploadsMiddleware

        middlewares.append(
            _layer(
                UploadsMiddleware(),
                layer_id="uploads",
                phase=MiddlewarePhase.THREAD_INFRA,
                slot=20,
                why="Uploads resolve against the thread before sandbox mounts.",
            )
        )
    middlewares.append(
        _layer(
            SandboxMiddleware(lazy_init=lazy_init),
            layer_id="sandbox",
            phase=MiddlewarePhase.THREAD_INFRA,
            slot=30,
            why="Sandbox consumes thread identity and admitted uploads.",
        )
    )
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
    tool_call_control: AgentMiddleware | None = None,
    host_execution_batch_barrier: AgentMiddleware | None = None,
    subagent: AgentMiddleware | None = None,
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

    _validate_tool_call_control_cardinality(
        (
            *runtime,
            *before_summarization,
            summarization,
            planning,
            output_limit_recovery,
            token_usage,
            title,
            *after_title,
            vision,
            *routing,
            system_message,
            tool_call_control,
            host_execution_batch_barrier,
            subagent,
            token_budget,
            *custom,
            safety,
            clarification,
        )
    )

    middlewares = list(runtime)
    for index, middleware in enumerate(before_summarization, start=1):
        middlewares.append(
            _layer(
                middleware,
                layer_id=(f"private_context_{index}_{type(middleware).__name__}"),
                phase=MiddlewarePhase.PRIVATE_CONTEXT,
                slot=index * 10,
                why="Private runtime context is established before compaction.",
            )
        )
    optional_layers = (
        (
            summarization,
            "summarization",
            MiddlewarePhase.COMPACTION,
            10,
            "Compaction runs after private context capture.",
        ),
        (
            planning,
            "planning",
            MiddlewarePhase.PLANNING,
            10,
            "Planning consumes compacted conversation state.",
        ),
        (
            output_limit_recovery,
            "output_limit_recovery",
            MiddlewarePhase.RESPONSE_RECOVERY,
            10,
            "Output-limit recovery precedes accounting in registration order.",
        ),
        (
            token_usage,
            "token_usage",
            MiddlewarePhase.ACCOUNTING,
            10,
            "Token usage observes recovered model output.",
        ),
        (
            title,
            "title",
            MiddlewarePhase.ACCOUNTING,
            20,
            "Title generation follows token accounting.",
        ),
    )
    for middleware, layer_id, phase, slot, why in optional_layers:
        if middleware is not None:
            middlewares.append(
                _layer(
                    middleware,
                    layer_id=layer_id,
                    phase=phase,
                    slot=slot,
                    why=why,
                )
            )
    for index, middleware in enumerate(after_title, start=1):
        middlewares.append(
            _layer(
                middleware,
                layer_id=(f"accounting_after_title_{index}_{type(middleware).__name__}"),
                phase=MiddlewarePhase.ACCOUNTING,
                slot=20 + index * 10,
                why="Caller accounting extensions follow title handling.",
            )
        )
    if vision is not None:
        middlewares.append(
            _layer(
                vision,
                layer_id="vision",
                phase=MiddlewarePhase.REQUEST_SHAPING,
                slot=10,
                why="Vision shapes the final model request.",
            )
        )
    for index, middleware in enumerate(routing, start=1):
        middlewares.append(
            _layer(
                middleware,
                layer_id=(f"request_routing_{index}_{type(middleware).__name__}"),
                phase=MiddlewarePhase.REQUEST_SHAPING,
                slot=10 + index * 10,
                why="Routing and deferred filtering shape available tools.",
            )
        )
    if system_message is not None:
        middlewares.append(
            _layer(
                system_message,
                layer_id="system_message_coalescing",
                phase=MiddlewarePhase.REQUEST_SHAPING,
                slot=90,
                why="Provider-facing system messages are coalesced after routing.",
            )
        )
    _append_tool_call_arbitration_band(
        middlewares,
        tool_call_control=tool_call_control,
        host_execution_batch_barrier=host_execution_batch_barrier,
        subagent=subagent,
        token_budget=token_budget,
        custom=custom,
        safety=safety,
    )
    middlewares.append(
        _layer(
            clarification,
            layer_id="clarification",
            phase=MiddlewarePhase.INTERRUPT_TAIL,
            slot=10,
            why="Clarification is the structural tail.",
        )
    )

    arbitration_order = (
        tool_call_control,
        host_execution_batch_barrier,
        subagent,
        token_budget,
        *custom,
        safety,
    )
    invariants = [
        _MiddlewareOrderInvariant(
            name="protected tool-call arbitration band",
            registration_order=arbitration_order,
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
                    tool_call_control,
                    host_execution_batch_barrier,
                    subagent,
                    token_budget,
                    *custom,
                    safety,
                ),
                reverse_hook="after_model",
            )
        )
    _validate_middleware_invariants(middlewares, invariants)
    validate_middleware_phase_ladder(middlewares)
    validate_middleware_dispatch_constraints(
        middlewares,
        (
            MiddlewareDispatchConstraint(
                name="safety cleanup before token budget arbitration",
                hook=MiddlewareHook.AFTER_MODEL,
                first="safety_finish_reason",
                then="token_budget",
                why="Safety must clear unsafe tool calls before any budget arbitration.",
            ),
            MiddlewareDispatchConstraint(
                name="token budget before subagent arbitration",
                hook=MiddlewareHook.AFTER_MODEL,
                first="token_budget",
                then="subagent_limit",
                why="Token accounting must run before the per-Run subagent limiter.",
            ),
            MiddlewareDispatchConstraint(
                name="subagent arbitration before host batch serialization",
                hook=MiddlewareHook.AFTER_MODEL,
                first="subagent_limit",
                then="host_execution_batch_barrier",
                why="The subagent limiter must precede approval-mode batch serialization.",
            ),
            MiddlewareDispatchConstraint(
                name="host batch serialization before tool-call control",
                hook=MiddlewareHook.AFTER_MODEL,
                first="host_execution_batch_barrier",
                then="tool_call_control",
                why="Tool-call control must arbitrate the batch that can actually reach ToolNode.",
            ),
            MiddlewareDispatchConstraint(
                name="token budget before output-limit recovery",
                hook=MiddlewareHook.AFTER_MODEL,
                first="token_budget",
                then="output_limit_recovery",
                why="Recovery must see the final token-budget outcome.",
            ),
            MiddlewareDispatchConstraint(
                name="token usage before output-limit recovery",
                hook=MiddlewareHook.AFTER_MODEL,
                first="token_usage",
                then="output_limit_recovery",
                why="Recovery consumes accounting captured from the response.",
            ),
            MiddlewareDispatchConstraint(
                name="output-limit recovery before planning cleanup",
                hook=MiddlewareHook.AFTER_MODEL,
                first="output_limit_recovery",
                then="planning",
                why="Planning is intentionally downstream of recovery.",
            ),
        ),
    )
    return middlewares


def _append_tool_call_arbitration_band(
    middlewares: list[AgentMiddleware],
    *,
    tool_call_control: AgentMiddleware | None,
    host_execution_batch_barrier: AgentMiddleware | None,
    subagent: AgentMiddleware | None,
    token_budget: AgentMiddleware | None,
    safety: AgentMiddleware | None,
    custom: Sequence[AgentMiddleware] = (),
) -> None:
    """Append the protected proposal chain and its outer response hooks.

    Registration order is intentionally the inverse of ``after_model``
    dispatch. Caller-owned middleware may run after Safety and before Token
    Budget, but cannot split Subagent, Host Batch, and ToolCallControl.
    """

    for instance, layer_id, slot, why in (
        (
            tool_call_control,
            "tool_call_control",
            10,
            "Tool-call control is the final proposal arbiter in reverse dispatch.",
        ),
        (
            host_execution_batch_barrier,
            "host_execution_batch_barrier",
            20,
            "Approval-capable batches are serialized before tool-call control.",
        ),
        (
            subagent,
            "subagent_limit",
            30,
            "Per-Run subagent policy remains independent from process scheduling.",
        ),
        (
            token_budget,
            "token_budget",
            40,
            "Token budget runs after custom response hooks in reverse dispatch.",
        ),
    ):
        if instance is not None:
            middlewares.append(
                _layer(
                    instance,
                    layer_id=layer_id,
                    phase=MiddlewarePhase.TOOL_CALL_ARBITRATION,
                    slot=slot,
                    why=why,
                )
            )
    for index, middleware in enumerate(custom, start=1):
        middlewares.append(
            _layer(
                middleware,
                layer_id=f"custom_{index}_{type(middleware).__name__}",
                phase=MiddlewarePhase.CUSTOM,
                slot=index * 10,
                why="Caller-owned after_model hooks run after Safety and before Token Budget.",
            )
        )
    if safety is not None:
        middlewares.append(
            _layer(
                safety,
                layer_id="safety_finish_reason",
                phase=MiddlewarePhase.RESPONSE_GATE,
                slot=10,
                why="Safety sees the raw model response before every other arbiter.",
            )
        )


def _host_execution_approval_is_required(app_config: AppConfig) -> bool:
    from deerflow.sandbox.security import (
        HostBashExecutionMode,
        resolve_host_bash_execution_mode,
    )

    return resolve_host_bash_execution_mode(app_config) is HostBashExecutionMode.LOCAL_APPROVAL_REQUIRED


def build_host_execution_batch_barrier(
    *,
    app_config: AppConfig,
) -> AgentMiddleware | None:
    """Build the approval-mode batch arbiter selected by ``AppConfig``."""

    if not _host_execution_approval_is_required(app_config):
        return None

    from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
        HostExecutionBatchBarrierMiddleware,
    )

    return HostExecutionBatchBarrierMiddleware()


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
    # the raw tool output first; the budget wrapper then applies its generic
    # budget to neutralized text or preserves a strict, independently bounded
    # canonical Vision result.
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
                _layer(
                    InputSanitizationMiddleware(),
                    layer_id="input_sanitization",
                    phase=MiddlewarePhase.UNTRUSTED_CONTENT,
                    slot=10,
                    why="Every inner model wrapper must see sanitized input.",
                ),
                _layer(
                    ToolOutputBudgetMiddleware.from_app_config(app_config),
                    layer_id="tool_output_budget",
                    phase=MiddlewarePhase.UNTRUSTED_CONTENT,
                    slot=20,
                    why=("Budgeting wraps sanitized remote tool output and preserves independently bounded canonical Vision JSON."),
                ),
                _layer(
                    ToolResultSanitizationMiddleware(),
                    layer_id="tool_result_sanitization",
                    phase=MiddlewarePhase.UNTRUSTED_CONTENT,
                    slot=30,
                    why="Raw remote tool results are sanitized before reuse.",
                ),
            ]
        )

    if sandbox is False:
        thread_hooks: list[AgentMiddleware] = []
    elif isinstance(sandbox, AgentMiddleware):
        thread_hooks = [
            _layer(
                sandbox,
                layer_id="sandbox",
                phase=MiddlewarePhase.THREAD_INFRA,
                slot=30,
                why="Caller-supplied sandbox occupies the canonical sandbox slot.",
            )
        ]
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

        tail.append(
            _layer(
                DanglingToolCallMiddleware(),
                layer_id="dangling_tool_call",
                phase=MiddlewarePhase.TRANSCRIPT_REPAIR,
                slot=10,
                why="Repair dangling tool calls before model and tool wrappers.",
            )
        )
    if include_security_wrappers:
        from deerflow.agents.middlewares.llm_error_handling_middleware import (
            LLMErrorHandlingMiddleware,
        )

        assert app_config is not None
        tail.append(
            _layer(
                LLMErrorHandlingMiddleware(app_config=app_config),
                layer_id="llm_error_handling",
                phase=MiddlewarePhase.TRANSCRIPT_REPAIR,
                slot=20,
                why="Translate model failures after transcript repair.",
            )
        )

    host_execution_approval_pause: AgentMiddleware | None = None
    if include_security_wrappers and app_config is not None:
        if _host_execution_approval_is_required(app_config):
            from deerflow.agents.middlewares.host_execution_batch_barrier_middleware import (
                HostExecutionApprovalPauseMiddleware,
            )

            host_execution_approval_pause = _layer(
                HostExecutionApprovalPauseMiddleware(),
                layer_id="host_execution_approval_pause",
                phase=MiddlewarePhase.TOOL_CALL_BOUNDARY,
                slot=5,
                why=("A staged approval exits before the next model call, after the ToolNode checkpoint."),
            )
            tail.append(host_execution_approval_pause)

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

        sandbox_audit_middleware = _layer(
            SandboxAuditMiddleware(),
            layer_id="sandbox_audit",
            phase=MiddlewarePhase.TOOL_CALL_BOUNDARY,
            slot=10,
            why="Audit wraps all admitted sandbox side effects.",
        )
        tail.append(sandbox_audit_middleware)

    read_before_write_middleware: AgentMiddleware | None = None
    if include_security_wrappers and app_config is not None and app_config.read_before_write.enabled:
        from deerflow.agents.middlewares.read_before_write_middleware import (
            ReadBeforeWriteMiddleware,
        )

        read_before_write_middleware = _layer(
            ReadBeforeWriteMiddleware(),
            layer_id="read_before_write",
            phase=MiddlewarePhase.TOOL_CALL_BOUNDARY,
            slot=20,
            why="Read-before-write policy precedes progress and execution.",
        )
        tail.append(read_before_write_middleware)

    tool_progress_middleware: AgentMiddleware | None = None
    if include_security_wrappers and app_config is not None and app_config.tool_progress.enabled:
        from deerflow.agents.middlewares.tool_progress_middleware import (
            ToolProgressMiddleware,
        )

        tool_progress_middleware = _layer(
            ToolProgressMiddleware.from_config(app_config.tool_progress),
            layer_id="tool_progress",
            phase=MiddlewarePhase.TOOL_CALL_BOUNDARY,
            slot=30,
            why="Progress observes the complete guarded tool call.",
        )
        tail.append(tool_progress_middleware)

    if guardrail_middleware is not None:
        guardrail_middleware = _layer(
            guardrail_middleware,
            layer_id="guardrail",
            phase=MiddlewarePhase.TOOL_CALL_BOUNDARY,
            slot=40,
            why="Guardrail admission runs before error stamping.",
        )
        tail.append(guardrail_middleware)

    # Delay this import so importing ``assembly`` directly cannot cycle through
    # the legacy module while it re-exports these builders.
    from deerflow.agents.middlewares.tool_error_handling_middleware import (
        ToolErrorHandlingMiddleware,
    )

    tool_error_middleware = _layer(
        ToolErrorHandlingMiddleware(app_config=app_config),
        layer_id="tool_error_handling",
        phase=MiddlewarePhase.TOOL_CALL_BOUNDARY,
        slot=50,
        why="Innermost tool wrapper stamps stable public failures.",
    )
    tail.append(tool_error_middleware)
    middlewares = [*outer_wrappers, *thread_hooks, *tail]

    _validate_middleware_invariants(
        middlewares,
        (
            _MiddlewareOrderInvariant(
                name="private tool-call boundary",
                registration_order=(
                    host_execution_approval_pause,
                    sandbox_audit_middleware,
                    read_before_write_middleware,
                    tool_progress_middleware,
                    guardrail_middleware,
                    tool_error_middleware,
                ),
            ),
        ),
    )
    validate_middleware_phase_ladder(middlewares)
    validate_middleware_dispatch_constraints(
        middlewares,
        (
            MiddlewareDispatchConstraint(
                name="tool output budget wraps result sanitization",
                hook=MiddlewareHook.WRAP_TOOL_CALL,
                first="tool_output_budget",
                then="tool_result_sanitization",
                why=("Vision evidence must be structurally sanitized and re-bounded before generic output budgeting observes it."),
            ),
            MiddlewareDispatchConstraint(
                name="tool progress wraps tool error handling",
                hook=MiddlewareHook.WRAP_TOOL_CALL,
                first="tool_progress",
                then="tool_error_handling",
                why="Progress must observe the final stamped tool result.",
            ),
            MiddlewareDispatchConstraint(
                name="guardrail precedes tool error handling",
                hook=MiddlewareHook.WRAP_TOOL_CALL,
                first="guardrail",
                then="tool_error_handling",
                why="Denied calls must still receive stable result stamping.",
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
    context_model: object | None = None,
    lazy_init: bool = True,
    deferred_setup: DeferredToolSetup | None = None,
    mcp_routing_middleware: AgentMiddleware | None = None,
    agent_name: str | None = None,
    tool_call_control: AgentMiddleware | None = None,
    context_compaction_observer: object | None = None,
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

    from deerflow.agents.middlewares.summarization_middleware import (
        create_summarization_middleware,
    )

    if app_config.summarization.enabled and context_model is None:
        raise ValueError(
            "Sub-Agent summarization requires its exact context model",
        )
    summarization_middleware = create_summarization_middleware(
        app_config=app_config,
        context_model=context_model,
        context_compaction_observer=context_compaction_observer,
    )
    if summarization_middleware is not None:
        from deerflow.agents.middlewares.durable_context_middleware import (
            DurableContextMiddleware,
        )

        middlewares.append(
            _layer(
                DurableContextMiddleware(
                    skills_container_path=app_config.skills.container_path,
                    skill_file_read_tool_names=(app_config.summarization.skill_file_read_tool_names),
                ),
                layer_id="subagent_durable_context",
                phase=MiddlewarePhase.PRIVATE_CONTEXT,
                slot=10,
                why=("A compacted Sub-Agent summary must be injected into its next model request without entering the Lead context."),
            )
        )
        middlewares.append(
            _layer(
                summarization_middleware,
                layer_id="summarization",
                phase=MiddlewarePhase.COMPACTION,
                slot=10,
                why=("Each Sub-Agent Task compacts its own ephemeral history from the frozen parent Run policy."),
            )
        )

    if model_name is None and app_config.models:
        model_name = app_config.models[0].name

    model_config = app_config.get_model_config(model_name) if model_name else None
    from deerflow.agents.middlewares.view_image_middleware import ViewImageMiddleware

    middlewares.append(
        _layer(
            ViewImageMiddleware(enable_injection=bool(model_config is not None and model_config.supports_vision)),
            layer_id="vision",
            phase=MiddlewarePhase.REQUEST_SHAPING,
            slot=10,
            why="Vision shapes the final subagent model request.",
        )
    )

    if mcp_routing_middleware is not None:
        middlewares.append(
            _layer(
                mcp_routing_middleware,
                layer_id="request_routing_1_McpRoutingMiddleware",
                phase=MiddlewarePhase.REQUEST_SHAPING,
                slot=20,
                why="MCP routing precedes deferred tool filtering.",
            )
        )

    if deferred_setup is not None and deferred_setup.deferred_names:
        from deerflow.agents.middlewares.deferred_tool_filter_middleware import (
            DeferredToolFilterMiddleware,
        )

        middlewares.append(
            _layer(
                DeferredToolFilterMiddleware(
                    deferred_setup.deferred_names,
                    deferred_setup.catalog_hash,
                ),
                layer_id="request_routing_2_DeferredToolFilterMiddleware",
                phase=MiddlewarePhase.REQUEST_SHAPING,
                slot=30,
                why="Deferred filtering consumes MCP routing decisions.",
            )
        )
        from deerflow.agents.middlewares.mcp_routing_middleware import (
            assert_mcp_routing_before_deferred_filter,
        )

        assert_mcp_routing_before_deferred_filter(middlewares)

    token_budget_middleware: AgentMiddleware | None = None
    token_budget_config = app_config.subagents.get_token_budget_for(agent_name) if agent_name is not None else app_config.subagents.token_budget
    if token_budget_config.enabled:
        from deerflow.agents.middlewares.token_budget_middleware import (
            TokenBudgetMiddleware,
        )

        token_budget_middleware = TokenBudgetMiddleware.from_config(token_budget_config)

    safety_middleware: AgentMiddleware | None = None
    safety_config = app_config.safety_finish_reason
    if safety_config.enabled:
        from deerflow.agents.middlewares.safety_finish_reason_middleware import (
            SafetyFinishReasonMiddleware,
        )

        safety_middleware = SafetyFinishReasonMiddleware.from_config(safety_config)

    host_execution_batch_barrier = build_host_execution_batch_barrier(
        app_config=app_config,
    )
    _append_tool_call_arbitration_band(
        middlewares,
        tool_call_control=tool_call_control,
        host_execution_batch_barrier=host_execution_batch_barrier,
        subagent=None,
        token_budget=token_budget_middleware,
        custom=(),
        safety=safety_middleware,
    )

    _validate_middleware_invariants(
        middlewares,
        (
            _MiddlewareOrderInvariant(
                name="protected subagent tool-call arbitration band",
                registration_order=(
                    tool_call_control,
                    host_execution_batch_barrier,
                    token_budget_middleware,
                    safety_middleware,
                ),
                reverse_hook="after_model",
            ),
        ),
    )
    validate_middleware_phase_ladder(middlewares)
    validate_middleware_dispatch_constraints(
        middlewares,
        (
            MiddlewareDispatchConstraint(
                name="subagent safety cleanup before token budget arbitration",
                hook=MiddlewareHook.AFTER_MODEL,
                first="safety_finish_reason",
                then="token_budget",
                why="Safety must clean the response before token arbitration.",
            ),
            MiddlewareDispatchConstraint(
                name="subagent token budget before host batch serialization",
                hook=MiddlewareHook.AFTER_MODEL,
                first="token_budget",
                then="host_execution_batch_barrier",
                why="Token accounting must precede approval-mode batch serialization.",
            ),
            MiddlewareDispatchConstraint(
                name="subagent host batch serialization before tool-call control",
                hook=MiddlewareHook.AFTER_MODEL,
                first="host_execution_batch_barrier",
                then="tool_call_control",
                why="Tool-call control must arbitrate the batch that can reach ToolNode.",
            ),
        ),
    )
    return middlewares


__all__ = [
    "assemble_agent_middlewares",
    "build_host_execution_batch_barrier",
    "build_lead_runtime_middlewares",
    "build_runtime_middlewares",
    "build_sandbox_infrastructure",
    "build_subagent_runtime_middlewares",
]
