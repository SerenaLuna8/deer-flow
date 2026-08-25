from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
import yaml
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.private_work.run_skill_tree_materializer import (
    MaterializationMemoryBudget,
)
from app.reliability import process_readiness
from app.reliability.process_readiness import (
    ProcessReadinessSnapshot,
    read_process_readiness,
)
from app.reliability.readiness import ReliabilityReadinessService
from app.reliability.workers import WorkerRegistry
from app.shared_assets.skill_archive import MAX_SKILL_ARCHIVE_BYTES
from app.worker.service import WorkerService
from deerflow.config.worker_config import (
    LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES,
    LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES,
    MIN_V4_MATERIALIZATION_INFLIGHT_BYTES,
    RELEASE_WORKER_MAX_CONCURRENT_JOBS,
    RELEASE_WORKER_PROCESS_COUNT,
    WorkerConfig,
    require_supported_worker_release_topology,
)
from deerflow.persistence.jobs.model import WorkerNodeRow
from scripts.check_local_execution_readiness import (
    probe_local_execution_readiness,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _Registry:
    def __init__(self) -> None:
        self.registration: tuple[object, ...] | None = None

    async def register(self, *args: object, **kwargs: object) -> None:
        self.registration = (*args, kwargs)


class _SchemaProbe:
    async def require_ready(self, _session: object) -> None:
        return None

    async def read(self, _session: object) -> SimpleNamespace:
        return SimpleNamespace(ready=True)


def test_release_worker_config_is_single_process_capacity_eight_and_covers_v4() -> None:
    config = WorkerConfig()

    assert RELEASE_WORKER_PROCESS_COUNT == 1
    assert RELEASE_WORKER_MAX_CONCURRENT_JOBS == 8
    assert config.max_concurrent_jobs == RELEASE_WORKER_MAX_CONCURRENT_JOBS
    assert MIN_V4_MATERIALIZATION_INFLIGHT_BYTES == MAX_SKILL_ARCHIVE_BYTES
    assert config.materialization_max_inflight_bytes == LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES == LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES
    assert config.materialization_v4_max_inflight_bytes == 256 * 1024 * 1024
    require_supported_worker_release_topology(config)

    with pytest.raises(ValueError, match="max_concurrent_jobs=8"):
        require_supported_worker_release_topology(
            WorkerConfig(max_concurrent_jobs=4),
        )
    with pytest.raises(ValueError, match="legal v4 maximum"):
        require_supported_worker_release_topology(config.model_copy(update={"materialization_v4_max_inflight_bytes": (MIN_V4_MATERIALIZATION_INFLIGHT_BYTES - 1)}))
    with pytest.raises(ValueError, match="legacy envelope"):
        require_supported_worker_release_topology(config.model_copy(update={"materialization_max_inflight_bytes": (LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES - 1)}))
    with pytest.raises(ValueError, match="v4 aggregate"):
        require_supported_worker_release_topology(config.model_copy(update={"materialization_v4_max_inflight_bytes": 300 * 1024 * 1024}))
    with pytest.raises(
        ValidationError,
        match="materialization_batch_max_bytes",
    ):
        WorkerConfig(
            materialization_max_inflight_bytes=(MIN_V4_MATERIALIZATION_INFLIGHT_BYTES),
            materialization_v4_max_inflight_bytes=(MIN_V4_MATERIALIZATION_INFLIGHT_BYTES),
            materialization_batch_max_bytes=(MIN_V4_MATERIALIZATION_INFLIGHT_BYTES + 1),
        )

    example = yaml.safe_load((_REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert example["worker"]["max_concurrent_jobs"] == 8
    assert example["worker"]["materialization_max_inflight_bytes"] == LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES
    assert example["worker"]["materialization_v4_max_inflight_bytes"] == 256 * 1024 * 1024


def test_release_worker_topology_rejects_total_materialization_budget_above_accepted_coordinate() -> None:
    config = WorkerConfig(
        materialization_max_inflight_bytes=(1536 * 1024 * 1024) + 1,
    )

    with pytest.raises(
        ValueError,
        match="accepted total materialization byte cap",
    ):
        require_supported_worker_release_topology(config)


@pytest.mark.parametrize(
    "batch_max_bytes",
    [
        (8 * 1024 * 1024) - 1,
        (8 * 1024 * 1024) + 1,
    ],
)
def test_release_worker_topology_rejects_v4_batch_byte_drift(
    batch_max_bytes: int,
) -> None:
    config = WorkerConfig(
        materialization_batch_max_bytes=batch_max_bytes,
    )

    with pytest.raises(
        ValueError,
        match="accepted v4 batch byte cap",
    ):
        require_supported_worker_release_topology(config)


@pytest.mark.parametrize(
    "batch_max_files",
    [49, 51],
)
def test_release_worker_topology_rejects_v4_batch_file_drift(
    batch_max_files: int,
) -> None:
    config = WorkerConfig(
        materialization_batch_max_files=batch_max_files,
    )

    with pytest.raises(
        ValueError,
        match="accepted v4 batch file cap",
    ):
        require_supported_worker_release_topology(config)


@pytest.mark.asyncio
async def test_worker_service_registers_release_capacity() -> None:
    registry = _Registry()
    service = WorkerService(
        None,
        registry,  # type: ignore[arg-type]
        {"private_run": object()},
        WorkerConfig(),
    )

    await service._register()

    assert registry.registration is not None
    worker_id, capabilities, capacity, kwargs = registry.registration
    assert worker_id == service.worker_id
    assert capabilities == frozenset({"private_run"})
    assert capacity == RELEASE_WORKER_MAX_CONCURRENT_JOBS
    assert kwargs == {"execution_domain_affinity": None}


@pytest.mark.asyncio
async def test_public_readiness_degrades_for_unvalidated_worker_topology() -> None:
    process = ProcessReadinessSnapshot(
        ready=False,
        role="gateway",
        worker_fleet="ready",
        worker_count=2,
        worker_capacity=16,
        worker_oldest_heartbeat_age_seconds=1,
        private_run_worker_fleet="unavailable",
        private_run_worker_count=2,
        private_run_worker_capacity=16,
        scheduler="disabled",
        scheduler_ownership="disabled",
        schema_state="ready",
    )
    readiness = await ReliabilityReadinessService(
        _SchemaProbe(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        "topology-readiness",
        stream=lambda: "ready",
        quota=lambda: "ready",
        audit=lambda: "ready",
        process=process,
    ).read()

    assert readiness.status == "degraded"
    assert readiness.worker_fleet == "ready"
    assert readiness.private_run_worker_fleet == "unavailable"


def test_eight_materialization_attempts_share_one_weighted_process_budget() -> None:
    async def scenario() -> None:
        mib = 1024 * 1024
        budget = MaterializationMemoryBudget(capacity_bytes=256 * mib)
        release = asyncio.Event()
        two_entered = asyncio.Event()
        active = 0
        maximum_active = 0
        completed = 0

        async def materialize() -> None:
            nonlocal active, completed, maximum_active
            async with budget.reserve_v4(content_size_bytes=100 * mib):
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 2:
                    two_entered.set()
                await release.wait()
                active -= 1
                completed += 1

        tasks = [asyncio.create_task(materialize()) for _ in range(8)]
        await asyncio.wait_for(two_entered.wait(), timeout=1)
        await asyncio.sleep(0)

        assert active == 2
        assert budget.in_use_bytes == 200 * mib
        assert budget.in_use_bytes <= budget.capacity_bytes

        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)

        assert completed == 8
        assert maximum_active == 2
        assert budget.peak_in_use_bytes == 200 * mib
        assert budget.in_use_bytes == 0

    asyncio.run(scenario())


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_registry_readback_accepts_only_exact_release_topology(
    migrated_postgres_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_worker_id = uuid.uuid4()
    second_worker_id = uuid.uuid4()
    observed_at = datetime.now(UTC)
    registry = WorkerRegistry(factory, version="materialization-topology-test")
    monkeypatch.setattr(
        process_readiness,
        "FinalSchemaProbe",
        lambda: _SchemaProbe(),
    )
    try:
        await registry.register(
            first_worker_id,
            frozenset({"private_run"}),
            RELEASE_WORKER_MAX_CONCURRENT_JOBS,
            execution_domain_affinity=None,
            now=observed_at,
        )

        async with factory() as session:
            registered_capacity = await session.scalar(
                sa.select(WorkerNodeRow.max_concurrent_jobs).where(
                    WorkerNodeRow.id == first_worker_id,
                )
            )
            assert registered_capacity == RELEASE_WORKER_MAX_CONCURRENT_JOBS
            assert await probe_local_execution_readiness(
                session,
                worker_fresh_for_seconds=60,
                schema_probe=_SchemaProbe(),
            )
            process = await read_process_readiness(
                session,
                role="gateway",
                scheduler_enabled=False,
                worker_fresh_for_seconds=60,
                now=observed_at,
            )
            assert process.ready is True
            assert process.private_run_worker_count == 1
            assert process.private_run_worker_capacity == 8

        await registry.register(
            second_worker_id,
            frozenset({"private_run"}),
            RELEASE_WORKER_MAX_CONCURRENT_JOBS,
            execution_domain_affinity=None,
            now=observed_at,
        )

        async with factory() as session:
            assert not await probe_local_execution_readiness(
                session,
                worker_fresh_for_seconds=60,
                schema_probe=_SchemaProbe(),
            )
            process = await read_process_readiness(
                session,
                role="gateway",
                scheduler_enabled=False,
                worker_fresh_for_seconds=60,
                now=observed_at,
            )
            assert process.ready is False
            assert process.private_run_worker_fleet == "unavailable"
            assert process.private_run_worker_count == 2
            assert process.private_run_worker_capacity == 16
    finally:
        await engine.dispose()
