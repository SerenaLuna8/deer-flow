"""GuardrailMiddleware - evaluates tool calls against a GuardrailProvider before execution."""

import copy
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deerflow.guardrails.provider import (
    GUARDRAIL_ATTRIBUTION_CONTEXT_KEY,
    GuardrailDecision,
    GuardrailProvider,
    GuardrailReason,
    GuardrailRequest,
    copy_guardrail_attribution,
)
from deerflow.sandbox.sandbox import (
    AuthorizationRevoked,
    check_authorization_boundary,
)

logger = logging.getLogger(__name__)

_REASON_MESSAGE_LIMIT = 500
_IDENTIFIER_LIMIT = 128
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_PRIVATE_ATTRIBUTION_TEXT_FIELDS = (
    "user_id",
    "user_role",
    "thread_id",
    "run_id",
)
_PRIVATE_AUTHZ_FIELDS = frozenset(
    {
        "project_id",
        "project_role",
        "capabilities",
    }
)


def _bounded_identifier(
    value: object,
    *,
    fallback: str | None,
) -> str | None:
    if not isinstance(value, str):
        return fallback
    selected = value[:_IDENTIFIER_LIMIT]
    return selected if _SAFE_IDENTIFIER.fullmatch(selected) else fallback


def _bounded_reason_message(value: str) -> str:
    from deerflow.agents.middlewares.input_sanitization_middleware import (
        neutralize_untrusted_tags,
    )

    return neutralize_untrusted_tags(value)[:_REASON_MESSAGE_LIMIT]


