from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.private_work.run_skill_tree_materializer import (
    MaterializationAttemptIdentity,
    RunSkillTreeMaterializer,
)
from app.private_work.run_skill_tree_orphan_reaper import (
    RunSkillTreeOrphanReaper,
    _owner_advisory_lock_key,
)
from deerflow.config.worker_config import WorkerConfig
from deerflow.sandbox.local.local_sandbox_provider import LocalSandboxProvider
from deerflow.sandbox.sandbox_provider import (
    Orphaned,
    ProviderRunMountLease,
    ProviderRunMountOwnerUnknown,
)


class _UnknownProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, ProviderRunMountLease | None]] = []

    async def ensure_run_readonly_mount_owner_absent_async(
        self,
        owner_id: uuid.UUID,
        *,
        persisted_lease: ProviderRunMountLease | None,
    ) -> ProviderRunMountOwnerUnknown:
        self.calls.append((owner_id, persisted_lease))
        return ProviderRunMountOwnerUnknown(
            owner_id=owner_id,
            provider_kind=(persisted_lease.provider_kind if persisted_lease is not None else None),
            reason_code="owner_readback_unknown",
        )


class _RuntimeSlot:
    def __init__(self) -> None:
        self.tree = None

    def adopt_materialized_skill_tree(self, tree) -> None:
        self.tree = tree


async def _create_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """CREATE TABLE jobs (
                       id uuid PRIMARY KEY,
                       job_type text NOT NULL,
                       status text NOT NULL,
                       lease_owner_id uuid,
                       lease_token_hash text,
                       lease_expires_at timestamptz
                   )""",
            )
        )
        await connection.execute(
            text(
                """CREATE TABLE job_attempts (
                       id uuid PRIMARY KEY,
                       job_id uuid NOT NULL,
                       worker_id uuid NOT NULL,
                       lease_token_hash text NOT NULL,
                       finished_at timestamptz
                   )""",
            )
        )


async def _owner(
    root: Path,
    *,
    state: str,
) -> tuple[MaterializationAttemptIdentity, uuid.UUID]:
    materializer = RunSkillTreeMaterializer(
        materialization_root=root,
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )
    builder = await materializer.begin_attempt(identity)
    owner_id = builder.owner_id
    if state == "materializing":
        return identity, owner_id
    await builder.write_file(
        "custom/exact/SKILL.md",
        b"---\nname: exact\n---\n",
    )
    pending = await builder.publish(manifests=(), skills=())
    if state == "materialized":
        return identity, owner_id
    runtime = pending.transfer_to(_RuntimeSlot())
    await runtime.persist_mount_acquiring()
    if state == "acquiring":
        return identity, owner_id
    lease = ProviderRunMountLease(
        owner_id=owner_id,
        provider_kind="local",
        sandbox_id=f"local-run:{owner_id.hex}",
        mount_lease_id=uuid.uuid4().hex,
    )
    await runtime.persist_mount_mounted(lease)
    if state == "mounted":
        return identity, owner_id
    await runtime.finalize(
        Orphaned.from_lease(
            lease,
            reason_code="release_readback_unknown",
            last_lifecycle_state="mounted",
        )
    )
    if state != "release_pending":
        raise ValueError("Unsupported test owner state")
    return identity, owner_id


