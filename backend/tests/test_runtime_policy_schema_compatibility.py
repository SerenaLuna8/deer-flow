"""Exact Runtime Policy schema compatibility and v5 contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi import FastAPI, Request

from app.gateway.deps import get_current_agent_runtime_config
from app.reliability.run_execution.tool_call_control_policy import (
    resolve_run_tool_call_control_policy,
)
from app.system_runtime_settings.materializer import (
    _materialize_exact,
    materialize_agent_runtime_policy,
)
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    MaterializedAgentRuntimePolicy,
    RuntimePolicySection,
)
from app.system_runtime_settings.schema_codec import (
    canonical_policy_value_v2,
    canonical_policy_value_v4,
)
from app.system_runtime_settings.validation import (
    RUNTIME_POLICY_SCHEMA_VERSION,
    RuntimePolicyInvalid,
    canonical_policy_payload,
    canonical_policy_payload_for_schema,
    decode_policy_value_for_schema,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig

_V3_AGENT_RUNTIME_VALUE: dict[str, object] = {
    "token_usage": {"enabled": True},
    "token_budget": {
        "enabled": False,
        "max_tokens": 200_000,
        "max_input_tokens": None,
        "max_output_tokens": None,
        "warn_threshold": 0.8,
        "hard_stop_threshold": 1.0,
    },
    "max_recursion_limit": 1_000,
    "title": {
        "enabled": True,
        "max_words": 6,
        "max_chars": 60,
        "model_name": None,
    },
    "suggestions": {"enabled": True},
    "input_polish": {
        "enabled": True,
        "max_chars": 4_000,
        "model_name": None,
    },
    "summarization": {
        "enabled": True,
        "model_name": None,
        "trigger": [{"type": "tokens", "value": 32_000}],
        "keep": {"type": "messages", "value": 10},
        "trim_tokens_to_summarize": 15_564,
        "skill_file_read_tool_names": ["read_file", "read", "view", "cat"],
    },
    "memory": {
        "enabled": True,
        "model_name": None,
        "dream_interval_minutes": 120,
        "max_injection_tokens": 2_000,
        "idle_seal_minutes": 1_440,
        "episode_retention_days": 365,
    },
    "tool_search": {"enabled": False, "auto_promote_top_k": 3},
    "tool_output": {
        "enabled": True,
        "externalize_min_chars": 12_000,
        "preview_head_chars": 2_000,
        "preview_tail_chars": 1_000,
        "fallback_max_chars": 30_000,
        "fallback_head_chars": 8_000,
        "fallback_tail_chars": 3_000,
        "exempt_tools": ["read_file", "read_file_tool"],
        "tool_overrides": {},
    },
    "loop_detection": {
        "enabled": True,
        "warn_threshold": 3,
        "hard_limit": 5,
        "window_size": 20,
        "max_tracked_threads": 100,
        "tool_freq_warn": 30,
        "tool_freq_hard_limit": 50,
        "tool_freq_overrides": {
            "web_fetch": {"warn": 6, "hard_limit": 10},
            "web_search": {"warn": 6, "hard_limit": 10},
            "recall_memory": {"warn": 6, "hard_limit": 10},
            "inspect_image": {"warn": 6, "hard_limit": 9},
        },
    },
    "read_before_write": {"enabled": True},
    "safety_finish_reason": {"enabled": True},
    "subagents": {"max_total_per_run": 6},
    "vision_bridge": {
        "model_name": "84e322c8-6a3c-516e-9e3c-7f08860efbdb",
        "timeout_seconds": 60,
        "contract_version": "vision.bridge.v1",
    },
}
_V3_AGENT_RUNTIME_CHECKSUM = "03c4b5333aaca53fafbe45aad48889e69617c8a18a654412472b63041f17cef4"
_V2_AGENT_RUNTIME_VALUE = {key: value for key, value in _V3_AGENT_RUNTIME_VALUE.items() if key != "vision_bridge"}
_V2_AGENT_RUNTIME_CHECKSUM = "272c452dafa5517375b30fe6dde36791cafe028a7c08afc71d2af1cdbae9aa7b"


def _v4_role_budget(*, web_warn: int, web_hard_limit: int) -> dict[str, object]:
    return {
        "default": {"warn": 30, "hard_limit": 50},
        "tools": {
            "web_search": {"warn": web_warn, "hard_limit": web_hard_limit},
            "web_fetch": {"warn": web_warn, "hard_limit": web_hard_limit},
            "recall_memory": {"warn": 6, "hard_limit": 10},
            "inspect_image": {"warn": 6, "hard_limit": 9},
        },
    }


_V4_AGENT_RUNTIME_VALUE = deepcopy(_V3_AGENT_RUNTIME_VALUE)
_V4_AGENT_RUNTIME_VALUE["loop_detection"] = {
    "enabled": True,
    "identical_calls": {
        "warn_threshold": 3,
        "hard_limit": 5,
        "window_size": 20,
    },
}
_V4_AGENT_RUNTIME_VALUE["tool_call_budget"] = {
    "profiles": {
        "interactive": {
            "lead": _v4_role_budget(web_warn=6, web_hard_limit=10),
            "subagent": _v4_role_budget(web_warn=6, web_hard_limit=10),
        },
        "research": {
            "lead": _v4_role_budget(web_warn=20, web_hard_limit=30),
            "subagent": _v4_role_budget(web_warn=12, web_hard_limit=20),
        },
    },
}
_V4_AGENT_RUNTIME_VALUE["subagents"] = {
    "max_concurrent": 3,
    "max_total_per_run_by_workload": {
        "interactive": 6,
        "research": 9,
    },
}
_V4_AGENT_RUNTIME_VALUE["vision_bridge"] = {
    "model_name": None,
    "timeout_seconds": 60,
    "contract_version": "vision.bridge.v1",
}
_V4_AGENT_RUNTIME_CHECKSUM = "06fc1c841ba2fe4c7d8ad318326b095050f2e9d78d9b2f319d88f4fa018b19f5"
_LEGACY_OTHER_SECTION_CASES = (
    (
        RuntimePolicySection.AUTH,
        {"allow_registration": True},
        "d1b69a81c600b8eb3c2cae5da75137b1d33d9bc92295a77cf84c888583595055",
    ),
    (
        RuntimePolicySection.AUTOMATIONS,
        {
            "enabled": True,
            "poll_interval_seconds": 5,
            "max_concurrent_runs": 3,
            "min_once_delay_seconds": 60,
        },
        "cd4eae7f36175c2eda25d142cb6d816becfa6d0984a3735a27f3d76465a1975f",
    ),
    (
        RuntimePolicySection.MEMORY_DOCUMENT,
        {
            "sections": [
                "用户偏好与协作方式",
                "项目背景",
                "长期约束与架构决策",
                "当前仍有效的目标",
            ]
        },
        "df0f23d20ab7052a19e74424843acc75e78e4f6a4f7610bdd23ceb5973c0eb13",
    ),
    (
        RuntimePolicySection.QUOTAS,
        {
            "default_member_limit": 20,
            "default_storage_bytes_limit": 5_368_709_120,
            "default_concurrent_run_limit": 3,
            "default_mcp_calls_daily_limit": 10_000,
            "warning_threshold": 0.8,
        },
        "0d4c39a5cea430d9041eea6eba95fd3070176a02b75e225e8e5cdff44638b79f",
    ),
)


class _CurrentAgentRuntimePolicyMaterializer:
    def __init__(self, policy: AgentRuntimePolicyValue) -> None:
        self._policy = policy

    async def materialize_current(
        self,
        _section: RuntimePolicySection,
    ) -> AgentRuntimePolicyValue:
        return self._policy


def _gateway_request_with_policy(policy: AgentRuntimePolicyValue) -> Request:
    app = FastAPI()
    app.state.system_runtime_policy_materializer = _CurrentAgentRuntimePolicyMaterializer(policy)
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/models",
            "raw_path": b"/api/models",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


@pytest.mark.asyncio
async def test_gateway_projects_decoded_v3_policy_before_app_config_overlay() -> None:
    decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        _V3_AGENT_RUNTIME_VALUE,
        schema_version=3,
    )
    assert isinstance(decoded, AgentRuntimePolicyValue)

    runtime = await get_current_agent_runtime_config(
        _gateway_request_with_policy(decoded),
        AppConfig(
            sandbox=SandboxConfig(
                use="deerflow.sandbox.local:LocalSandboxProvider",
            ),
        ),
    )

    assert runtime.token_usage.enabled is True
    assert runtime.loop_detection.enabled is True
    assert runtime.subagents.max_total_per_run == 6


def test_schema_v3_agent_runtime_fixture_remains_exact_when_v5_is_current() -> None:
    canonical = canonical_policy_payload_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        _V3_AGENT_RUNTIME_VALUE,
        schema_version=3,
    )

    assert RUNTIME_POLICY_SCHEMA_VERSION == 5
    assert canonical.schema_version == 3
    assert canonical.value == _V3_AGENT_RUNTIME_VALUE
    assert canonical.checksum == _V3_AGENT_RUNTIME_CHECKSUM
    assert "agent_runtime" not in canonical.value

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload_for_schema(
            RuntimePolicySection.AGENT_RUNTIME,
            AgentRuntimePolicyValue(),
            schema_version=3,
        )


def test_schema_v2_uses_its_own_exact_codec_without_v3_bridge_defaults() -> None:
    assert (
        canonical_policy_value_v2(
            RuntimePolicySection.AGENT_RUNTIME,
            _V2_AGENT_RUNTIME_VALUE,
        )
        == _V2_AGENT_RUNTIME_VALUE
    )

    canonical = canonical_policy_payload_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        _V2_AGENT_RUNTIME_VALUE,
        schema_version=2,
    )

    assert canonical.schema_version == 2
    assert canonical.value == _V2_AGENT_RUNTIME_VALUE
    assert canonical.checksum == _V2_AGENT_RUNTIME_CHECKSUM

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload_for_schema(
            RuntimePolicySection.AGENT_RUNTIME,
            _V3_AGENT_RUNTIME_VALUE,
            schema_version=2,
        )


def test_schema_v5_agent_runtime_has_one_internal_tool_call_limit() -> None:
    value = AgentRuntimePolicyValue()
    canonical = canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)

    assert canonical.schema_version == 5
    assert "agent_runtime" not in canonical.value
    assert canonical.value["internal_tool_call_limit"] == 200
    assert "tool_call_budget" not in canonical.value
    assert canonical.value["loop_detection"] == {
        "enabled": True,
        "identical_calls": {
            "warn_threshold": 3,
            "hard_limit": 20,
            "window_size": 20,
        },
    }
    assert canonical.value["subagents"] == {
        "max_concurrent": 3,
        "max_total_per_run_by_workload": {
            "interactive": 6,
            "research": 9,
        },
    }


def test_schema_v4_agent_runtime_fixture_remains_exact_when_v5_is_current() -> None:
    assert (
        canonical_policy_value_v4(
            RuntimePolicySection.AGENT_RUNTIME,
            _V4_AGENT_RUNTIME_VALUE,
        )
        == _V4_AGENT_RUNTIME_VALUE
    )

    canonical = canonical_policy_payload_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        _V4_AGENT_RUNTIME_VALUE,
        schema_version=4,
    )

    assert canonical.schema_version == 4
    assert canonical.value == _V4_AGENT_RUNTIME_VALUE
    assert canonical.checksum == _V4_AGENT_RUNTIME_CHECKSUM

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload_for_schema(
            RuntimePolicySection.AGENT_RUNTIME,
            AgentRuntimePolicyValue(),
            schema_version=4,
        )


def test_schema_v4_agent_runtime_decodes_max_hard_limit_into_single_v5_limit() -> None:
    legacy = deepcopy(_V4_AGENT_RUNTIME_VALUE)
    legacy["tool_call_budget"]["profiles"]["research"]["subagent"]["tools"]["web_fetch"] = {"warn": 12, "hard_limit": 87}

    decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        legacy,
        schema_version=4,
    )

    assert isinstance(decoded, AgentRuntimePolicyValue)
    assert decoded.internal_tool_call_limit == 87
    assert "tool_call_budget" not in decoded.model_dump(mode="json")


def test_schema_v3_agent_runtime_decodes_frequency_into_single_v5_limit() -> None:
    legacy = deepcopy(_V3_AGENT_RUNTIME_VALUE)
    legacy["loop_detection"] = {
        "enabled": True,
        "warn_threshold": 4,
        "hard_limit": 7,
        "window_size": 15,
        "max_tracked_threads": 88,
        "tool_freq_warn": 17,
        "tool_freq_hard_limit": 23,
        "tool_freq_overrides": {
            "web_search": {"warn": 11, "hard_limit": 13},
            "custom_reader": {"warn": 5, "hard_limit": 8},
        },
    }
    legacy["subagents"] = {"max_total_per_run": 8}

    decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        legacy,
        schema_version=3,
    )

    assert isinstance(decoded, AgentRuntimePolicyValue)
    assert decoded.loop_detection.model_dump(mode="json") == {
        "enabled": True,
        "identical_calls": {
            "warn_threshold": 4,
            "hard_limit": 7,
            "window_size": 15,
        },
    }
    assert decoded.internal_tool_call_limit == 23
    assert "tool_call_budget" not in decoded.model_dump(mode="json")
    assert decoded.subagents.max_concurrent == 3
    assert decoded.subagents.max_total_per_run_by_workload.interactive == 8
    assert decoded.subagents.max_total_per_run_by_workload.research == 9

    v2_decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        _V2_AGENT_RUNTIME_VALUE,
        schema_version=2,
    )
    assert isinstance(v2_decoded, AgentRuntimePolicyValue)
    assert v2_decoded.vision_bridge.model_name is None


@pytest.mark.parametrize(
    ("schema_version", "fixture", "expected_checksum"),
    [
        (
            2,
            _V2_AGENT_RUNTIME_VALUE,
            "e4db7cf1df779a1b55bdcd9f3a0c0f569bedfe34985b22899dd886f5563ebf8f",
        ),
        (
            3,
            _V3_AGENT_RUNTIME_VALUE,
            "0b78237e5fe652fcec375cd528a9058ddb75b1f0f8aeab01020d52a0e38b452e",
        ),
    ],
)
def test_legacy_repeat_window_below_hard_limit_materializes_and_resolves_exactly(
    schema_version: int,
    fixture: dict[str, object],
    expected_checksum: str,
) -> None:
    legacy = deepcopy(fixture)
    legacy["loop_detection"]["window_size"] = 4

    canonical = canonical_policy_payload_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        legacy,
        schema_version=schema_version,
    )
    materialized = materialize_agent_runtime_policy(
        schema_version=schema_version,
        value=canonical.value,
        checksum=expected_checksum,
    )
    resolved = resolve_run_tool_call_control_policy(materialized, {})

    assert canonical.value == legacy
    assert canonical.checksum == expected_checksum
    assert materialized.value.loop_detection.identical_calls.model_dump(
        mode="json",
    ) == {
        "warn_threshold": 3,
        "hard_limit": 5,
        "window_size": 4,
    }
    assert resolved.lead.repeated_calls.hard_limit == 5
    assert resolved.lead.repeated_calls.window_size == 4


def test_schema_v3_task_frequency_remains_exact_and_contributes_to_v5_limit() -> None:
    legacy = deepcopy(_V3_AGENT_RUNTIME_VALUE)
    legacy["loop_detection"]["tool_freq_overrides"]["task"] = {
        "warn": 55,
        "hard_limit": 60,
    }

    canonical = canonical_policy_payload_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        legacy,
        schema_version=3,
    )
    decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        canonical.value,
        schema_version=3,
    )

    assert canonical.value["loop_detection"]["tool_freq_overrides"]["task"] == {
        "warn": 55,
        "hard_limit": 60,
    }
    assert isinstance(decoded, AgentRuntimePolicyValue)
    assert decoded.internal_tool_call_limit == 60


@pytest.mark.parametrize(
    ("schema_version", "value", "checksum"),
    [
        (2, _V2_AGENT_RUNTIME_VALUE, _V2_AGENT_RUNTIME_CHECKSUM),
        (3, _V3_AGENT_RUNTIME_VALUE, _V3_AGENT_RUNTIME_CHECKSUM),
        (4, _V4_AGENT_RUNTIME_VALUE, _V4_AGENT_RUNTIME_CHECKSUM),
    ],
)
def test_materializer_verifies_legacy_checksum_then_returns_current_policy(
    schema_version: int,
    value: dict[str, object],
    checksum: str,
) -> None:
    materialized = _materialize_exact(
        RuntimePolicySection.AGENT_RUNTIME,
        schema_version=schema_version,
        value=value,
        checksum=checksum,
    )

    assert isinstance(materialized, AgentRuntimePolicyValue)
    assert materialized.internal_tool_call_limit == 50


def test_agent_runtime_materialization_preserves_frozen_schema_in_envelope() -> None:
    materialized = materialize_agent_runtime_policy(
        schema_version=3,
        value=_V3_AGENT_RUNTIME_VALUE,
        checksum=_V3_AGENT_RUNTIME_CHECKSUM,
    )

    assert isinstance(materialized, MaterializedAgentRuntimePolicy)
    assert materialized.schema_version == 3
    assert isinstance(materialized.value, AgentRuntimePolicyValue)
    assert materialized.value.internal_tool_call_limit == 50


@pytest.mark.parametrize("schema_version", [2, 3, 4])
@pytest.mark.parametrize(
    ("section", "value", "checksum"),
    _LEGACY_OTHER_SECTION_CASES,
)
def test_other_runtime_policy_sections_keep_exact_legacy_values_and_checksums(
    schema_version: int,
    section: RuntimePolicySection,
    value: dict[str, object],
    checksum: str,
) -> None:
    canonical = canonical_policy_payload_for_schema(
        section,
        value,
        schema_version=schema_version,
    )
    decoded = decode_policy_value_for_schema(
        section,
        value,
        schema_version=schema_version,
    )
    current = canonical_policy_payload(section, value)

    assert canonical.value == value
    assert canonical.checksum == checksum
    assert decoded.model_dump(mode="json") == value
    assert current.schema_version == 5
    assert current.value == value
    assert current.checksum == checksum


@pytest.mark.parametrize("internal_tool_call_limit", [0, 100_001])
def test_schema_v5_rejects_out_of_range_internal_tool_call_limit(
    internal_tool_call_limit: int,
) -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="python")
    value["internal_tool_call_limit"] = internal_tool_call_limit

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)


def test_schema_v5_rejects_v4_tool_budget_and_agent_runtime_wrapper() -> None:
    legacy_budget = AgentRuntimePolicyValue().model_dump(mode="python")
    legacy_budget["tool_call_budget"] = deepcopy(
        _V4_AGENT_RUNTIME_VALUE["tool_call_budget"],
    )
    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, legacy_budget)

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(
            RuntimePolicySection.AGENT_RUNTIME,
            {"agent_runtime": AgentRuntimePolicyValue().model_dump(mode="python")},
        )
