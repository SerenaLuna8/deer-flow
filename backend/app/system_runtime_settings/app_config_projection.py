"""Project database-owned Agent Runtime Policy into legacy ``AppConfig``."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.system_runtime_settings.models import AgentRuntimePolicyValue


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def project_agent_runtime_app_config_policy(
    policy: AgentRuntimePolicyValue,
    *,
    max_total_subagents: int,
) -> Mapping[str, object]:
    """Return only policy leaves still represented by Harness ``AppConfig``.

    The internal tool-call limit and repeated-call thresholds are resolved separately and
    passed to ``ToolCallControl``. ``AppConfig`` retains only the compatibility
    enablement switch and the selected workload's legacy Sub-Agent total.
    """

    overlay = policy.model_dump(mode="python")
    overlay.pop("internal_tool_call_limit", None)
    overlay["loop_detection"] = {
        "enabled": policy.loop_detection.enabled,
    }
    overlay["subagents"] = {
        "max_total_per_run": max_total_subagents,
    }
    frozen = _freeze(overlay)
    if not isinstance(frozen, Mapping):
        raise TypeError("runtime policy overlay must be a mapping")
    return frozen


def project_memory_compaction_app_config_policy(
    policy: AgentRuntimePolicyValue,
) -> Mapping[str, object]:
    """Project only the policy leaves required by Memory compaction."""

    frozen = _freeze(
        {
            "memory": policy.memory.model_dump(mode="python"),
            "summarization": policy.summarization.model_dump(mode="python"),
        }
    )
    if not isinstance(frozen, Mapping):
        raise TypeError("memory compaction policy overlay must be a mapping")
    return frozen


__all__ = [
    "project_agent_runtime_app_config_policy",
    "project_memory_compaction_app_config_policy",
]
