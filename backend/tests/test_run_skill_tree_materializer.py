from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import shutil
import stat
import threading
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.private_work import run_skill_tree_materializer as materializer_module
from app.private_work.asset_runtime import PrivateAssetRuntime
from app.private_work.run_skill_tree_materializer import (
    LegacyInlineRunSkillPlan,
    LegacyInlineRunSkillSourceAdapter,
    MaterializationAttemptIdentity,
    MaterializationAuthorityReadback,
    MaterializationMemoryBudget,
    PinnedSkillVersionPlan,
    PinnedSkillVersionSourceAdapter,
    RunSkillTreeMaterializationPlan,
    RunSkillTreeMaterializer,
    SkillVersionFileContent,
    SkillVersionFileMetadata,
)
from app.shared_assets.models import (
    AssetScope,
    SkillSecretRequirementSnapshot,
)
from app.shared_assets.skill_version_facts import skill_version_archive_facts
from deerflow.config.worker_config import (
    LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES,
    LEGACY_V2_MATERIALIZATION_ENVELOPE_BYTES,
    LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES,
    WorkerConfig,
)
from deerflow.sandbox import (
    NotAcquired,
    Orphaned,
    ProviderMountAbsentProof,
    ProviderRunMountLease,
    Released,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_legacy_source_contract_keeps_large_json_out_of_the_control_loader() -> None:
    adapter_source = inspect.getsource(
        LegacyInlineRunSkillSourceAdapter._read_exact_snapshot,
    )
    control_source = inspect.getsource(PrivateAssetRuntime._legacy_skill_plan)

    assert "select(asset.snapshot_json)" in adapter_source
    assert "~exists().where" in adapter_source
    assert "snapshot_json" not in control_source
    assert "version.secret_requirements" in control_source
    assert "~exists().where" in control_source
    for source in (adapter_source, control_source):
        assert "current_version_id" not in source
        assert "update(" not in source
        assert "insert(" not in source
        assert "delete(" not in source


def test_v4_source_reserves_before_any_version_metadata_query() -> None:
    source = inspect.getsource(
        PinnedSkillVersionSourceAdapter.materialize_version,
    )

    assert source.index("memory_budget.reserve_v4") < source.index("self._session_factory()")


def test_asset_plan_and_boundary_fingerprints_use_atomic_control_transactions() -> None:
    plan_source = inspect.getsource(PrivateAssetRuntime.materialize)
    fingerprint_source = inspect.getsource(
        PrivateAssetRuntime._read_plan_fingerprint_in_session,
    )

    assert plan_source.index('lock_mode="share"') < plan_source.index("await execution_suffix")
    assert plan_source.index("await execution_suffix") < plan_source.index("self._asset_facts")
    assert plan_source.index("self._asset_facts") < plan_source.index("plan_fingerprint = _asset_plan_fingerprint")
    assert "assert_materialization_attempt_active" not in plan_source
    assert "before_checkpoint_read" not in plan_source
    assert "_materialization_attempt_identity" not in plan_source
    assert "_session_factory" not in fingerprint_source


def test_legacy_plan_accepts_only_bounded_v2_or_v3_version_facts() -> None:
    plan = LegacyInlineRunSkillPlan(
        dependency_order=1,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        payload_checksum="a" * 64,
        catalog_generation=9,
        snapshot_schema_version=2,
        file_count=1,
        content_size_bytes=42,
        secret_requirements=(),
    )

    assert plan.snapshot_schema_version == 2
    assert replace(plan, snapshot_schema_version=3).snapshot_schema_version == 3
    with pytest.raises(ValueError, match="legacy inline"):
        replace(plan, snapshot_schema_version=4)
    with pytest.raises(ValueError, match="legacy inline"):
        replace(plan, snapshot_schema_version=2.0)  # type: ignore[arg-type]


def test_v4_materialization_plan_is_closed_and_attempt_bound() -> None:
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )
    version = PinnedSkillVersionPlan(
        dependency_order=1,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        payload_checksum="a" * 64,
        catalog_generation=9,
        dependency_version_ids=(),
        file_count=1,
        content_size_bytes=42,
        secret_requirements=(
            SkillSecretRequirementSnapshot(
                name="API_TOKEN",
                target_env="PINNED_API_TOKEN",
                optional=False,
            ),
        ),
    )
    plan = RunSkillTreeMaterializationPlan(
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        thread_id="thread-v4",
        run_id="run-v4",
        runtime_kind="chat",
        attempt_identity=identity,
        plan_fingerprint="b" * 64,
        skill_versions=(version,),
    )

    assert plan.skill_versions == (version,)
    assert (
        MaterializationAuthorityReadback(
            attempt_identity=identity,
            plan_fingerprint=plan.plan_fingerprint,
        ).attempt_identity
        == identity
    )


