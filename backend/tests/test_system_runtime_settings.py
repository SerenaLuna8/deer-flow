from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.run_repository import PrivateRunCreate
from app.private_work.snapshot_repository import _apply_runtime_recursion_limit
from app.system_runtime_settings.models import (
    AgentRuntimePolicyValue,
    AuthPolicyValue,
    LockedAgentRuntimePolicy,
    QuotaPolicyValue,
    RuntimePolicySection,
    default_policy_value,
)
from app.system_runtime_settings.repository import (
    SystemRuntimePolicyRepositoryInvariant,
)
from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_runtime_settings.validation import (
    RuntimePolicyInvalid,
    canonical_policy_payload,
    parse_policy_value,
)
from deerflow.persistence.base import Base
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyCatalogStateRow,
    SystemRuntimePolicyRow,
    SystemRuntimePolicyVersionRow,
)


def test_runtime_policy_tables_are_registered_and_schema_is_append_only() -> None:
    assert {
        "system_runtime_policy_catalog_state",
        "system_runtime_policies",
        "system_runtime_policy_versions",
        "run_runtime_policy_snapshots",
    }.issubset(Base.metadata.tables)
    assert SystemRuntimePolicyCatalogStateRow.__table__ is Base.metadata.tables["system_runtime_policy_catalog_state"]
    assert SystemRuntimePolicyRow.__table__ is Base.metadata.tables["system_runtime_policies"]
    assert SystemRuntimePolicyVersionRow.__table__ is Base.metadata.tables["system_runtime_policy_versions"]
    assert RunRuntimePolicySnapshotRow.__table__ is Base.metadata.tables["run_runtime_policy_snapshots"]

    schema = (Path(__file__).parents[1] / "packages" / "harness" / "deerflow" / "persistence" / "full_schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE system_runtime_policy_catalog_state (" in schema
    assert "CREATE TABLE system_runtime_policies (" in schema
    assert "CREATE TABLE system_runtime_policy_versions (" in schema
    assert "CREATE TABLE run_runtime_policy_snapshots (" in schema
    assert "trg_system_runtime_policy_versions_immutable" in schema
    assert "trg_run_runtime_policy_snapshots_immutable" in schema
    assert "FROM runs\n     WHERE project_id = OLD.project_id" in schema
    assert "FOREIGN KEY(section, policy_version_id, schema_version, payload_checksum)" in schema


def test_policy_defaults_are_strict_secret_free_and_bounded() -> None:
    agent = default_policy_value(RuntimePolicySection.AGENT_RUNTIME)
    auth = default_policy_value(RuntimePolicySection.AUTH)
    quotas = default_policy_value(RuntimePolicySection.QUOTAS)

    assert isinstance(agent, AgentRuntimePolicyValue)
    assert isinstance(auth, AuthPolicyValue)
    assert isinstance(quotas, QuotaPolicyValue)
    assert agent.subagents.max_total_per_run == 6
    assert auth.allow_registration is True
    assert quotas.default_member_limit == 20
    fraction = parse_policy_value(
        RuntimePolicySection.AGENT_RUNTIME,
        {
            **agent.model_dump(mode="python"),
            "summarization": {
                **agent.summarization.model_dump(mode="python"),
                "keep": {"type": "fraction", "value": 1},
            },
        },
    )
    assert isinstance(fraction, AgentRuntimePolicyValue)
    assert fraction.summarization.keep.value == 1.0

    with pytest.raises((RuntimePolicyInvalid, ValidationError)):
        parse_policy_value(RuntimePolicySection.AUTH, {"allow_registration": True, "unknown": False})
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(RuntimePolicySection.AUTH, {"allow_registration": True, "api_key": "secret"})
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(
            RuntimePolicySection.AGENT_RUNTIME,
            {
                **agent.model_dump(mode="python"),
                "tool_output": {
                    **agent.tool_output.model_dump(mode="python"),
                    "tool_overrides": {"openaiApiKey": 10},
                },
            },
        )
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(
            RuntimePolicySection.QUOTAS,
            {
                **quotas.model_dump(mode="python"),
                "default_storage_bytes_limit": 2**53,
            },
        )
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(
            RuntimePolicySection.AGENT_RUNTIME,
            {**agent.model_dump(mode="python"), "title": {**agent.title.model_dump(), "model_name": "sk-proj-secret-value"}},
        )
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(
            RuntimePolicySection.AGENT_RUNTIME,
            {**agent.model_dump(mode="python"), "token_budget": {**agent.token_budget.model_dump(), "warn_threshold": 0.9, "hard_stop_threshold": 0.8}},
        )

    oversized = agent.model_dump(mode="python")
    oversized["tool_output"]["exempt_tools"] = [f"tool_{index}_{'x' * 500}" for index in range(64)]
    with pytest.raises(RuntimePolicyInvalid):
        parse_policy_value(RuntimePolicySection.AGENT_RUNTIME, oversized)


def test_policy_canonical_payload_is_deterministic_and_contains_no_dataclass_escape_hatch() -> None:
    value = default_policy_value(RuntimePolicySection.QUOTAS)
    first = canonical_policy_payload(RuntimePolicySection.QUOTAS, value)
    second = canonical_policy_payload(RuntimePolicySection.QUOTAS, value.model_dump(mode="python"))
    assert first == second
    assert first.schema_version == 1
    assert len(first.checksum) == 64
    assert first.value["warning_threshold"] == 0.8
    assert not dataclasses.is_dataclass(value)


def test_run_policy_snapshot_orm_is_secret_free_and_exact() -> None:
    columns = set(RunRuntimePolicySnapshotRow.__table__.columns.keys())
    assert {
        "project_id",
        "owner_user_id",
        "thread_id",
        "run_id",
        "section",
        "policy_version_id",
        "payload_checksum",
    }.issubset(columns)
    assert not ({"secret", "token", "password", "api_key"} & columns)
    assert uuid.UUID("10000000-0000-0000-0000-000000000001")


def test_admission_final_clamp_uses_the_locked_database_policy() -> None:
    value = default_policy_value(RuntimePolicySection.AGENT_RUNTIME).model_copy(
        update={"max_recursion_limit": 37},
    )
    assert isinstance(value, AgentRuntimePolicyValue)
    locked = LockedAgentRuntimePolicy(
        policy_version_id=uuid.uuid4(),
        schema_version=1,
        payload_checksum="a" * 64,
        value=value,
    )
    request = PrivateRunCreate(
        origin_trace_id=str(uuid.uuid4()),
        kwargs={"config": {"recursion_limit": 100_000}},
    )

    admitted = _apply_runtime_recursion_limit(request, locked)

    assert admitted.kwargs["config"]["recursion_limit"] == 37
    assert request.kwargs["config"]["recursion_limit"] == 100_000


@pytest.mark.asyncio
async def test_corrupt_database_payload_fails_closed_as_repository_invariant(
    monkeypatch,
) -> None:
    async def corrupt_current(*_args, **_kwargs):
        return (
            SimpleNamespace(),
            SimpleNamespace(
                id=uuid.uuid4(),
                schema_version=1,
                value={"unknown": True},
                payload_checksum="a" * 64,
            ),
        )

    monkeypatch.setattr(
        "app.system_runtime_settings.service.SystemRuntimePolicyRepository.current",
        corrupt_current,
    )
    session = MagicMock(spec=AsyncSession)
    session.in_transaction.return_value = True

    with pytest.raises(SystemRuntimePolicyRepositoryInvariant):
        await SystemRuntimePolicyService.lock_agent_runtime_for_admission(
            session,
        )