async def _seed_active_attempt(
    engine: AsyncEngine,
    identity: MaterializationAttemptIdentity,
) -> None:
    lease_hash = "a" * 64
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO jobs (
                       id, job_type, status, lease_owner_id,
                       lease_token_hash, lease_expires_at
                   ) VALUES (
                       :job_id, 'private_run', 'running', :worker_id,
                       :lease_hash, clock_timestamp() + interval '10 minutes'
                   )""",
            ),
            {
                "job_id": identity.job_id,
                "worker_id": identity.worker_id,
                "lease_hash": lease_hash,
            },
        )
        await connection.execute(
            text(
                """INSERT INTO job_attempts (
                       id, job_id, worker_id, lease_token_hash, finished_at
                   ) VALUES (
                       :attempt_id, :job_id, :worker_id, :lease_hash, NULL
                   )""",
            ),
            {
                "attempt_id": identity.attempt_id,
                "job_id": identity.job_id,
                "worker_id": identity.worker_id,
                "lease_hash": lease_hash,
            },
        )


@pytest.mark.asyncio
async def test_reaper_deletes_only_inactive_proven_owners_and_is_idempotent(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    root = tmp_path / "run-skill-materializations"
    provider = _UnknownProvider()
    try:
        await _create_tables(engine)
        _inactive_identity, inactive = await _owner(
            root,
            state="materializing",
        )
        active_identity, active = await _owner(root, state="materialized")
        await _seed_active_attempt(engine, active_identity)
        _unknown_identity, unknown = await _owner(root, state="acquiring")

        # The zero-grace comparison pits the owners' process-clock updated_at
        # against PostgreSQL clock_timestamp(); give the wall clock a moment
        # so a sub-millisecond cross-clock skew cannot park fresh owners in
        # preserved_grace and flake the dispositions below under full-suite
        # load.
        await asyncio.sleep(0.1)

        report = await RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=root,
            provider=provider,
            grace_seconds=0,
        ).reap_startup()

        assert report.scanned == 3
        assert report.deleted_never_acquired == 1
        assert report.preserved_active == 1
        assert report.preserved_unknown == 1
        assert not (root / inactive.hex).exists()
        assert (root / active.hex).is_dir()
        assert (root / unknown.hex).is_dir()
        assert provider.calls == [(unknown, None)]

        second = await RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=root,
            provider=provider,
            grace_seconds=3600,
        ).reap_startup()
        assert second.deleted == 0
        assert second.preserved_grace == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reaper_requires_session_advisory_lock_then_local_absence_proof(
    postgres_database_url: str,
    tmp_path: Path,
) -> None:
    engine = create_async_engine(postgres_database_url)
    root = tmp_path / "run-skill-materializations"
    try:
        await _create_tables(engine)
        _identity, owner_id = await _owner(root, state="mounted")
        key = _owner_advisory_lock_key(owner_id)
        async with engine.connect() as holder:
            await holder.execute(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": key},
            )
            await holder.rollback()
            locked = await RunSkillTreeOrphanReaper(
                engine=engine,
                materialization_root=root,
                provider=LocalSandboxProvider(),
                grace_seconds=0,
            ).reap_startup()
            assert locked.preserved_lock == 1
            assert (root / owner_id.hex).is_dir()
            assert (
                await holder.scalar(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": key},
                )
            ) is True

        reaped = await RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=root,
            provider=LocalSandboxProvider(),
            grace_seconds=0,
        ).reap_startup()
        assert reaped.deleted_provider_absent == 1
        assert not (root / owner_id.hex).exists()
        assert (
            await RunSkillTreeOrphanReaper(
                engine=engine,
                materialization_root=root,
                provider=LocalSandboxProvider(),
                grace_seconds=0,
            ).reap_startup()
        ).scanned == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_reaper_rejects_any_non_dedicated_scan_root(
    tmp_path: Path,
) -> None:
    engine = create_async_engine("postgresql+asyncpg://invalid@localhost/invalid")
    try:
        with pytest.raises(ValueError, match="configuration"):
            RunSkillTreeOrphanReaper(
                engine=engine,
                materialization_root=tmp_path,
                provider=_UnknownProvider(),
                grace_seconds=0,
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_database_unavailable_preserves_owner_root(tmp_path: Path) -> None:
    root = tmp_path / "run-skill-materializations"
    _identity, owner_id = await _owner(root, state="materializing")
    engine = create_async_engine(
        "postgresql+asyncpg://invalid@127.0.0.1:1/invalid",
        connect_args={"timeout": 0.1},
    )
    try:
        report = await RunSkillTreeOrphanReaper(
            engine=engine,
            materialization_root=root,
            provider=_UnknownProvider(),
            grace_seconds=0,
        ).reap_startup()
        assert report.preserved_unknown == 1
        assert (root / owner_id.hex).is_dir()
    finally:
        await engine.dispose()