def test_materialize_cancelled_at_version_authority_removes_unique_owner(
    tmp_path: Path,
) -> None:
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )
    version = PinnedSkillVersionPlan(
        dependency_order=1,
        scope=AssetScope.PROJECT,
        asset_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        payload_checksum="a" * 64,
        catalog_generation=9,
        dependency_version_ids=(),
        file_count=1,
        content_size_bytes=42,
        secret_requirements=(),
    )
    plan = RunSkillTreeMaterializationPlan(
        project_id=uuid.uuid4(),
        owner_user_id=str(uuid.uuid4()),
        thread_id="thread-cancel",
        run_id="run-cancel",
        runtime_kind="chat",
        attempt_identity=identity,
        plan_fingerprint="b" * 64,
        skill_versions=(version,),
    )
    readback = MaterializationAuthorityReadback(
        attempt_identity=identity,
        plan_fingerprint=plan.plan_fingerprint,
    )
    version_boundary_entered = asyncio.Event()

    class BlockingAuthority:
        async def read_materialization_authority(
            self,
            *,
            boundary: str,
            dependency_order: int | None,
        ) -> MaterializationAuthorityReadback:
            del dependency_order
            if boundary == "version":
                version_boundary_entered.set()
                await asyncio.Future()
            return readback

    materialization_root = tmp_path / "materializations"
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
        pinned_source_adapter=PinnedSkillVersionSourceAdapter(lambda: None),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            materializer.materialize(
                plan=plan,
                authority=BlockingAuthority(),  # type: ignore[arg-type]
            )
        )
        await version_boundary_entered.wait()
        assert len(tuple(materialization_root.iterdir())) == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(materialization_root.iterdir())

    asyncio.run(scenario())


def test_weighted_materialization_budget_bounds_wait_cancel_and_finally() -> None:
    async def scenario() -> None:
        budget = MaterializationMemoryBudget(capacity_bytes=100)
        holder_entered = asyncio.Event()
        release_holder = asyncio.Event()
        legacy_read_started = asyncio.Event()

        async def hold_v4() -> None:
            async with budget.reserve_v4(content_size_bytes=80):
                holder_entered.set()
                await release_holder.wait()

        async def read_legacy() -> None:
            async with budget.reserve_legacy(envelope_bytes=30):
                legacy_read_started.set()

        holder = asyncio.create_task(hold_v4())
        await holder_entered.wait()
        waiter = asyncio.create_task(read_legacy())
        await asyncio.sleep(0)

        assert budget.in_use_bytes == 80
        assert budget.peak_in_use_bytes == 80
        assert not legacy_read_started.is_set()

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert budget.in_use_bytes == 80

        release_holder.set()
        await holder
        assert budget.in_use_bytes == 0

        with pytest.raises(RuntimeError, match="source failed"):
            async with budget.reserve_legacy(envelope_bytes=100):
                assert budget.in_use_bytes == 100
                raise RuntimeError("source failed")
        assert budget.in_use_bytes == 0
        assert budget.peak_in_use_bytes == 100

    asyncio.run(scenario())


