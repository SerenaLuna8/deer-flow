"""Exact Runtime Policy schema v1 contracts.

The pre-release policy history (schemas v2-v7) was consolidated into one v1
baseline before launch. These tests pin that v1 is the only readable schema:
stored payloads canonicalize and decode exactly, any other declared schema
number fails closed, and no upgrade path exists.
"""

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
from app.system_runtime_settings.validation import (
    RUNTIME_POLICY_SCHEMA_VERSION,
    RuntimePolicyInvalid,
    canonical_policy_payload,
    canonical_policy_payload_for_schema,
    decode_policy_value_for_schema,
)
from deerflow.config.app_config import AppConfig
from deerflow.config.sandbox_config import SandboxConfig
from deerflow.config.summarization_config import MIN_TRIM_TOKENS_TO_SUMMARIZE

_RETIRED_TRIGGER_LIST_SUMMARIZATION: dict[str, object] = {
    "enabled": True,
    "model_name": None,
    "trigger": [{"type": "tokens", "value": 32_000}],
    "keep": {"type": "tokens", "value": 64_000},
    "trim_tokens_to_summarize": 15_564,
    "skill_file_read_tool_names": ["read_file", "read", "view", "cat"],
}
_OTHER_SECTION_CASES = (
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


def test_schema_v1_is_the_only_runtime_policy_schema() -> None:
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        AgentRuntimePolicyValue(),
    )

    assert RUNTIME_POLICY_SCHEMA_VERSION == 1
    assert canonical.schema_version == 1
    assert canonical.value["internal_tool_call_limits"] == {
        "lead_per_run": 200,
        "subagent_per_task": 50,
    }
    assert "internal_tool_call_limit" not in canonical.value
    assert "tool_call_budget" not in canonical.value
    assert canonical.value["summarization"]["trigger_tokens"] == 320_000
    assert canonical.value["summarization"]["keep"] == {
        "type": "tokens",
        "value": 64_000,
    }
    assert "trigger" not in canonical.value["summarization"]


def test_schema_v1_roundtrips_through_declared_schema_reads() -> None:
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        AgentRuntimePolicyValue(),
    )
    reread = canonical_policy_payload_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        canonical.value,
        schema_version=1,
    )
    decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        canonical.value,
        schema_version=1,
    )

    assert reread == canonical
    assert isinstance(decoded, AgentRuntimePolicyValue)
    assert decoded == AgentRuntimePolicyValue()


@pytest.mark.parametrize("schema_version", [0, 2, 3, 4, 5, 6, 7, True])
def test_retired_schema_numbers_fail_closed(schema_version: object) -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="json")

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload_for_schema(
            RuntimePolicySection.AGENT_RUNTIME,
            value,
            schema_version=schema_version,  # type: ignore[arg-type]
        )
    with pytest.raises(RuntimePolicyInvalid):
        decode_policy_value_for_schema(
            RuntimePolicySection.AGENT_RUNTIME,
            value,
            schema_version=schema_version,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("schema_version", [2, 5, 6, 7])
def test_materializer_rejects_retired_schema_numbers(schema_version: int) -> None:
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        AgentRuntimePolicyValue(),
    )

    with pytest.raises(Exception):  # noqa: B017 - repository invariant marker
        _materialize_exact(
            RuntimePolicySection.AGENT_RUNTIME,
            schema_version=schema_version,
            value=canonical.value,
            checksum=canonical.checksum,
        )


def test_schema_v1_rejects_retired_keep_measurements() -> None:
    for keep in (
        {"type": "messages", "value": 10},
        {"type": "fraction", "value": 0.8},
    ):
        value = AgentRuntimePolicyValue().model_dump(mode="python")
        value["summarization"]["keep"] = keep

        with pytest.raises(RuntimePolicyInvalid):
            canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)


def test_schema_v1_rejects_keep_that_cannot_reduce_below_trigger() -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="python")
    value["summarization"]["trigger_tokens"] = 64_000
    value["summarization"]["keep"] = {
        "type": "tokens",
        "value": 64_000,
    }

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)


def test_schema_v1_rejects_the_retired_trigger_list_shape() -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="python")
    value["summarization"] = deepcopy(_RETIRED_TRIGGER_LIST_SUMMARIZATION)

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)


