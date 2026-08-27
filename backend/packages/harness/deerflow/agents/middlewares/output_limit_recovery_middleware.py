"""Recover one provider-truncated lead response without replaying tools.

Raw response classification happens in ``wrap_model_call`` before later
``after_model`` middleware can normalize finish metadata or tool intent.  The
classification is checkpointed in a private state channel; the final routing
decision runs from ``after_model`` only after usage and guardrail accounting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import Annotated, Any, NotRequired, TypedDict, override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelCallResult,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
    hook_config,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.runtime import Runtime
from langgraph.types import Command

from deerflow.agents.middlewares.token_budget_middleware import (
    OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY,
)
from deerflow.error_codes import PublicRunError, PublicRunErrorCode

OUTPUT_LIMIT_RECOVERY_STATE_KEY = "output_limit_recovery"
_STATE_VERSION = 1
_LIMIT_REASONS = frozenset(
    {
        "length",
        "max_completion_tokens",
        "max_output_tokens",
        "max_token",
        "max_tokens",
        "token_limit",
    }
)
_TEXT_BLOCK_TYPES = frozenset({"text", "output_text"})
_REASONING_BLOCK_TYPES = frozenset({"reasoning", "thinking"})
_RECOVERY_PROMPT = (
    "The previous assistant response reached the provider output limit. "
    "Using the original request and any visible partial answer, produce a concise, "
    "complete final answer now. Do not claim to continue hidden reasoning, do not "
    "repeat completed material, and do not call tools."
)


class _RecoveryFacts(TypedDict):
    version: int
    run_id: str
    phase: str
    limit_hit: bool
    safe: bool
    visible: bool


class OutputLimitRecoveryState(AgentState):
    output_limit_recovery: NotRequired[Annotated[_RecoveryFacts | None, PrivateStateAttr]]


def _run_id(runtime: Runtime) -> str | None:
    context = getattr(runtime, "context", None)
    if not isinstance(context, Mapping):
        return None
    value = context.get("run_id")
    return value if isinstance(value, str) and value else None


def _normalized_reason(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return normalized if normalized in _LIMIT_REASONS else None


def _mapping_reports_limit(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    for key in ("finish_reason", "stop_reason", "done_reason", "reason"):
        if _normalized_reason(value.get(key)) is not None:
            return True
    incomplete = value.get("incomplete_details")
    if isinstance(incomplete, Mapping) and _normalized_reason(incomplete.get("reason")):
        return True
    return False


def message_reports_output_limit(message: AIMessage) -> bool:
    """Return whether provider metadata reports an output-token limit.

    This classifier is intentionally side-effect free so specialized runtimes
    that cannot safely use the generic plain-text retry can preserve the same
    closed provider-reason vocabulary while choosing a fail-closed policy.
    """

    metadata = getattr(message, "response_metadata", {}) or {}
    additional = getattr(message, "additional_kwargs", {}) or {}
    if _mapping_reports_limit(metadata) or _mapping_reports_limit(additional):
        return True
    return isinstance(metadata, Mapping) and str(metadata.get("status", "")).lower() == "incomplete" and _mapping_reports_limit(metadata.get("incomplete_details"))


def _has_tool_intent(message: AIMessage) -> bool:
    if message.tool_calls or getattr(message, "invalid_tool_calls", None):
        return True
    additional = getattr(message, "additional_kwargs", {}) or {}
    if isinstance(additional, Mapping) and (additional.get("tool_calls") or additional.get("function_call")):
        return True
    metadata = getattr(message, "response_metadata", {}) or {}
    if not isinstance(metadata, Mapping):
        return False
    reason = str(metadata.get("finish_reason", "")).strip().lower()
    return reason in {"tool_calls", "function_call"}


def _allows_plain_response(request: ModelRequest) -> bool:
    choices = [request.tool_choice]
    if isinstance(request.model_settings, Mapping):
        choices.append(request.model_settings.get("tool_choice"))
    for choice in choices:
        if choice is None:
            continue
        if isinstance(choice, str) and choice.strip().lower() in {
            "auto",
            "none",
        }:
            continue
        return False
    return True


def _content_is_plain_or_reasoning(content: object) -> bool:
    if content is None or isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, str):
            continue
        if not isinstance(block, Mapping):
            return False
        block_type = str(block.get("type", "")).lower()
        if block_type not in _TEXT_BLOCK_TYPES | _REASONING_BLOCK_TYPES:
            return False
    return True


def _has_visible_text(message: AIMessage) -> bool:
    content = message.content
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, str) and block.strip():
            return True
        if isinstance(block, Mapping) and str(block.get("type", "")).lower() in _TEXT_BLOCK_TYPES and isinstance(block.get("text"), str) and block["text"].strip():
            return True
    return False


def _model_response(result: ModelCallResult) -> ModelResponse:
    if isinstance(result, ExtendedModelResponse):
        return result.model_response
    if isinstance(result, AIMessage):
        return ModelResponse(result=[result])
    return result


def _attach_facts(
    result: ModelCallResult,
    facts: _RecoveryFacts,
) -> ExtendedModelResponse:
    response = _model_response(result)
    update = {OUTPUT_LIMIT_RECOVERY_STATE_KEY: facts}
    if not isinstance(result, ExtendedModelResponse) or result.command is None:
        return ExtendedModelResponse(response, Command(update=update))
    existing = result.command
    if existing.update is not None and not isinstance(existing.update, Mapping):
        raise RuntimeError("Cannot merge output-limit recovery state command")
    merged = {**dict(existing.update or {}), **update}
    return ExtendedModelResponse(response, replace(existing, update=merged))


def _strip_truncated_reasoning(message: AIMessage) -> AIMessage:
    additional = dict(message.additional_kwargs or {})
    additional.pop("reasoning", None)
    additional.pop("reasoning_content", None)
    content = message.content
    if isinstance(content, list):
        content = [block for block in content if not (isinstance(block, Mapping) and str(block.get("type", "")).lower() in _REASONING_BLOCK_TYPES)]
    return message.model_copy(update={"additional_kwargs": additional, "content": content})


def _prepare_recovery_messages(messages: list[Any]) -> list[Any]:
    prepared = list(messages)
    for index in range(len(prepared) - 1, -1, -1):
        message = prepared[index]
        if isinstance(message, AIMessage):
            prepared[index] = _strip_truncated_reasoning(message)
            break
    prepared.append(
        HumanMessage(
            content=_RECOVERY_PROMPT,
            name="output_limit_recovery",
            additional_kwargs={"hide_from_ui": True},
        )
    )
    return prepared


class OutputLimitRecoveryMiddleware(AgentMiddleware[OutputLimitRecoveryState]):
    """Retry one safe truncated response with an ephemeral constrained request."""

    state_schema = OutputLimitRecoveryState

    def __init__(
        self,
        *,
        recovery_model: BaseChatModel,
        budget_hard_stopped: Callable[[str | None], bool] | None = None,
    ) -> None:
        super().__init__()
        self._recovery_model = recovery_model
        self._budget_hard_stopped = budget_hard_stopped

    @staticmethod
    def _facts(
        request: ModelRequest,
        response: ModelResponse,
        *,
        phase: str,
        run_id: str,
    ) -> _RecoveryFacts:
        ai_messages = [item for item in response.result if isinstance(item, AIMessage)]
        limit_hit = any(message_reports_output_limit(item) for item in ai_messages)
        safe = (
            request.response_format is None
            and _allows_plain_response(request)
            and response.structured_response is None
            and bool(ai_messages)
            and len(ai_messages) == len(response.result)
            and all(not _has_tool_intent(item) and _content_is_plain_or_reasoning(item.content) for item in ai_messages)
        )
        visible = any(_has_visible_text(item) for item in ai_messages)
        return {
            "version": _STATE_VERSION,
            "run_id": run_id,
            "phase": phase,
            "limit_hit": limit_hit,
            "safe": safe,
            "visible": visible,
        }

    @staticmethod
    def _pending_for_request(request: ModelRequest, run_id: str) -> bool:
        state = request.state or {}
        facts = state.get(OUTPUT_LIMIT_RECOVERY_STATE_KEY)
        return isinstance(facts, Mapping) and facts.get("version") == _STATE_VERSION and facts.get("run_id") == run_id and facts.get("phase") == "pending"

    def _prepare_request(self, request: ModelRequest) -> ModelRequest:
        return request.override(
            model=self._recovery_model,
            messages=_prepare_recovery_messages(request.messages),
            tools=[],
            tool_choice=None,
            response_format=None,
            model_settings={},
        )

    def _budget_hard_stopped_for_request(
        self,
        request: ModelRequest,
        run_id: str,
    ) -> bool:
        hard_stop = (request.state or {}).get(
            OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY,
        )
        if isinstance(hard_stop, Mapping) and hard_stop.get("run_id") == run_id:
            return True
        return self._budget_hard_stopped is not None and self._budget_hard_stopped(
            run_id,
        )

    def _record_terminal_output_limit(
        self,
        request: ModelRequest,
        facts: _RecoveryFacts,
        *,
        recovery: bool,
        run_id: str,
    ) -> None:
        if self._budget_hard_stopped_for_request(request, run_id):
            return
        if recovery:
            terminal = facts["limit_hit"] or not facts["safe"] or not facts["visible"]
        else:
            terminal = facts["limit_hit"] and not facts["safe"]
        if not terminal:
            return
        # This middleware is imported by runtime.serialization while the
        # deerflow.runtime package is still initializing. Resolve the
        # server-owned context contract lazily to avoid a package cycle.
        from deerflow.runtime.context_keys import RuntimeContextKeys
        from deerflow.runtime.runs.execution_contracts import (
            RunSemanticStopRecorder,
        )

        context = getattr(request.runtime, "context", None)
        recorder = context.get(RuntimeContextKeys.RUN_SEMANTIC_STOP_RECORDER) if isinstance(context, Mapping) else None
        if isinstance(recorder, RunSemanticStopRecorder):
            # The model handler has returned at this point. RunJournal's async
            # on_llm_end durable barrier therefore completed before this
            # receipt becomes visible to cancellation arbitration.
            recorder.record("model_output_limit")

    def _wrap_result(
        self,
        request: ModelRequest,
        result: ModelCallResult,
        *,
        recovery: bool,
        run_id: str,
    ) -> ModelCallResult:
        response = _model_response(result)
        facts = self._facts(
            request,
            response,
            phase="recovery_observed" if recovery else "initial_observed",
            run_id=run_id,
        )
        self._record_terminal_output_limit(
            request,
            facts,
            recovery=recovery,
            run_id=run_id,
        )
        if recovery or facts["limit_hit"]:
            return _attach_facts(result, facts)
        return result

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        run_id = _run_id(request.runtime)
        if run_id is None:
            return handler(request)
        recovery = self._pending_for_request(request, run_id)
        effective_request = self._prepare_request(request) if recovery else request
        result = handler(effective_request)
        return self._wrap_result(
            effective_request,
            result,
            recovery=recovery,
            run_id=run_id,
        )

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        run_id = _run_id(request.runtime)
        if run_id is None:
            return await handler(request)
        recovery = self._pending_for_request(request, run_id)
        effective_request = self._prepare_request(request) if recovery else request
        result = await handler(effective_request)
        return self._wrap_result(
            effective_request,
            result,
            recovery=recovery,
            run_id=run_id,
        )

    @hook_config(can_jump_to=["model", "end"])
    @override
    def after_model(
        self,
        state: OutputLimitRecoveryState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        run_id = _run_id(runtime)
        facts = state.get(OUTPUT_LIMIT_RECOVERY_STATE_KEY)
        if run_id is None or not isinstance(facts, Mapping) or facts.get("version") != _STATE_VERSION or facts.get("run_id") != run_id:
            return None

        phase = facts.get("phase")
        if phase == "pending":
            messages = state.get("messages") or []
            last_ai = next(
                (message for message in reversed(messages) if isinstance(message, AIMessage)),
                None,
            )
            if last_ai is not None and (last_ai.additional_kwargs or {}).get("deerflow_error_fallback") is True:
                return {
                    OUTPUT_LIMIT_RECOVERY_STATE_KEY: None,
                    "jump_to": "end",
                }
            # An outer model wrapper returned without allowing raw recovery
            # classification. Never let Todo/router turn that unobserved
            # response into a third model call.
            raise PublicRunError(PublicRunErrorCode.MODEL_OUTPUT_LIMIT)

        if phase == "initial_observed":
            hard_stop = state.get(OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY)
            durable_hard_stop = isinstance(hard_stop, Mapping) and hard_stop.get("run_id") == run_id
            if durable_hard_stop or (self._budget_hard_stopped is not None and self._budget_hard_stopped(run_id)):
                return {
                    OUTPUT_LIMIT_RECOVERY_STATE_KEY: None,
                    OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY: None,
                    "jump_to": "end",
                }
            if not facts.get("limit_hit"):
                return None
            if not facts.get("safe"):
                raise PublicRunError(PublicRunErrorCode.MODEL_OUTPUT_LIMIT)
            return {
                OUTPUT_LIMIT_RECOVERY_STATE_KEY: {
                    **dict(facts),
                    "phase": "pending",
                },
                "jump_to": "model",
            }

        if phase == "recovery_observed":
            if facts.get("limit_hit") or not facts.get("safe") or not facts.get("visible"):
                raise PublicRunError(PublicRunErrorCode.MODEL_OUTPUT_LIMIT)
            return {
                OUTPUT_LIMIT_RECOVERY_STATE_KEY: None,
                "jump_to": "end",
            }
        return None

    @hook_config(can_jump_to=["model", "end"])
    @override
    async def aafter_model(
        self,
        state: OutputLimitRecoveryState,
        runtime: Runtime,
    ) -> dict[str, Any] | None:
        return self.after_model(state, runtime)


__all__ = [
    "OUTPUT_LIMIT_BUDGET_HARD_STOP_STATE_KEY",
    "OUTPUT_LIMIT_RECOVERY_STATE_KEY",
    "OutputLimitRecoveryMiddleware",
    "OutputLimitRecoveryState",
    "message_reports_output_limit",
]