def test_dual_reader_total_gate_preserves_the_smaller_v4_aggregate() -> None:
    async def scenario() -> None:
        budget = MaterializationMemoryBudget(
            capacity_bytes=768,
            v4_capacity_bytes=256,
        )
        holders_entered = asyncio.Event()
        release_holders = asyncio.Event()
        third_v4_entered = asyncio.Event()
        legacy_entered = asyncio.Event()
        active_holders = 0

        async def hold_v4() -> None:
            nonlocal active_holders
            async with budget.reserve_v4(content_size_bytes=128):
                active_holders += 1
                if active_holders == 2:
                    holders_entered.set()
                await release_holders.wait()

        async def third_v4() -> None:
            async with budget.reserve_v4(content_size_bytes=1):
                third_v4_entered.set()

        async def legacy() -> None:
            async with budget.reserve_legacy(envelope_bytes=768):
                legacy_entered.set()

        holders = [asyncio.create_task(hold_v4()) for _ in range(2)]
        await holders_entered.wait()
        v4_waiter = asyncio.create_task(third_v4())
        legacy_waiter = asyncio.create_task(legacy())
        await asyncio.sleep(0)

        assert budget.in_use_bytes == 256
        assert budget.v4_in_use_bytes == 256
        assert not third_v4_entered.is_set()
        assert not legacy_entered.is_set()

        release_holders.set()
        await asyncio.gather(*holders, v4_waiter, legacy_waiter)

        assert third_v4_entered.is_set()
        assert legacy_entered.is_set()
        assert budget.peak_v4_in_use_bytes == 256
        assert budget.peak_in_use_bytes == 768
        assert budget.in_use_bytes == 0
        assert budget.v4_in_use_bytes == 0

    asyncio.run(scenario())


def test_materializers_share_one_process_budget_before_source_reads(
    tmp_path: Path,
) -> None:
    config = WorkerConfig()
    first = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=config,
    )
    second = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=config,
    )

    async def scenario() -> None:
        v4_read_started = asyncio.Event()
        allow_v4_read_to_finish = asyncio.Event()
        legacy_read_started = asyncio.Event()

        async def read_v4() -> None:
            async with first.reserve_v4_source(
                content_size_bytes=200 * 1024 * 1024,
            ):
                v4_read_started.set()
                await allow_v4_read_to_finish.wait()

        async def read_legacy() -> None:
            async with second.reserve_legacy_source(
                envelope_bytes=(LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES),
            ):
                legacy_read_started.set()

        v4_task = asyncio.create_task(read_v4())
        await v4_read_started.wait()
        legacy_task = asyncio.create_task(read_legacy())
        await asyncio.sleep(0)
        assert not legacy_read_started.is_set()

        allow_v4_read_to_finish.set()
        await v4_task
        await legacy_task
        assert legacy_read_started.is_set()

    asyncio.run(scenario())


