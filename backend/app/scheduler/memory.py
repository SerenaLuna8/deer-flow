"""Memory v2 maintenance admission inside the existing Scheduler process."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.system_runtime_settings.service import SystemRuntimePolicyService
from app.system_settings.repository import SystemModelRepository
from deerflow.agents.memory.consolidator import (
    MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION,
    MEMORY_CONSOLIDATE_PROMPT_VERSION,
    MEMORY_CONSOLIDATOR_VERSION,
)
from deerflow.persistence.private_work.memory_v2_repository import (
    MemoryConsolidationAdmissionContract,
    MemoryV2Repository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryConsolidationRuntime:
    enabled: bool
    pipeline_mode: Literal["off", "shadow", "consolidate", "v2"]
    consolidation_interval_minutes: int
    candidate_retention_days: int
    fact_confidence_threshold: float
    max_facts: int
    policy_revision: int
    model_config_id: uuid.UUID | None
    model_config_version_id: uuid.UUID | None
    model_config_checksum: str | None


@dataclass(frozen=True, slots=True)
class MemoryMaintenanceAdmissionResult:
    consolidation_jobs: int
    retention_jobs: int


class MemoryMaintenanceRepository(Protocol):
    async def admit_next_consolidation(self, **kwargs): ...

    async def admit_next_retention(self, **kwargs): ...


MemoryRuntimeResolver = Callable[
    [AsyncSession],
    Awaitable[MemoryConsolidationRuntime],
]


async def resolve_memory_consolidation_runtime(
    session: AsyncSession,
) -> MemoryConsolidationRuntime:
    locked = await SystemRuntimePolicyService.read_agent_runtime_for_admission(
        session,
    )
    memory = locked.value.memory
    model_config_id = None
    model_config_version_id = None
    model_config_checksum = None
    if memory.enabled and memory.pipeline_mode in {"consolidate", "v2"}:
        material = await SystemModelRepository(session).resolve_active_model(
            memory.model_name,
            load_envelope=False,
        )
        if material is not None:
            model_config_id = material.model.id
            model_config_version_id = material.version.id
            model_config_checksum = material.version.payload_checksum
    return MemoryConsolidationRuntime(
        enabled=memory.enabled,
        pipeline_mode=memory.pipeline_mode,
        consolidation_interval_minutes=memory.consolidation_interval_minutes,
        candidate_retention_days=memory.candidate_retention_days,
        fact_confidence_threshold=memory.fact_confidence_threshold,
        max_facts=memory.max_facts,
        policy_revision=locked.revision,
        model_config_id=model_config_id,
        model_config_version_id=model_config_version_id,
        model_config_checksum=model_config_checksum,
    )


class MemoryMaintenanceSchedulerService:
    def __init__(
        self,
        *,
        runtime_resolver: MemoryRuntimeResolver,
        repository_builder: Callable[[AsyncSession], MemoryMaintenanceRepository] = MemoryV2Repository,
        max_jobs_per_poll: int = 100,
    ) -> None:
        if not callable(runtime_resolver) or not callable(repository_builder) or isinstance(max_jobs_per_poll, bool) or not isinstance(max_jobs_per_poll, int) or not 1 <= max_jobs_per_poll <= 100:
            raise ValueError("Memory Scheduler configuration is invalid")
        self._runtime_resolver = runtime_resolver
        self._repository_builder = repository_builder
        self._max_jobs_per_poll = max_jobs_per_poll

    async def admit_due(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> MemoryMaintenanceAdmissionResult:
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Memory Scheduler time is invalid")
        runtime = await self._runtime_resolver(session)
        if not runtime.enabled or runtime.pipeline_mode not in {"consolidate", "v2"}:
            return MemoryMaintenanceAdmissionResult(0, 0)
        repository = self._repository_builder(session)
        retention_jobs = 0
        for _ in range(self._max_jobs_per_poll):
            try:
                async with session.begin_nested():
                    admitted = await repository.admit_next_retention(
                        retention_days=runtime.candidate_retention_days,
                        policy_revision=runtime.policy_revision,
                        now=now,
                    )
            except Exception as error:  # noqa: BLE001 - isolate maintenance lanes
                logger.error(
                    "Memory retention admission failed: error_type=%s",
                    type(error).__name__,
                )
                break
            if admitted is None:
                break
            retention_jobs += 1
        consolidation_jobs = 0
        if runtime.model_config_id is not None and runtime.model_config_version_id is not None and runtime.model_config_checksum is not None:
            contract = MemoryConsolidationAdmissionContract(
                interval_minutes=runtime.consolidation_interval_minutes,
                policy_revision=runtime.policy_revision,
                model_config_id=runtime.model_config_id,
                model_config_version_id=runtime.model_config_version_id,
                model_config_checksum=runtime.model_config_checksum,
                prompt_version=MEMORY_CONSOLIDATE_PROMPT_VERSION,
                consolidator_version=MEMORY_CONSOLIDATOR_VERSION,
                output_schema_version=MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION,
            )
            for _ in range(self._max_jobs_per_poll):
                try:
                    async with session.begin_nested():
                        admitted = await repository.admit_next_consolidation(
                            contract=contract,
                            now=now,
                        )
                except Exception as error:  # noqa: BLE001 - isolate maintenance lanes
                    logger.error(
                        "Memory consolidation admission failed: error_type=%s",
                        type(error).__name__,
                    )
                    break
                if admitted is None:
                    break
                consolidation_jobs += 1
        return MemoryMaintenanceAdmissionResult(
            consolidation_jobs=consolidation_jobs,
            retention_jobs=retention_jobs,
        )


__all__ = [
    "MEMORY_CONSOLIDATE_OUTPUT_SCHEMA_VERSION",
    "MEMORY_CONSOLIDATE_PROMPT_VERSION",
    "MEMORY_CONSOLIDATOR_VERSION",
    "MemoryConsolidationRuntime",
    "MemoryMaintenanceAdmissionResult",
    "MemoryMaintenanceSchedulerService",
    "resolve_memory_consolidation_runtime",
]
