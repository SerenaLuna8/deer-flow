from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.config.paths import Paths
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox import (
    NotAcquired,
    Orphaned,
    ProviderMountAbsentProof,
    ProviderRunMountLease,
    Released,
    RunMountAcquireCancelled,
    RunMountReleaseCancelled,
    RunReadonlyMountSource,
    merge_run_mount_release_outcome,
)
from deerflow.sandbox.exceptions import SandboxRuntimeError
from deerflow.sandbox.local.local_sandbox_provider import LocalSandboxProvider

_MOUNT_MANIFEST_NAME = ".actweave-run-mount.json"


def _mount_manifest(owner_id: uuid.UUID) -> str:
    return f'{{"owner_id":"{owner_id}","schema_version":1}}\n'


def _make_readonly_tree(
    paths: Paths,
    owner_id: uuid.UUID,
    *,
    skill_content: bytes = b"---\nname: skill-one\n---\n",
) -> RunReadonlyMountSource:
    owner_root = paths.run_skill_materialization_root() / owner_id.hex
    tree = owner_root / "tree"
    skill_file = tree / "custom" / "skill-one" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_bytes(skill_content)
    manifest = tree / _MOUNT_MANIFEST_NAME
    manifest.write_text(_mount_manifest(owner_id), encoding="utf-8")
    for path in tree.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    tree.chmod(0o555)
    owner_root.chmod(0o700)
    return RunReadonlyMountSource(owner_id=owner_id, worker_root=tree)


def _restore_tree_permissions(source: RunReadonlyMountSource) -> None:
    for path in source.worker_root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    source.worker_root.chmod(0o700)


def test_run_mount_release_outcome_preserves_exact_owner_and_lease() -> None:
    owner_id = uuid.UUID("10000000-0000-0000-0000-000000000001")
    source = RunReadonlyMountSource(
        owner_id=owner_id,
        worker_root=Path("/var/lib/actweave/run-skill-materializations") / owner_id.hex / "tree",
    )
    lease = ProviderRunMountLease(
        owner_id=owner_id,
        provider_kind="local",
        sandbox_id="local-run:owner:thread:run",
        mount_lease_id="20000000000000000000000000000001",
    )
    proof = ProviderMountAbsentProof.from_lease(lease)
    orphaned = Orphaned.from_lease(
        lease,
        reason_code="release_readback_unknown",
        last_lifecycle_state="release_pending",
    )
    released = Released(proof=proof)

    assert lease.matches_source(source)
    assert proof.matches_lease(lease)
    assert released.matches_lease(lease)
    assert merge_run_mount_release_outcome(orphaned, released) is released
    assert merge_run_mount_release_outcome(released, orphaned) is released
    not_acquired = NotAcquired(owner_id=owner_id)
    assert not_acquired.matches_source(source)
    assert (
        merge_run_mount_release_outcome(
            not_acquired,
            NotAcquired(owner_id=owner_id),
        )
        is not_acquired
    )
    with pytest.raises(ValueError, match="not-acquired proof conflicts"):
        merge_run_mount_release_outcome(not_acquired, orphaned)

    wrong_lease = ProviderRunMountLease(
        owner_id=uuid.UUID("10000000-0000-0000-0000-000000000002"),
        provider_kind="local",
        sandbox_id=lease.sandbox_id,
        mount_lease_id=lease.mount_lease_id,
    )
    assert not proof.matches_lease(wrong_lease)
    with pytest.raises(ValueError, match="different owners"):
        merge_run_mount_release_outcome(
            orphaned,
            Released(proof=ProviderMountAbsentProof.from_lease(wrong_lease)),
        )
    different_lease = ProviderRunMountLease(
        owner_id=owner_id,
        provider_kind="local",
        sandbox_id=lease.sandbox_id,
        mount_lease_id="20000000000000000000000000000002",
    )
    with pytest.raises(ValueError, match="different mount lease"):
        merge_run_mount_release_outcome(
            orphaned,
            Released(proof=ProviderMountAbsentProof.from_lease(different_lease)),
        )
    with pytest.raises(FrozenInstanceError):
        lease.sandbox_id = "different"  # type: ignore[misc]