def test_worker_materialization_limits_cover_dual_reader_without_widening_v4() -> None:
    config = WorkerConfig()

    assert LEGACY_V2_MATERIALIZATION_ENVELOPE_BYTES == 1536 * 1024 * 1024
    assert LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES == 1536 * 1024 * 1024
    assert LEGACY_MATERIALIZATION_EXCLUSIVE_RESERVATION_BYTES == LEGACY_V3_MATERIALIZATION_ENVELOPE_BYTES
    assert config.materialization_max_inflight_bytes == 1536 * 1024 * 1024
    assert config.materialization_v4_max_inflight_bytes == 256 * 1024 * 1024
    assert config.materialization_batch_max_bytes == 8 * 1024 * 1024
    assert config.materialization_batch_max_files == 50

    with pytest.raises(ValidationError, match="materialization_max_inflight_bytes"):
        WorkerConfig(materialization_max_inflight_bytes=100 * 1024 * 1024 - 1)
    with pytest.raises(ValidationError):
        WorkerConfig(materialization_batch_max_bytes=0)
    with pytest.raises(ValidationError):
        WorkerConfig(materialization_batch_max_files=0)
    with pytest.raises(
        ValidationError,
        match="materialization_v4_max_inflight_bytes",
    ):
        WorkerConfig(
            materialization_max_inflight_bytes=200 * 1024 * 1024,
            materialization_v4_max_inflight_bytes=256 * 1024 * 1024,
        )

    worker = yaml.safe_load((_REPO_ROOT / "config.example.yaml").read_text(encoding="utf-8"))["worker"]
    assert worker["materialization_max_inflight_bytes"] == 1536 * 1024 * 1024
    assert worker["materialization_v4_max_inflight_bytes"] == 256 * 1024 * 1024
    assert worker["materialization_batch_max_bytes"] == 8 * 1024 * 1024
    assert worker["materialization_batch_max_files"] == 50

    with pytest.raises(ValueError, match="legacy envelope"):
        RunSkillTreeMaterializer(
            materialization_root=Path("/tmp/legacy-budget-test"),
            worker_config=WorkerConfig(
                materialization_max_inflight_bytes=256 * 1024 * 1024,
            ),
            legacy_source_adapter=LegacyInlineRunSkillSourceAdapter(
                lambda: None,
            ),  # type: ignore[arg-type]
        )


def test_v4_batch_plan_bounds_rows_bytes_and_oversized_singletons(
    tmp_path: Path,
) -> None:
    materializer = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=WorkerConfig(
            materialization_batch_max_bytes=10,
            materialization_batch_max_files=3,
        ),
    )
    metadata = tuple(
        SkillVersionFileMetadata(
            path=f"custom/skill/{index:02d}.txt",
            media_type="text/plain",
            size_bytes=size,
            sha256=f"{index + 1:064x}",
        )
        for index, size in enumerate((4, 4, 4, 12, 2))
    )

    batches = materializer.plan_v4_content_batches(metadata)

    assert [batch.expected_paths for batch in batches] == [
        ("custom/skill/00.txt", "custom/skill/01.txt"),
        ("custom/skill/02.txt",),
        ("custom/skill/03.txt",),
        ("custom/skill/04.txt",),
    ]
    assert [batch.content_size_bytes for batch in batches] == [8, 4, 12, 2]
    assert [batch.oversized_singleton for batch in batches] == [
        False,
        False,
        True,
        False,
    ]

    too_large = SkillVersionFileMetadata(
        path="custom/skill/too-large.bin",
        media_type="application/octet-stream",
        size_bytes=64 * 1024 * 1024 + 1,
        sha256="f" * 64,
    )
    with pytest.raises(ValueError, match="64 MiB"):
        materializer.plan_v4_content_batches((too_large,))


def test_v4_query_ranges_bound_a_simulated_fetch_50_full_result(
    tmp_path: Path,
) -> None:
    materializer = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=WorkerConfig(
            materialization_batch_max_bytes=13,
            materialization_batch_max_files=7,
        ),
    )
    contents = tuple(
        SkillVersionFileContent(
            path=f"files/{index:03d}.txt",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        )
        for index in range(120)
        for content in [bytes([index % 251]) * (index % 3 + 1)]
    )
    metadata = tuple(row.metadata(media_type="text/plain") for row in contents)

    batches = materializer.plan_v4_content_batches(metadata)

    for batch in batches:
        # asyncpg may prefetch 50 decoded rows, but the SQL range itself can
        # only return this already-bounded complete result.
        query_result = tuple(row for row in contents if batch.first_path <= row.path <= batch.last_path)
        simulated_fetch_50 = query_result[:50]
        assert simulated_fetch_50 == query_result
        materializer.validate_v4_content_batch(batch, query_result)
        assert len(query_result) <= 7
        assert sum(row.size_bytes for row in query_result) <= 13

    first = batches[0]
    unexpected_extra = tuple(row for row in contents if first.first_path <= row.path <= batches[1].first_path)
    with pytest.raises(ValueError, match="query result"):
        materializer.validate_v4_content_batch(first, unexpected_extra)


