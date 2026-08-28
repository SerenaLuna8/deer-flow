from __future__ import annotations

from app.system_runtime_settings import (
    AgentRuntimePolicyValue,
    project_memory_compaction_app_config_policy,
)


def test_memory_compaction_projection_exposes_only_memory_and_summarization() -> None:
    policy = AgentRuntimePolicyValue.model_validate(
        {
            **AgentRuntimePolicyValue().model_dump(mode="python"),
            "memory": {
                "enabled": True,
                "idle_seal_minutes": 45,
            },
            "summarization": {
                "enabled": True,
                "trigger_tokens": 320_000,
                "keep": {"type": "tokens", "value": 64_000},
                "trim_tokens_to_summarize": 15564,
            },
        }
    )

    projected = project_memory_compaction_app_config_policy(policy)

    assert set(projected) == {"memory", "summarization"}
    assert projected["memory"]["enabled"] is True
    assert projected["memory"]["idle_seal_minutes"] == 45
    assert projected["summarization"]["enabled"] is True
    assert projected["summarization"]["trigger_tokens"] == 320_000
    assert projected["summarization"]["trim_tokens_to_summarize"] == 15564
