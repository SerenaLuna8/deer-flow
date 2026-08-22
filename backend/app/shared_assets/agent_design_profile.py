"""Capability-checked generation profiles for Agent Builder one-shot calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.system_settings.model_refs import DEFAULT_MODEL_REF, exact_model_ref
from deerflow.config.model_execution import FrozenSystemModelExecution

type AgentDesignGenerationMode = Literal["flash", "thinking", "pro", "ultra"]
type AgentDesignReasoningEffort = Literal["none", "low", "medium", "high"]

_MODES = frozenset({"flash", "thinking", "pro", "ultra"})


class AgentDesignGenerationProfileUnsupported(ValueError):
    """The selected model cannot honor the requested Builder profile."""


@dataclass(frozen=True, slots=True)
class AgentDesignGenerationProfile:
    model_ref: str
    mode: AgentDesignGenerationMode
    thinking_enabled: bool
    reasoning_effort: AgentDesignReasoningEffort | None
    model_execution: FrozenSystemModelExecution | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "model_ref": self.model_ref,
            "mode": self.mode,
            "thinking_enabled": self.thinking_enabled,
            "reasoning_effort": self.reasoning_effort,
        }
        if self.model_execution is not None:
            result["model_execution"] = {
                "model_config_id": str(self.model_execution.model_config_id),
                "provider_payload": dict(self.model_execution.provider_payload),
                "payload_checksum": self.model_execution.payload_checksum,
                "secret_generation_id": (str(self.model_execution.secret_generation_id) if self.model_execution.secret_generation_id is not None else None),
                "secret_envelope_digest": (self.model_execution.secret_envelope_digest),
            }
        return result


def _default_mode(
    supports_thinking: bool,
    supports_reasoning_effort: bool,
) -> AgentDesignGenerationMode:
    if not supports_thinking:
        return "flash"
    return "pro" if supports_reasoning_effort else "thinking"


def agent_design_mode_profile(
    mode: AgentDesignGenerationMode,
    *,
    supports_thinking: bool,
    supports_reasoning_effort: bool,
) -> tuple[bool, AgentDesignReasoningEffort | None]:
    if mode == "flash":
        return False, "none" if supports_reasoning_effort else None
    if not supports_thinking:
        raise AgentDesignGenerationProfileUnsupported
    if mode == "thinking":
        return True, "low" if supports_reasoning_effort else None
    if not supports_reasoning_effort:
        raise AgentDesignGenerationProfileUnsupported
    return True, "medium" if mode == "pro" else "high"


def agent_design_mode_matches_profile(
    mode: str,
    *,
    thinking_enabled: bool,
    reasoning_effort: str | None,
) -> bool:
    """Validate a normalized chooser value without model capability state."""

    if mode not in _MODES or type(thinking_enabled) is not bool:
        return False
    if reasoning_effort not in {None, "none", "low", "medium", "high"}:
        return False
    try:
        expected = agent_design_mode_profile(
            mode,  # type: ignore[arg-type]
            supports_thinking=thinking_enabled,
            supports_reasoning_effort=reasoning_effort is not None,
        )
    except AgentDesignGenerationProfileUnsupported:
        return False
    return expected == (thinking_enabled, reasoning_effort)


def resolve_agent_design_generation_profile(
    *,
    requested_model_ref: str,
    effective_model_ref: str,
    mode: str | None,
    thinking_enabled: bool | None,
    reasoning_effort: str | None,
    supports_thinking: bool,
    supports_reasoning_effort: bool,
) -> AgentDesignGenerationProfile:
    """Resolve capability-aware defaults and reject stale client profiles."""

    if (requested_model_ref != DEFAULT_MODEL_REF and exact_model_ref(requested_model_ref) is None) or exact_model_ref(effective_model_ref) is None:
        raise AgentDesignGenerationProfileUnsupported
    if type(supports_thinking) is not bool or type(supports_reasoning_effort) is not bool:
        raise AgentDesignGenerationProfileUnsupported
    if supports_reasoning_effort and not supports_thinking:
        raise AgentDesignGenerationProfileUnsupported

    if mode is None:
        if thinking_enabled is not None or reasoning_effort is not None:
            raise AgentDesignGenerationProfileUnsupported
        resolved_mode = _default_mode(
            supports_thinking,
            supports_reasoning_effort,
        )
    else:
        if type(mode) is not str or mode not in _MODES:
            raise AgentDesignGenerationProfileUnsupported
        resolved_mode = mode

    expected_thinking, expected_effort = agent_design_mode_profile(
        resolved_mode,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_reasoning_effort,
    )
    if mode is not None and (thinking_enabled is not expected_thinking or reasoning_effort != expected_effort):
        raise AgentDesignGenerationProfileUnsupported

    return AgentDesignGenerationProfile(
        model_ref=effective_model_ref,
        mode=resolved_mode,
        thinking_enabled=expected_thinking,
        reasoning_effort=expected_effort,
    )


__all__ = [
    "AgentDesignGenerationMode",
    "AgentDesignGenerationProfile",
    "AgentDesignGenerationProfileUnsupported",
    "AgentDesignReasoningEffort",
    "agent_design_mode_profile",
    "agent_design_mode_matches_profile",
    "resolve_agent_design_generation_profile",
]