def test_incremental_checksum_matches_existing_unicode_canonical_contract(
    tmp_path: Path,
) -> None:
    materializer = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=WorkerConfig(),
    )
    metadata = (
        SkillVersionFileMetadata(
            path="SKILL.md",
            media_type="text/markdown",
            size_bytes=17,
            sha256="1" * 64,
        ),
        SkillVersionFileMetadata(
            path="自定义/技能/说明-🦌.txt",
            media_type="text/plain",
            size_bytes=29,
            sha256="a" * 64,
        ),
    )
    existing = skill_version_archive_facts(tuple((row.path, row.sha256, row.size_bytes) for row in metadata))

    incremental = materializer.archive_facts(metadata)

    assert incremental.file_count == existing.file_count
    assert incremental.content_size_bytes == existing.content_size_bytes
    assert incremental.payload_checksum == existing.payload_checksum


def test_materializer_publishes_random_durable_read_only_owner_tree(
    tmp_path: Path,
) -> None:
    materialization_root = tmp_path / "materializations"
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.UUID("80000000-0000-0000-0000-000000000001"),
        attempt_id=uuid.UUID("80000000-0000-0000-0000-000000000002"),
        worker_id=uuid.UUID("80000000-0000-0000-0000-000000000003"),
    )

    async def scenario() -> None:
        builder = await materializer.begin_attempt(identity)
        other = await materializer.begin_attempt(identity)
        assert builder.owner_id != other.owner_id
        await other.aclose()
        assert not (materialization_root / other.owner_id.hex).exists()

        materializing = await materializer.inspect_owner(builder.owner_id)
        assert materializing.state == "materializing"
        assert materializing.job_id == identity.job_id
        assert materializing.attempt_id == identity.attempt_id
        assert materializing.worker_id == identity.worker_id
        assert materializing.state_generation == 1
        assert materializing.created_at == materializing.updated_at

        asset_id = uuid.UUID("80000000-0000-0000-0000-000000000004")
        await builder.write_file(
            f"custom/{asset_id.hex}/SKILL.md",
            b"---\nname: exact-skill\n---\n",
        )
        await builder.write_file(
            f"custom/{asset_id.hex}/notes/说明.txt",
            "内容".encode(),
        )
        staging_root = materialization_root / builder.owner_id.hex / ".staging"
        assert stat.S_IMODE(staging_root.stat().st_mode) == 0o700
        assert stat.S_IMODE((staging_root / f"custom/{asset_id.hex}/SKILL.md").stat().st_mode) == 0o600
        pending = await builder.publish(manifests=(), skills=())

        source = pending.source
        owner_root = source.worker_root.parent
        metadata_path = owner_root / "metadata.json"
        assert source.owner_id == builder.owner_id
        assert source.worker_root == materialization_root / builder.owner_id.hex / "tree"
        assert stat.S_IMODE(owner_root.stat().st_mode) == 0o700
        assert stat.S_IMODE(metadata_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(source.worker_root.stat().st_mode) == 0o555
        for path in source.worker_root.rglob("*"):
            expected_mode = 0o555 if path.is_dir() else 0o444
            assert not path.is_symlink()
            assert stat.S_IMODE(path.stat().st_mode) == expected_mode
        assert (source.worker_root / ".actweave-run-mount.json").is_file()
        assert (source.worker_root / f"custom/{asset_id.hex}/SKILL.md").read_bytes().startswith(b"---")
        materialized = await materializer.inspect_owner(builder.owner_id)
        assert materialized.state == "materialized"
        assert materialized.state_generation == 2
        assert materialized.created_at <= materialized.updated_at

        await pending.aclose()
        assert not owner_root.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe_kind", ["symlink", "fifo"])
def test_materializer_rejects_links_and_special_entries_before_publish(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    materialization_root = tmp_path / "materializations"
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )

    async def scenario() -> None:
        builder = await materializer.begin_attempt(identity)
        await builder.write_file(
            "custom/skill/SKILL.md",
            b"---\nname: exact-skill\n---\n",
        )
        owner_root = materialization_root / builder.owner_id.hex
        unsafe = owner_root / ".staging" / "custom" / "skill" / "unsafe"
        if unsafe_kind == "symlink":
            unsafe.symlink_to(tmp_path / "outside")
        else:
            os.mkfifo(unsafe, mode=0o600)

        with pytest.raises(ValueError, match="link or special"):
            await builder.publish(manifests=(), skills=())
        assert not owner_root.exists()

    asyncio.run(scenario())


def test_cancelled_publish_joins_file_thread_before_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    materialization_root = tmp_path / "materializations"
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )
    rename_entered = threading.Event()
    allow_rename = threading.Event()
    original_replace = materializer_module.os.replace

    def blocking_replace(source: object, destination: object) -> None:
        if Path(source).name == ".staging":
            rename_entered.set()
            assert allow_rename.wait(timeout=5)
        original_replace(source, destination)

    monkeypatch.setattr(materializer_module.os, "replace", blocking_replace)

    async def scenario() -> None:
        builder = await materializer.begin_attempt(identity)
        await builder.write_file(
            "custom/skill/SKILL.md",
            b"---\nname: exact-skill\n---\n",
        )
        owner_root = materialization_root / builder.owner_id.hex
        publish = asyncio.create_task(builder.publish(manifests=(), skills=()))
        assert await asyncio.to_thread(rename_entered.wait, 5)

        publish.cancel()
        await asyncio.sleep(0)
        assert not publish.done()
        assert owner_root.exists()

        allow_rename.set()
        with pytest.raises(asyncio.CancelledError):
            await publish
        assert not owner_root.exists()

    asyncio.run(scenario())