def test_async_prepare_cancellation_returns_cleanup_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = object.__new__(LocalSandboxProvider)
    owner_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
    lease = ProviderRunMountLease(
        owner_id=owner_id,
        provider_kind="local",
        sandbox_id="local-run:owner:thread:run",
        mount_lease_id="20000000000000000000000000000001",
    )
    released = Released(proof=ProviderMountAbsentProof.from_lease(lease))
    source = RunReadonlyMountSource(
        owner_id=owner_id,
        worker_root=Path("/var/lib/actweave/run-skill-materializations") / owner_id.hex / "tree",
    )
    acquire_entered = threading.Event()
    allow_acquire = threading.Event()
    cleanup_calls: list[ProviderRunMountLease] = []

    def prepare(*_args: object, **_kwargs: object) -> ProviderRunMountLease:
        acquire_entered.set()
        assert allow_acquire.wait(timeout=5)
        return lease

    def release(observed: ProviderRunMountLease):
        cleanup_calls.append(observed)
        return released

    monkeypatch.setattr(provider, "prepare_run_readonly_mount", prepare)
    monkeypatch.setattr(provider, "release_run_readonly_mount", release)

    async def scenario() -> None:
        task = asyncio.create_task(
            provider.prepare_run_readonly_mount_async(
                "thread-1",
                scope=PrivateResourceScope(
                    project_id="project-1",
                    owner_user_id="owner-1",
                    membership_version=1,
                ),
                run_id="run-1",
                source=source,
            )
        )
        assert await asyncio.to_thread(acquire_entered.wait, 5)
        task.cancel()
        allow_acquire.set()
        with pytest.raises(RunMountAcquireCancelled) as exc_info:
            await task
        assert exc_info.value.lease is lease
        assert exc_info.value.release_outcome is released

    asyncio.run(scenario())
    assert cleanup_calls == [lease]


def test_async_release_cancellation_returns_completed_release_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = object.__new__(LocalSandboxProvider)
    lease = ProviderRunMountLease(
        owner_id=uuid.UUID("20000000-0000-0000-0000-000000000002"),
        provider_kind="local",
        sandbox_id="local-run:owner:thread:run",
        mount_lease_id="20000000000000000000000000000002",
    )
    released = Released(proof=ProviderMountAbsentProof.from_lease(lease))
    release_entered = threading.Event()
    allow_release = threading.Event()

    def release(observed: ProviderRunMountLease):
        assert observed is lease
        release_entered.set()
        assert allow_release.wait(timeout=5)
        return released

    monkeypatch.setattr(provider, "release_run_readonly_mount", release)

    async def scenario() -> None:
        task = asyncio.create_task(provider.release_run_readonly_mount_async(lease))
        assert await asyncio.to_thread(release_entered.wait, 5)
        task.cancel()
        allow_release.set()
        with pytest.raises(RunMountReleaseCancelled) as exc_info:
            await task
        assert exc_info.value.lease is lease
        assert exc_info.value.release_outcome is released

    asyncio.run(scenario())


@pytest.mark.provider_integration
@pytest.mark.p01_native_local
def test_native_local_mount_readback_is_read_only_and_release_proves_absence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "state")
    owner_id = uuid.UUID("30000000-0000-0000-0000-000000000001")
    source = _make_readonly_tree(paths, owner_id)
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    config = SimpleNamespace(
        skills=SimpleNamespace(container_path="/mnt/skills"),
        sandbox=SimpleNamespace(mounts=(), allow_host_bash=False),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    provider = LocalSandboxProvider()

    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-1",
            scope=scope,
            run_id="run-1",
            source=source,
        )

        assert lease.matches_source(source)
        assert provider.readback_run_readonly_mount(lease) == lease
        sandbox = provider.get(lease.sandbox_id)
        assert sandbox is not None
        assert sandbox.read_file("/mnt/skills/custom/skill-one/SKILL.md") == "---\nname: skill-one\n---\n"
        with pytest.raises(OSError, match="Read-only file system"):
            sandbox.write_file(
                "/mnt/skills/custom/skill-one/SKILL.md",
                "changed",
            )

        released = provider.release_run_readonly_mount(lease)

        assert type(released) is Released
        assert released.matches_lease(lease)
        assert provider.get(lease.sandbox_id) is None
        assert provider.release_run_readonly_mount(lease) is released
    finally:
        _restore_tree_permissions(source)


def test_native_local_mount_reads_only_the_bounded_owner_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "state")
    owner_id = uuid.UUID("30000000-0000-0000-0000-000000000002")
    source = _make_readonly_tree(
        paths,
        owner_id,
        skill_content=b"\xff",
    )
    config = SimpleNamespace(
        skills=SimpleNamespace(container_path="/mnt/skills"),
        sandbox=SimpleNamespace(mounts=(), allow_host_bash=False),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )

    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-1",
            scope=scope,
            run_id="run-manifest",
            source=source,
        )
        assert provider.readback_run_readonly_mount(lease) == lease
        assert type(provider.release_run_readonly_mount(lease)) is Released
    finally:
        _restore_tree_permissions(source)


