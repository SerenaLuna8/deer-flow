from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref

type ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]

RUN_EXECUTION_PROFILE_KWARG = "__run_execution_profile"

_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high"})
_REQUESTED_KEYS = frozenset({"model_name", "thinking_enabled", "reasoning_effort"})
_EFFECTIVE_KEYS = frozenset(
    {
        "model_name",
        "thinking_enabled",
        "reasoning_effort",
        "supports_vision",
    }
)


class RunExecutionProfileError(Exception):
    """Internal fail-closed execution-profile validation marker."""


class RunModelSelectionLocked(RunExecutionProfileError):
    """The exact admitted Agent model cannot be overridden."""


class RunSelectedModelUnavailable(RunExecutionProfileError):
    """An explicitly selected model UUID is not active and admissible."""


class RunExecutionProfileUnsupported(RunExecutionProfileError):
    """The selected exact model cannot honor the requested profile."""


def _validate_model_name(value: str | None) -> None:
    if value is not None and exact_model_ref(value) is None:
        raise TypeError("model_name must be an exact model UUID")


def _validate_reasoning_effort(value: str | None) -> None:
    if value is not None and (type(value) is not str or value not in _REASONING_EFFORTS):
        raise TypeError("reasoning_effort is invalid")


@dataclass(frozen=True, slots=True)
class RequestedRunExecutionProfile:
    """Strict, non-authoritative user selection awaiting server admission."""

    model_name: str | None = None
    thinking_enabled: bool | None = None
    reasoning_effort: ReasoningEffort | None = None

    def __post_init__(self) -> None:
        _validate_model_name(self.model_name)
        if self.thinking_enabled is not None and type(self.thinking_enabled) is not bool:
            raise TypeError("thinking_enabled must be a boolean")
        _validate_reasoning_effort(self.reasoning_effort)

    def as_dict(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
        }


@dataclass(frozen=True, slots=True)
class EffectiveRunExecutionProfile:
    """Exact, server-admitted profile consumed at the Worker boundary."""

    model_name: str
    thinking_enabled: bool
    reasoning_effort: ReasoningEffort | None
    supports_vision: bool

    def __post_init__(self) -> None:
        _validate_model_name(self.model_name)
        if type(self.thinking_enabled) is not bool:
            raise TypeError("thinking_enabled must be a boolean")
        _validate_reasoning_effort(self.reasoning_effort)
        if type(self.supports_vision) is not bool:
            raise TypeError("supports_vision must be a boolean")
        if not self.thinking_enabled and self.reasoning_effort not in {None, "none"}:
            raise TypeError(
                "disabled thinking only permits no reasoning effort",
            )
        if self.thinking_enabled and self.reasoning_effort == "none":
            raise TypeError("enabled thinking requires a nonzero reasoning effort")

    def as_dict(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
            "supports_vision": self.supports_vision,
        }


def selected_run_model_ref(
    agent_model_ref: str,
    requested: RequestedRunExecutionProfile,
) -> str:
    """Return the only model ref that admission may freeze for the lead Agent."""

    if type(agent_model_ref) is not str or not agent_model_ref or type(requested) is not RequestedRunExecutionProfile:
        raise RunExecutionProfileUnsupported
    if agent_model_ref == DEFAULT_MODEL_REF:
        return requested.model_name or DEFAULT_MODEL_REF
    if requested.model_name is not None and requested.model_name != agent_model_ref:
        raise RunModelSelectionLocked
    return agent_model_ref