def test_owner_metadata_transitions_fsync_file_before_and_directory_after_rename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    materializer = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )
    events: list[str] = []
    original_fsync = materializer_module.os.fsync
    original_replace = materializer_module.os.replace

    def tracked_fsync(descriptor: int) -> None:
        events.append("fsync")
        original_fsync(descriptor)

    def tracked_replace(source: object, destination: object) -> None:
        if Path(destination).name == "metadata.json":
            events.append("metadata_replace")
        original_replace(source, destination)

    monkeypatch.setattr(materializer_module.os, "fsync", tracked_fsync)
    monkeypatch.setattr(materializer_module.os, "replace", tracked_replace)

    async def scenario() -> None:
        builder = await materializer.begin_attempt(identity)
        await builder.write_file(
            "custom/skill/SKILL.md",
            b"---\nname: exact-skill\n---\n",
        )
        pending = await builder.publish(manifests=(), skills=())

        metadata_replaces = [index for index, event in enumerate(events) if event == "metadata_replace"]
        assert len(metadata_replaces) == 2
        for index in metadata_replaces:
            assert events[index - 1] == "fsync"
            assert events[index + 1] == "fsync"
        await pending.aclose()

    asyncio.run(scenario())


def test_pending_transfer_is_strong_and_has_one_cleanup_owner(
    tmp_path: Path,
) -> None:
    materialization_root = tmp_path / "materializations"
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )

    async def publish_one():
        builder = await materializer.begin_attempt(identity)
        await builder.write_file(
            "custom/skill/SKILL.md",
            b"---\nname: exact-skill\n---\n",
        )
        return await builder.publish(manifests=(), skills=())

    class RejectBeforeWrite:
        def adopt_materialized_skill_tree(self, tree: object) -> None:
            del tree
            raise RuntimeError("runtime slot is occupied")

    class EmptyRuntimeSlot:
        def __init__(self) -> None:
            self.tree = None

        def adopt_materialized_skill_tree(self, tree: object) -> None:
            if self.tree is not None:
                raise RuntimeError("runtime slot is occupied")
            self.tree = tree

    class WriteThenRaise:
        def __init__(self) -> None:
            self.tree = None

        def adopt_materialized_skill_tree(self, tree: object) -> None:
            self.tree = tree
            raise RuntimeError("fault after slot write")

    async def scenario() -> None:
        rejected = await publish_one()
        rejected_root = rejected.source.worker_root.parent
        with pytest.raises(RuntimeError, match="slot is occupied"):
            rejected.transfer_to(RejectBeforeWrite())
        assert rejected_root.exists()
        await rejected.aclose()
        await rejected.aclose()
        assert not rejected_root.exists()
        with pytest.raises(RuntimeError, match="not active"):
            rejected.transfer_to(EmptyRuntimeSlot())

        violated_owner_contract = await publish_one()
        violated_root = violated_owner_contract.source.worker_root.parent
        write_then_raise = WriteThenRaise()
        with pytest.raises(RuntimeError, match="fault after slot write"):
            violated_owner_contract.transfer_to(write_then_raise)
        assert write_then_raise.tree is not None
        with pytest.raises(RuntimeError, match="not active"):
            await write_then_raise.tree.finalize(  # type: ignore[union-attr]
                NotAcquired(owner_id=violated_owner_contract.source.owner_id)
            )
        await violated_owner_contract.aclose()
        assert not violated_root.exists()

        pending = await publish_one()
        owner_root = pending.source.worker_root.parent
        owner = EmptyRuntimeSlot()
        runtime = pending.transfer_to(owner)
        assert owner.tree is runtime
        await pending.aclose()
        assert owner_root.exists()
        with pytest.raises(RuntimeError, match="not active"):
            pending.transfer_to(EmptyRuntimeSlot())

        assert not await runtime.provider_acquire_may_have_started()
        await runtime.finalize(NotAcquired(owner_id=pending.source.owner_id))
        assert not owner_root.exists()

    asyncio.run(scenario())