@pytest.mark.parametrize(
    ("target", "mode", "message"),
    [
        ("owner", 0o755, "Untrusted"),
        ("tree", 0o755, "Untrusted"),
        ("nested", 0o755, "directory mode"),
        ("file", 0o644, "file mode"),
    ],
)
def test_native_local_mount_enforces_materializer_owned_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    target: str,
    mode: int,
    message: str,
) -> None:
    paths = Paths(tmp_path / "state")
    owner_id = uuid.uuid4()
    source = _make_readonly_tree(paths, owner_id)
    targets = {
        "owner": source.worker_root.parent,
        "tree": source.worker_root,
        "nested": source.worker_root / "custom",
        "file": source.worker_root / "custom" / "skill-one" / "SKILL.md",
    }
    targets[target].chmod(mode)
    config = SimpleNamespace(
        skills=SimpleNamespace(container_path="/mnt/skills"),
        sandbox=SimpleNamespace(mounts=(), allow_host_bash=False),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    provider = LocalSandboxProvider()

    try:
        with pytest.raises(SandboxRuntimeError, match=message):
            provider.prepare_run_readonly_mount(
                "thread-1",
                scope=PrivateResourceScope(
                    project_id="project-1",
                    owner_user_id="owner-1",
                    membership_version=1,
                ),
                run_id="run-invalid-mode",
                source=source,
            )
    finally:
        _restore_tree_permissions(source)


def test_native_local_mount_reserves_owner_before_provider_acquire(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "state")
    owner_id = uuid.UUID("30000000-0000-0000-0000-000000000003")
    source = _make_readonly_tree(paths, owner_id)
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )
    config = SimpleNamespace(
        skills=SimpleNamespace(container_path="/mnt/skills"),
        sandbox=SimpleNamespace(mounts=(), allow_host_bash=False),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    provider = LocalSandboxProvider()
    original_acquire = provider.acquire_private
    acquire_entered = threading.Event()
    allow_acquire = threading.Event()
    acquire_calls = 0
    leases: list[ProviderRunMountLease] = []
    errors: list[BaseException] = []

    def acquire_private(*args: object, **kwargs: object):
        nonlocal acquire_calls
        acquire_calls += 1
        if acquire_calls != 1:
            raise AssertionError("duplicate owner reached provider acquisition")
        acquire_entered.set()
        assert allow_acquire.wait(timeout=5)
        return original_acquire(*args, **kwargs)

    monkeypatch.setattr(provider, "acquire_private", acquire_private)

    def prepare() -> None:
        try:
            leases.append(
                provider.prepare_run_readonly_mount(
                    "thread-1",
                    scope=scope,
                    run_id="run-reserved",
                    source=source,
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=prepare)
    worker.start()
    try:
        assert acquire_entered.wait(timeout=5)
        with pytest.raises(SandboxRuntimeError, match="already registered"):
            provider.prepare_run_readonly_mount(
                "thread-2",
                scope=scope,
                run_id="run-duplicate",
                source=source,
            )
    finally:
        allow_acquire.set()
        worker.join(timeout=5)

    try:
        assert not worker.is_alive()
        assert errors == []
        assert len(leases) == 1
        assert acquire_calls == 1
        assert type(provider.release_run_readonly_mount(leases[0])) is Released
    finally:
        _restore_tree_permissions(source)


def test_native_local_mount_rejects_traversal_symlink_and_wrong_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = Paths(tmp_path / "state")
    owner_id = uuid.UUID("40000000-0000-0000-0000-000000000001")
    wrong_owner_id = uuid.UUID("40000000-0000-0000-0000-000000000002")
    trusted_root = paths.base_dir / "run-skill-materializations"
    config = SimpleNamespace(
        skills=SimpleNamespace(container_path="/mnt/skills"),
        sandbox=SimpleNamespace(mounts=(), allow_host_bash=False),
    )
    monkeypatch.setattr("deerflow.config.get_app_config", lambda: config)
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    provider = LocalSandboxProvider()
    scope = PrivateResourceScope(
        project_id="project-1",
        owner_user_id="owner-1",
        membership_version=1,
    )

    with pytest.raises(ValueError, match="Invalid run read-only mount source"):
        RunReadonlyMountSource(
            owner_id=owner_id,
            worker_root=trusted_root / owner_id.hex / "tree" / ".." / "tree",
        )

    real_tree = trusted_root / owner_id.hex / "tree"
    real_skill = real_tree / "custom" / "skill-one" / "SKILL.md"
    real_skill.parent.mkdir(parents=True)
    real_skill.write_text("---\nname: skill-one\n---\n", encoding="utf-8")
    with pytest.raises(SandboxRuntimeError, match="Untrusted"):
        provider.prepare_run_readonly_mount(
            "thread-1",
            scope=scope,
            run_id="run-wrong-owner",
            source=RunReadonlyMountSource(
                owner_id=wrong_owner_id,
                worker_root=real_tree,
            ),
        )

    symlink_owner = uuid.UUID("40000000-0000-0000-0000-000000000003")
    outside_tree = tmp_path / "outside-tree"
    outside_skill = outside_tree / "custom" / "skill-two" / "SKILL.md"
    outside_skill.parent.mkdir(parents=True)
    outside_skill.write_text("---\nname: skill-two\n---\n", encoding="utf-8")
    symlink_tree = trusted_root / symlink_owner.hex / "tree"
    symlink_tree.parent.mkdir(parents=True)
    symlink_tree.symlink_to(outside_tree, target_is_directory=True)
    with pytest.raises(SandboxRuntimeError, match="Untrusted"):
        provider.prepare_run_readonly_mount(
            "thread-1",
            scope=scope,
            run_id="run-symlink",
            source=RunReadonlyMountSource(
                owner_id=symlink_owner,
                worker_root=symlink_tree,
            ),
        )