def resolve_admitted_run_execution_profile(
    *,
    requested: RequestedRunExecutionProfile,
    model_ref: str,
    supports_thinking: bool,
    supports_reasoning_effort: bool,
    supports_vision: bool,
    agent_thinking_enabled: bool | None,
    agent_reasoning_effort: ReasoningEffort | None,
) -> EffectiveRunExecutionProfile:
    """Resolve request > exact Agent settings > model capability without fallback."""

    if type(requested) is not RequestedRunExecutionProfile:
        raise RunExecutionProfileUnsupported
    try:
        _validate_model_name(model_ref)
        if model_ref is None:
            raise TypeError
        if any(
            type(value) is not bool
            for value in (
                supports_thinking,
                supports_reasoning_effort,
                supports_vision,
            )
        ):
            raise TypeError
        if agent_thinking_enabled is not None and type(agent_thinking_enabled) is not bool:
            raise TypeError
        _validate_reasoning_effort(agent_reasoning_effort)
    except TypeError:
        raise RunExecutionProfileUnsupported from None

    if requested.thinking_enabled is not None:
        thinking_enabled = requested.thinking_enabled
    elif agent_thinking_enabled is not None:
        thinking_enabled = agent_thinking_enabled
    else:
        # Capability-aware default preserves thinking-by-default for models
        # that support it without making every text-only model inadmissible.
        thinking_enabled = supports_thinking

    if thinking_enabled and not supports_thinking:
        raise RunExecutionProfileUnsupported

    reasoning_effort = requested.reasoning_effort if requested.reasoning_effort is not None else agent_reasoning_effort
    if not thinking_enabled:
        if reasoning_effort not in {None, "none"}:
            raise RunExecutionProfileUnsupported
        # GPT-5.6 defaults to medium when effort is omitted. Preserve Flash as
        # a real disabled-reasoning mode by freezing the provider's explicit
        # ``none`` value whenever the exact model exposes effort controls.
        reasoning_effort = "none" if supports_reasoning_effort else None
    elif reasoning_effort == "none":
        raise RunExecutionProfileUnsupported
    elif reasoning_effort is not None and not supports_reasoning_effort:
        raise RunExecutionProfileUnsupported

    return EffectiveRunExecutionProfile(
        model_name=model_ref,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        supports_vision=supports_vision,
    )


def persisted_run_execution_profile(
    requested: RequestedRunExecutionProfile,
    effective: EffectiveRunExecutionProfile,
) -> dict[str, object]:
    if type(requested) is not RequestedRunExecutionProfile or type(effective) is not EffectiveRunExecutionProfile:
        raise TypeError("exact execution profile types are required")
    return {
        "requested": requested.as_dict(),
        "effective": effective.as_dict(),
    }


def parse_persisted_run_execution_profile(
    value: object,
) -> tuple[RequestedRunExecutionProfile, EffectiveRunExecutionProfile]:
    if not isinstance(value, Mapping) or set(value) != {"requested", "effective"}:
        raise RunExecutionProfileUnsupported
    requested_value = value.get("requested")
    effective_value = value.get("effective")
    if not isinstance(requested_value, Mapping) or set(requested_value) != _REQUESTED_KEYS or not isinstance(effective_value, Mapping) or set(effective_value) != _EFFECTIVE_KEYS:
        raise RunExecutionProfileUnsupported
    try:
        requested = RequestedRunExecutionProfile(
            model_name=requested_value.get("model_name"),
            thinking_enabled=requested_value.get("thinking_enabled"),
            reasoning_effort=requested_value.get("reasoning_effort"),
        )
        effective = EffectiveRunExecutionProfile(
            model_name=effective_value.get("model_name"),
            thinking_enabled=effective_value.get("thinking_enabled"),
            reasoning_effort=effective_value.get("reasoning_effort"),
            supports_vision=effective_value.get("supports_vision"),
        )
    except TypeError:
        raise RunExecutionProfileUnsupported from None
    return requested, effective


def effective_run_execution_profile_from_kwargs(
    kwargs: Mapping[str, object],
) -> EffectiveRunExecutionProfile | None:
    if not isinstance(kwargs, Mapping):
        raise RunExecutionProfileUnsupported
    value = kwargs.get(RUN_EXECUTION_PROFILE_KWARG)
    if value is None:
        return None
    return parse_persisted_run_execution_profile(value)[1]


__all__ = [
    "EffectiveRunExecutionProfile",
    "RUN_EXECUTION_PROFILE_KWARG",
    "ReasoningEffort",
    "RequestedRunExecutionProfile",
    "RunExecutionProfileError",
    "RunExecutionProfileUnsupported",
    "RunModelSelectionLocked",
    "RunSelectedModelUnavailable",
    "effective_run_execution_profile_from_kwargs",
    "parse_persisted_run_execution_profile",
    "persisted_run_execution_profile",
    "resolve_admitted_run_execution_profile",
    "selected_run_model_ref",
]