class GuardrailMiddleware(AgentMiddleware[AgentState]):
    """Evaluate tool calls against a GuardrailProvider before execution.

    Denied calls return an error ToolMessage so the agent can adapt.
    If the provider raises, behavior depends on fail_closed:
      - True (default): block the call
      - False: allow it through with a warning
    """

    def __init__(self, provider: GuardrailProvider, *, fail_closed: bool = True, passport: str | None = None):
        self.provider = provider
        self.fail_closed = fail_closed
        self.passport = passport

    @staticmethod
    def _resolve_context(request: ToolCallRequest) -> dict:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None) if runtime is not None else None
        return context if isinstance(context, dict) else {}

    @staticmethod
    def _validate_private_attribution(context: dict) -> None:
        raw = context.get(GUARDRAIL_ATTRIBUTION_CONTEXT_KEY)
        if not isinstance(raw, Mapping):
            raise AuthorizationRevoked
        if any(not isinstance(raw.get(field), str) or not raw[field] for field in _PRIVATE_ATTRIBUTION_TEXT_FIELDS):
            raise AuthorizationRevoked
        if type(raw.get("is_subagent")) is not bool:
            raise AuthorizationRevoked

        attributes = raw.get("authz_attributes")
        if (
            not isinstance(attributes, Mapping)
            or set(attributes) != _PRIVATE_AUTHZ_FIELDS
            or not isinstance(attributes.get("project_id"), str)
            or not attributes["project_id"]
            or not isinstance(attributes.get("project_role"), str)
            or not attributes["project_role"]
            or attributes["project_role"] != raw["user_role"]
        ):
            raise AuthorizationRevoked
        capabilities = attributes.get("capabilities")
        if not isinstance(capabilities, tuple) or not all(isinstance(capability, str) and capability for capability in capabilities):
            raise AuthorizationRevoked

        private_scope = context.get("private_scope")
        scope_project_id = getattr(private_scope, "project_id", None)
        scope_owner_user_id = getattr(private_scope, "owner_user_id", None)
        if str(scope_project_id) != attributes["project_id"] or str(scope_owner_user_id) != raw["user_id"]:
            raise AuthorizationRevoked

    @classmethod
    async def _private_authorization_preflight(cls, context: dict) -> None:
        cls._validate_private_attribution(context)
        boundary = context.get("__authorization_boundary")
        if not callable(getattr(boundary, "before_read_only_tool_call", None)):
            raise AuthorizationRevoked
        await check_authorization_boundary(
            context,
            "before_read_only_tool_call",
        )

    @staticmethod
    def _normalize_decision(value: object) -> GuardrailDecision:
        if not isinstance(value, GuardrailDecision) or type(value.allow) is not bool:
            raise TypeError("invalid guardrail decision")
        if not isinstance(value.reasons, list):
            raise TypeError("invalid guardrail reasons")
        reasons: list[GuardrailReason] = []
        for reason in value.reasons:
            if not isinstance(reason, GuardrailReason) or not isinstance(reason.code, str) or not isinstance(reason.message, str):
                raise TypeError("invalid guardrail reason")
            reasons.append(
                GuardrailReason(
                    code=_bounded_identifier(
                        reason.code,
                        fallback="oap.denied",
                    )
                    or "oap.denied",
                    message=_bounded_reason_message(reason.message),
                )
            )
        if value.policy_id is not None and not isinstance(
            value.policy_id,
            str,
        ):
            raise TypeError("invalid guardrail policy id")
        return GuardrailDecision(
            allow=value.allow,
            reasons=reasons,
            policy_id=_bounded_identifier(
                value.policy_id,
                fallback=None,
            ),
        )

    def _build_request(self, request: ToolCallRequest, context: dict) -> GuardrailRequest:
        # A private Run must use only the closed Worker-issued carrier. Missing
        # attribution fails closed to empty values; raw context fields are never
        # a fallback because those names can also occur in graph state.
        if "private_scope" in context:
            attribution = copy_guardrail_attribution(context.get(GUARDRAIL_ATTRIBUTION_CONTEXT_KEY)) or {}
        else:
            attribution = context
        raw_attributes = attribution.get("authz_attributes")
        authz_attributes = dict(raw_attributes) if isinstance(raw_attributes, dict) else {}
        raw_tool_input = request.tool_call.get("args", {})
        if not isinstance(raw_tool_input, dict):
            raise TypeError("guardrail tool input must be a mapping")
        return GuardrailRequest(
            tool_name=str(request.tool_call.get("name", "")),
            tool_input=copy.deepcopy(raw_tool_input),
            agent_id=self.passport,
            thread_id=attribution.get("thread_id"),
            is_subagent=attribution.get("is_subagent") is True,
            timestamp=datetime.now(UTC).isoformat(),
            user_id=attribution.get("user_id"),
            user_role=attribution.get("user_role"),
            oauth_provider=attribution.get("oauth_provider"),
            oauth_id=attribution.get("oauth_id"),
            run_id=attribution.get("run_id"),
            tool_call_id=request.tool_call.get("id"),
            channel_user_id=attribution.get("channel_user_id"),
            is_internal=attribution.get("is_internal") is True,
            authz_attributes=authz_attributes,
        )

    def _build_denied_message(self, request: ToolCallRequest, decision: GuardrailDecision) -> ToolMessage:
        tool_name = (
            _bounded_identifier(
                request.tool_call.get("name"),
                fallback="unknown_tool",
            )
            or "unknown_tool"
        )
        tool_call_id = (
            _bounded_identifier(
                request.tool_call.get("id"),
                fallback="missing_id",
            )
            or "missing_id"
        )
        reason_text = decision.reasons[0].message if decision.reasons else "blocked by guardrail policy"
        reason_code = decision.reasons[0].code if decision.reasons else "oap.denied"
        return ToolMessage(
            content=f"Guardrail denied: tool '{tool_name}' was blocked ({reason_code}). Reason: {reason_text}. Choose an alternative approach.",
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    def _record_guardrail_event(
        self,
        context: dict,
        guardrail_request: GuardrailRequest,
        decision: GuardrailDecision,
        *,
        action: str,
        provider_error: bool,
    ) -> None:
        """Persist a security-relevant guardrail decision to RunJournal.

        This follows the optional-Journal pattern used by existing middleware:
        audit persistence is best-effort and must never change tool execution
        behavior. Runtimes without ``__run_journal`` (including embedded and
        subagent execution) skip persistence.
        """
        journal = context.get("__run_journal")
        if journal is None:
            return

        reason_codes = [reason.code for reason in decision.reasons if reason.code]
        reason_messages = [reason.message for reason in decision.reasons if reason.message]

        changes = {
            "tool_name": _bounded_identifier(
                guardrail_request.tool_name,
                fallback="unknown_tool",
            ),
            "tool_call_id": _bounded_identifier(
                guardrail_request.tool_call_id,
                fallback=None,
            ),
            "agent_id": _bounded_identifier(
                guardrail_request.agent_id,
                fallback=None,
            ),
            # Native subagents do not currently inherit __run_journal; custom
            # runtimes may still provide one with subagent attribution.
            "is_subagent": guardrail_request.is_subagent,
            "user_role": _bounded_identifier(
                guardrail_request.user_role,
                fallback=None,
            ),
            "allow": decision.allow,
            "policy_id": decision.policy_id,
            "reason_codes": reason_codes,
            "reason_messages": reason_messages,
            "fail_closed": self.fail_closed,
            "provider_error": provider_error,
        }

        try:
            journal.record_middleware(
                tag="guardrail",
                name=type(self).__name__,
                hook="wrap_tool_call",
                action=action,
                changes=changes,
            )
        except Exception:  # noqa: BLE001
            logger.warning("security_event=guardrail_journal_write_failed")

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        context = self._resolve_context(request)
        if "private_scope" in context:
            # Private authorization is asynchronous and database-backed. A
            # synchronous private tool path cannot prove current authority.
            raise AuthorizationRevoked
        gr: GuardrailRequest | None = None
        try:
            gr = self._build_request(request, context)
            decision = self._normalize_decision(self.provider.evaluate(gr))
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception:
            if gr is None:
                gr = GuardrailRequest(
                    tool_name="unknown_tool",
                    tool_input={},
                )
            logger.error(
                "security_event=guardrail_provider_error mode=sync disposition=%s",
                "fail_closed" if self.fail_closed else "fail_open",
            )
            if self.fail_closed:
                decision = GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.evaluator_error", message="guardrail provider error (fail-closed)")])
                self._record_guardrail_event(
                    context,
                    gr,
                    decision,
                    action="deny_tool_call",
                    provider_error=True,
                )
                return self._build_denied_message(request, decision)
            else:
                decision = GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.evaluator_error", message="guardrail provider error (fail-open)")])
                self._record_guardrail_event(
                    context,
                    gr,
                    decision,
                    action="allow_tool_call_after_provider_error",
                    provider_error=True,
                )
                return handler(request)
        if not decision.allow:
            logger.warning("security_event=guardrail_tool_denied")
            self._record_guardrail_event(
                context,
                gr,
                decision,
                action="deny_tool_call",
                provider_error=False,
            )
            return self._build_denied_message(request, decision)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        context = self._resolve_context(request)
        if "private_scope" in context:
            await self._private_authorization_preflight(context)
        gr: GuardrailRequest | None = None
        try:
            gr = self._build_request(request, context)
            decision = self._normalize_decision(await self.provider.aevaluate(gr))
        except GraphBubbleUp:
            # Preserve LangGraph control-flow signals (interrupt/pause/resume).
            raise
        except Exception:
            if gr is None:
                gr = GuardrailRequest(
                    tool_name="unknown_tool",
                    tool_input={},
                )
            logger.error(
                "security_event=guardrail_provider_error mode=async disposition=%s",
                "fail_closed" if self.fail_closed else "fail_open",
            )
            if self.fail_closed:
                decision = GuardrailDecision(allow=False, reasons=[GuardrailReason(code="oap.evaluator_error", message="guardrail provider error (fail-closed)")])
                self._record_guardrail_event(
                    context,
                    gr,
                    decision,
                    action="deny_tool_call",
                    provider_error=True,
                )
                return self._build_denied_message(request, decision)
            else:
                decision = GuardrailDecision(allow=True, reasons=[GuardrailReason(code="oap.evaluator_error", message="guardrail provider error (fail-open)")])
                self._record_guardrail_event(
                    context,
                    gr,
                    decision,
                    action="allow_tool_call_after_provider_error",
                    provider_error=True,
                )
                return await handler(request)
        if not decision.allow:
            logger.warning("security_event=guardrail_tool_denied")
            self._record_guardrail_event(
                context,
                gr,
                decision,
                action="deny_tool_call",
                provider_error=False,
            )
            return self._build_denied_message(request, decision)
        return await handler(request)