def test_runtime_finalize_deletes_only_matching_proof_and_hands_off_orphans(
    tmp_path: Path,
) -> None:
    materialization_root = tmp_path / "materializations"
    materializer = RunSkillTreeMaterializer(
        materialization_root=materialization_root,
        worker_config=WorkerConfig(),
    )
    identity = MaterializationAttemptIdentity(
        job_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        worker_id=uuid.uuid4(),
    )

    class EmptyRuntimeSlot:
        def __init__(self) -> None:
            self.tree = None

        def adopt_materialized_skill_tree(self, tree: object) -> None:
            if self.tree is not None:
                raise RuntimeError("runtime slot is occupied")
            self.tree = tree

    async def runtime_tree():
        builder = await materializer.begin_attempt(identity)
        await builder.write_file(
            "custom/skill/SKILL.md",
            b"---\nname: exact-skill\n---\n",
        )
        pending = await builder.publish(manifests=(), skills=())
        return pending.transfer_to(EmptyRuntimeSlot())

    async def scenario() -> None:
        released_runtime = await runtime_tree()
        released_root = released_runtime.source.worker_root.parent
        wrong_owner = uuid.uuid4()
        with pytest.raises(RuntimeError, match="does not match"):
            await released_runtime.finalize(NotAcquired(owner_id=wrong_owner))
        assert released_root.exists()

        lease = ProviderRunMountLease(
            owner_id=released_runtime.source.owner_id,
            provider_kind="local",
            sandbox_id="local-run:owner:thread:run",
            mount_lease_id=uuid.uuid4().hex,
        )
        with pytest.raises(RuntimeError, match="lifecycle state"):
            await released_runtime.finalize(
                Released(
                    proof=ProviderMountAbsentProof.from_lease(lease),
                )
            )
        await released_runtime.persist_mount_acquiring()
        assert (await materializer.inspect_owner(released_runtime.source.owner_id)).state == "acquiring"
        assert await released_runtime.read_mount_lifecycle_state() == "acquiring"
        assert await released_runtime.provider_acquire_may_have_started()
        with pytest.raises(RuntimeError, match="Not-acquired proof requires materialized"):
            await released_runtime.finalize(NotAcquired(owner_id=released_runtime.source.owner_id))
        assert released_root.exists()
        await released_runtime.persist_mount_mounted(lease)
        mounted = await materializer.inspect_owner(released_runtime.source.owner_id)
        assert mounted.state == "mounted"
        assert mounted.state_generation == 4
        assert mounted.provider_kind == lease.provider_kind
        assert mounted.sandbox_id == lease.sandbox_id
        assert mounted.mount_lease_id == lease.mount_lease_id
        assert await released_runtime.read_mount_lifecycle_state() == "mounted"
        await released_runtime.finalize(Released(proof=ProviderMountAbsentProof.from_lease(lease)))
        assert not released_root.exists()

        acquiring_runtime = await runtime_tree()
        acquiring_root = acquiring_runtime.source.worker_root.parent
        acquiring_lease = ProviderRunMountLease(
            owner_id=acquiring_runtime.source.owner_id,
            provider_kind="local",
            sandbox_id="local-acquiring-release",
            mount_lease_id=uuid.uuid4().hex,
        )
        await acquiring_runtime.persist_mount_acquiring()
        await acquiring_runtime.finalize(
            Released(
                proof=ProviderMountAbsentProof.from_lease(
                    acquiring_lease,
                )
            )
        )
        assert not acquiring_root.exists()

        acquiring_orphan = await runtime_tree()
        acquiring_orphan_root = acquiring_orphan.source.worker_root.parent
        await acquiring_orphan.persist_mount_acquiring()
        await acquiring_orphan.finalize(
            Orphaned(
                owner_id=acquiring_orphan.source.owner_id,
                reason_code="acquire_readback_unknown",
                last_lifecycle_state="acquiring",
            )
        )
        acquiring_pending = await materializer.inspect_owner(acquiring_orphan.source.owner_id)
        assert acquiring_pending.state == "release_pending"
        assert acquiring_pending.provider_kind is None

        orphaned_runtime = await runtime_tree()
        orphaned_root = orphaned_runtime.source.worker_root.parent
        orphaned_lease = ProviderRunMountLease(
            owner_id=orphaned_runtime.source.owner_id,
            provider_kind="aio-local-container",
            sandbox_id="private-owner-run",
            mount_lease_id=uuid.uuid4().hex,
        )
        await orphaned_runtime.persist_mount_acquiring()
        await orphaned_runtime.persist_mount_mounted(orphaned_lease)
        orphaned = Orphaned.from_lease(
            orphaned_lease,
            reason_code="release_readback_unknown",
            last_lifecycle_state="mounted",
        )
        await orphaned_runtime.finalize(orphaned)

        assert orphaned_root.exists()
        metadata = await materializer.inspect_owner(orphaned_runtime.source.owner_id)
        assert metadata.state == "release_pending"
        assert metadata.state_generation == 5
        assert metadata.provider_kind == orphaned_lease.provider_kind
        assert metadata.sandbox_id == orphaned_lease.sandbox_id
        assert metadata.mount_lease_id == orphaned_lease.mount_lease_id
        with pytest.raises(RuntimeError, match="not active"):
            await orphaned_runtime.finalize(orphaned)

        for path in orphaned_root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        orphaned_root.chmod(0o700)
        shutil.rmtree(orphaned_root)

        for path in acquiring_orphan_root.rglob("*"):
            if path.is_dir():
                path.chmod(0o700)
            elif path.is_file():
                path.chmod(0o600)
        acquiring_orphan_root.chmod(0o700)
        shutil.rmtree(acquiring_orphan_root)

    asyncio.run(scenario())
