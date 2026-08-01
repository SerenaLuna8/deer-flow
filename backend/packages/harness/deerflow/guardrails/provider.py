"""GuardrailProvider protocol and data structures for pre-tool-call authorization."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

GUARDRAIL_ATTRIBUTION_CONTEXT_KEY = "__guardrail_attribution"
_GUARDRAIL_ATTRIBUTION_TEXT_FIELDS = frozenset(
    {
        "user_id",
        "user_role",
        "thread_id",
        "run_id",
        "oauth_provider",
        "oauth_id",
        "channel_user_id",
    }
)


def copy_guardrail_attribution(
    value: object,
    *,
    is_subagent: bool | None = None,
) -> dict[str, Any] | None:
    """Copy only the closed attribution carrier understood by Guardrail."""

    if not isinstance(value, Mapping):
        return None
    result = {key: field_value for key in _GUARDRAIL_ATTRIBUTION_TEXT_FIELDS if isinstance((field_value := value.get(key)), str)}
    if value.get("is_subagent") is True:
        result["is_subagent"] = True
    elif "is_subagent" in value:
        result["is_subagent"] = False
    if value.get("is_internal") is True:
        result["is_internal"] = True
    elif "is_internal" in value:
        result["is_internal"] = False
    attributes = value.get("authz_attributes")
    if isinstance(attributes, Mapping):
        result["authz_attributes"] = copy.deepcopy({str(key): item for key, item in attributes.items() if isinstance(key, str)})
    if is_subagent is not None:
        result["is_subagent"] = is_subagent
    return result


@dataclass
class GuardrailRequest:
    """Context passed to the provider for each tool call."""

    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None = None
    thread_id: str | None = None
    is_subagent: bool = False
    timestamp: str = ""
    user_id: str | None = None
    user_role: str | None = None
    oauth_provider: str | None = None
    oauth_id: str | None = None
    run_id: str | None = None
    tool_call_id: str | None = None
    channel_user_id: str | None = None
    is_internal: bool = False
    authz_attributes: dict[str, Any] = field(default_factory=dict)


@dataclass
class GuardrailReason:
    """Structured reason for an allow/deny decision (OAP reason object)."""

    code: str
    message: str = ""


@dataclass
class GuardrailDecision:
    """Provider's allow/deny verdict (aligned with OAP Decision object)."""

    allow: bool
    reasons: list[GuardrailReason] = field(default_factory=list)
    policy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GuardrailProvider(Protocol):
    """Contract for pluggable tool-call authorization.

    Any class with these methods works - no base class required.
    Providers are loaded by class path via resolve_variable(),
    the same mechanism DeerFlow uses for models, tools, and sandbox.
    """

    name: str

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """Evaluate whether a tool call should proceed."""
        ...

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        """Async variant."""
        ...
