from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.scheduler.memory import (
    MemoryConsolidationRuntime,
    MemoryMaintenanceSchedulerService,
)


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _Session:
    def begin_nested(self) -> _Transaction:
        return _Transaction()


class _Repository:
    def __init__(self) -> None:
        self.consolidation_calls: list[dict[str, object]] = []
        self.retention_calls: list[dict[str, object]] = []
        self._consolidation_results = [SimpleNamespace(), None]
        self._retention_results = [SimpleNamespace(), None]

    async def admit_next_consolidation(self, **kwargs):
        self.consolidation_calls.append(kwargs)
        return self._consolidation_results.pop(0)

    async def admit_next_retention(self, **kwargs):
        self.retention_calls.append(kwargs)
        return self._retention_results.pop(0)


async def _runtime(_session) -> MemoryConsolidationRuntime:
    return MemoryConsolidationRuntime(
        enabled=True,
        pipeline_mode="consolidate",
        consolidation_interval_minutes=120,
        candidate_retention_days=30,
        fact_confidence_threshold=0.7,
        max_facts=100,
        policy_revision=4,
        model_config_id=uuid.uuid4(),
        model_config_version_id=uuid.uuid4(),
        model_config_checksum="a" * 64,
    )


@pytest.mark.asyncio
async def test_scheduler_admits_bounded_consolidation_and_retention_jobs() -> None:
    repository = _Repository()
    service = MemoryMaintenanceSchedulerService(
        runtime_resolver=_runtime,
        repository_builder=lambda _session: repository,
        max_jobs_per_poll=10,
    )
    now = datetime(2026, 8, 5, tzinfo=UTC)

    result = await service.admit_due(_Session(), now=now)

    assert result.consolidation_jobs == 1
    assert result.retention_jobs == 1
    assert repository.consolidation_calls[0]["now"] == now
    contract = repository.consolidation_calls[0]["contract"]
    assert contract.policy_revision == 4
    assert contract.prompt_version == "memory-consolidate-prompt-v1"
    assert contract.consolidator_version == "memory-consolidator-v1"
    assert contract.output_schema_version == "memory-consolidate-output-v1"
    assert repository.retention_calls[0]["retention_days"] == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,mode", [(False, "consolidate"), (True, "off"), (True, "shadow")])
async def test_scheduler_pause_preserves_candidate_backlog(
    enabled: bool,
    mode: str,
) -> None:
    repository = _Repository()

    async def runtime(_session):
        value = await _runtime(_session)
        return replace(value, enabled=enabled, pipeline_mode=mode)

    service = MemoryMaintenanceSchedulerService(
        runtime_resolver=runtime,
        repository_builder=lambda _session: repository,
    )

    result = await service.admit_due(
        _Session(),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert result.consolidation_jobs == 0
    assert result.retention_jobs == 0
    assert repository.consolidation_calls == []
    assert repository.retention_calls == []


@pytest.mark.asyncio
async def test_scheduler_retention_does_not_depend_on_model_availability() -> None:
    repository = _Repository()

    async def runtime(_session):
        value = await _runtime(_session)
        return replace(
            value,
            model_config_id=None,
            model_config_version_id=None,
            model_config_checksum=None,
        )

    service = MemoryMaintenanceSchedulerService(
        runtime_resolver=runtime,
        repository_builder=lambda _session: repository,
    )

    result = await service.admit_due(
        _Session(),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert result.consolidation_jobs == 0
    assert result.retention_jobs == 1
    assert repository.consolidation_calls == []
    assert len(repository.retention_calls) == 2


@pytest.mark.asyncio
async def test_scheduler_isolates_retention_from_consolidation_failure() -> None:
    repository = _Repository()

    async def fail_consolidation(**kwargs):
        repository.consolidation_calls.append(kwargs)
        raise RuntimeError("injected consolidation failure")

    repository.admit_next_consolidation = fail_consolidation
    service = MemoryMaintenanceSchedulerService(
        runtime_resolver=_runtime,
        repository_builder=lambda _session: repository,
    )

    result = await service.admit_due(
        _Session(),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert result.retention_jobs == 1
    assert result.consolidation_jobs == 0
    assert len(repository.retention_calls) == 2
    assert len(repository.consolidation_calls) == 1
