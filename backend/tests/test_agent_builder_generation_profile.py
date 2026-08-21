from __future__ import annotations

import pytest

from app.shared_assets.agent_design_profile import (
    AgentDesignGenerationProfileUnsupported,
    resolve_agent_design_generation_profile,
)

MODEL_REF = "00000000-0000-4000-8000-000000000308"


@pytest.mark.parametrize(
    ("mode", "thinking_enabled", "reasoning_effort"),
    (
        ("flash", False, "none"),
        ("thinking", True, "low"),
        ("pro", True, "medium"),
        ("ultra", True, "high"),
    ),
)
def test_agent_design_generation_profile_matches_normal_chat_modes(
    mode: str,
    thinking_enabled: bool,
    reasoning_effort: str,
) -> None:
    profile = resolve_agent_design_generation_profile(
        requested_model_ref=MODEL_REF,
        effective_model_ref=MODEL_REF,
        mode=mode,
        thinking_enabled=thinking_enabled,
        reasoning_effort=reasoning_effort,
        supports_thinking=True,
        supports_reasoning_effort=True,
    )

    assert profile.model_ref == MODEL_REF
    assert profile.mode == mode
    assert profile.thinking_enabled is thinking_enabled
    assert profile.reasoning_effort == reasoning_effort


@pytest.mark.parametrize(
    ("supports_thinking", "supports_reasoning_effort", "mode", "thinking_enabled", "reasoning_effort"),
    (
        (False, False, "thinking", True, None),
        (True, False, "pro", True, None),
        (True, True, "pro", True, "high"),
        (True, True, "flash", False, "medium"),
    ),
)
def test_agent_design_generation_profile_rejects_stale_or_inconsistent_capabilities(
    supports_thinking: bool,
    supports_reasoning_effort: bool,
    mode: str,
    thinking_enabled: bool,
    reasoning_effort: str | None,
) -> None:
    with pytest.raises(AgentDesignGenerationProfileUnsupported):
        resolve_agent_design_generation_profile(
            requested_model_ref=MODEL_REF,
            effective_model_ref=MODEL_REF,
            mode=mode,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            supports_thinking=supports_thinking,
            supports_reasoning_effort=supports_reasoning_effort,
        )


@pytest.mark.parametrize(
    ("supports_thinking", "supports_reasoning_effort", "expected"),
    (
        (False, False, ("flash", False, None)),
        (True, False, ("thinking", True, None)),
        (True, True, ("pro", True, "medium")),
    ),
)
def test_agent_design_generation_profile_uses_capability_aware_default(
    supports_thinking: bool,
    supports_reasoning_effort: bool,
    expected: tuple[str, bool, str | None],
) -> None:
    profile = resolve_agent_design_generation_profile(
        requested_model_ref=MODEL_REF,
        effective_model_ref=MODEL_REF,
        mode=None,
        thinking_enabled=None,
        reasoning_effort=None,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_reasoning_effort,
    )

    assert (
        profile.mode,
        profile.thinking_enabled,
        profile.reasoning_effort,
    ) == expected