def test_schema_v1_materialization_resolves_role_scoped_limits() -> None:
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        AgentRuntimePolicyValue(),
    )
    materialized = materialize_agent_runtime_policy(
        schema_version=1,
        value=canonical.value,
        checksum=canonical.checksum,
    )
    resolved = resolve_run_tool_call_control_policy(
        materialized,
        {
            "__run_workload_profile": {
                "requested": {"name": "interactive"},
                "effective": {"name": "interactive"},
            }
        },
    )

    assert isinstance(materialized, MaterializedAgentRuntimePolicy)
    assert materialized.schema_version == 1
    assert materialized.value.internal_tool_call_limits.lead_per_run == 200
    assert materialized.value.internal_tool_call_limits.subagent_per_task == 50
    assert resolved.lead.internal_tool_call_limit == 200
    assert resolved.subagent.internal_tool_call_limit == 50


def test_schema_v1_materialization_rejects_checksum_mismatch() -> None:
    canonical = canonical_policy_payload(
        RuntimePolicySection.AGENT_RUNTIME,
        AgentRuntimePolicyValue(),
    )

    with pytest.raises(Exception):  # noqa: B017 - repository invariant marker
        materialize_agent_runtime_policy(
            schema_version=1,
            value=canonical.value,
            checksum="0" * 64,
        )


@pytest.mark.asyncio
async def test_gateway_projects_decoded_v1_policy_before_app_config_overlay() -> None:
    decoded = decode_policy_value_for_schema(
        RuntimePolicySection.AGENT_RUNTIME,
        AgentRuntimePolicyValue().model_dump(mode="json"),
        schema_version=1,
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
    assert runtime.summarization.trigger_tokens == 320_000
    assert runtime.subagents.max_total_per_run == 6


@pytest.mark.parametrize(
    ("section", "value", "checksum"),
    _OTHER_SECTION_CASES,
)
def test_other_runtime_policy_sections_keep_exact_values_and_checksums(
    section: RuntimePolicySection,
    value: dict[str, object],
    checksum: str,
) -> None:
    canonical = canonical_policy_payload(section, value)
    reread = canonical_policy_payload_for_schema(
        section,
        value,
        schema_version=1,
    )
    decoded = decode_policy_value_for_schema(section, value, schema_version=1)

    assert canonical.schema_version == 1
    assert canonical.value == value
    assert canonical.checksum == checksum
    assert reread == canonical
    assert decoded.model_dump(mode="json") == value


@pytest.mark.parametrize("field", ["lead_per_run", "subagent_per_task"])
@pytest.mark.parametrize("limit", [0, 100_001])
def test_schema_v1_rejects_out_of_range_internal_tool_call_limit(
    field: str,
    limit: int,
) -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="python")
    value["internal_tool_call_limits"][field] = limit

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)


def test_schema_v1_rejects_trim_budget_below_packaged_prompt_floor() -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="python")
    value["summarization"]["trim_tokens_to_summarize"] = MIN_TRIM_TOKENS_TO_SUMMARIZE - 1

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)

    value["summarization"]["trim_tokens_to_summarize"] = MIN_TRIM_TOKENS_TO_SUMMARIZE
    canonical = canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)
    summarization = canonical.value["summarization"]
    assert isinstance(summarization, dict)
    assert summarization["trim_tokens_to_summarize"] == MIN_TRIM_TOKENS_TO_SUMMARIZE


def test_schema_v1_rejects_retired_tool_budget_and_agent_runtime_wrapper() -> None:
    legacy_budget = AgentRuntimePolicyValue().model_dump(mode="python")
    legacy_budget["tool_call_budget"] = {"profiles": {}}
    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, legacy_budget)

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(
            RuntimePolicySection.AGENT_RUNTIME,
            {"agent_runtime": AgentRuntimePolicyValue().model_dump(mode="python")},
        )


def test_schema_v1_repeat_window_must_cover_the_hard_limit() -> None:
    value = AgentRuntimePolicyValue().model_dump(mode="python")
    value["loop_detection"]["identical_calls"]["window_size"] = 4

    with pytest.raises(RuntimePolicyInvalid):
        canonical_policy_payload(RuntimePolicySection.AGENT_RUNTIME, value)
