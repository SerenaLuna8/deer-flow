from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest

from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkUnavailable
from app.private_work.run_skill_tree_materializer import (
    MaterializationAttemptIdentity,
    RunSkillTreeMaterializer,
    RuntimeOwnedMaterializedRunSkillTree,
)
from app.private_work.sandbox_files import (
    PrivateFileRunScope,
    PrivateRunFileAuthority,
)
from app.projects.capabilities import capabilities_for
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from deerflow.config.worker_config import WorkerConfig
from deerflow.file_authority import AuthorityManifest
from deerflow.sandbox.sandbox_provider import (
    NotAcquired,
    Orphaned,
    ProviderMountAbsentProof,
    ProviderRunMountLease,
    Released,
    RunReadonlyMountSource,
)


class _Sandbox:
    def list_secure_files(self, *_args, **_kwargs):
        return iter(())


class _Projection:
    def __init__(self, events: list[str], run_id: str) -> None:
        self._events = events
        self._manifest = AuthorityManifest(entries=(), run_id=run_id)

    async def restore(self, _scope, _sandbox) -> AuthorityManifest:
        self._events.append("projection")
        return self._manifest


class _Boundary:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def before_sandbox_restore(self) -> None:
        self._events.append("authority")

    async def before_run_readonly_mount_acquire(self, tree) -> None:
        assert type(tree) is RuntimeOwnedMaterializedRunSkillTree
        self._events.append("transaction-a")

    async def after_run_readonly_mount_acquire(self, tree, lease) -> None:
        assert type(tree) is RuntimeOwnedMaterializedRunSkillTree
        assert type(lease) is ProviderRunMountLease
        self._events.append("transaction-b")


class _Owner:
    def __init__(self) -> None:
        self.tree: RuntimeOwnedMaterializedRunSkillTree | None = None

    def adopt_materialized_skill_tree(
        self,
        tree: RuntimeOwnedMaterializedRunSkillTree,
    ) -> None:
        self.tree = tree


class _TransactionAFailsAfterPersist:
    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    async def before_run_readonly_mount_acquire(self, tree) -> None:
        await tree.persist_mount_acquiring()
        raise self._failure

    async def after_run_readonly_mount_acquire(self, _tree, _lease) -> None:
        raise AssertionError("provider acquisition must not start")


class _TransactionACancelsAfterPersist:
    async def before_run_readonly_mount_acquire(self, tree) -> None:
        await tree.persist_mount_acquiring()
        current = asyncio.current_task()
        assert current is not None
        current.cancel()
        await asyncio.sleep(0)

    async def after_run_readonly_mount_acquire(self, _tree, _lease) -> None:
        raise AssertionError("provider acquisition must not start")


class _PersistingBoundary:
    async def before_run_readonly_mount_acquire(self, tree) -> None:
        await tree.persist_mount_acquiring()

    async def after_run_readonly_mount_acquire(self, _tree, _lease) -> None:
        raise AssertionError("failed provider acquisition has no transaction B")


class _PersistingSuccessfulBoundary(_Boundary):
    async def before_run_readonly_mount_acquire(self, tree) -> None:
        await tree.persist_mount_acquiring()
        await super().before_run_readonly_mount_acquire(tree)

    async def after_run_readonly_mount_acquire(self, tree, lease) -> None:
        await tree.persist_mount_mounted(lease)
        await super().after_run_readonly_mount_acquire(tree, lease)


class _AcquireFailsProvider:
    def __init__(self) -> None:
        self.acquire_calls = 0

    async def prepare_run_readonly_mount_async(self, *_args, **_kwargs):
        self.acquire_calls += 1
        raise RuntimeError("provider acquisition failed after invocation")


class _Provider:
    def __init__(
        self,
        events: list[str],
        lease: ProviderRunMountLease,
        release_outcome: Released | Orphaned,
    ) -> None:
        self._events = events
        self._lease = lease
        self._release_outcome = release_outcome
        self._sandbox = _Sandbox()
        self.release_calls = 0

    async def prepare_run_readonly_mount_async(
        self,
        _thread_id: str,
        *,
        scope,
        run_id: str,
        source: RunReadonlyMountSource,
    ) -> ProviderRunMountLease:
        del scope, run_id
        assert source.owner_id == self._lease.owner_id
        self._events.append("provider-acquire")
        return self._lease

    async def readback_run_readonly_mount_async(
        self,
        lease: ProviderRunMountLease,
    ) -> ProviderRunMountLease:
        assert lease == self._lease
        self._events.append("provider-readback")
        return lease

    async def release_run_readonly_mount_async(
        self,
        lease: ProviderRunMountLease,
    ) -> Released | Orphaned:
        assert lease == self._lease
        self.release_calls += 1
        return self._release_outcome

    def get(self, sandbox_id: str):
        assert sandbox_id == self._lease.sandbox_id
        return self._sandbox


class _SequencedReleaseProvider(_Provider):
    def __init__(
        self,
        events: list[str],
        lease: ProviderRunMountLease,
        release_outcomes: tuple[Released | Orphaned, ...],
    ) -> None:
        super().__init__(events, lease, release_outcomes[-1])
        self._release_outcomes = release_outcomes

    async def release_run_readonly_mount_async(
        self,
        lease: ProviderRunMountLease,
    ) -> Released | Orphaned:
        assert lease == self._lease
        outcome = self._release_outcomes[self.release_calls]
        self.release_calls += 1
        return outcome


def _context() -> PrivateWorkContext:
    return PrivateWorkContext.from_project(
        ProjectContext(
            user_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            membership_id=uuid.uuid4(),
            role=ProjectRole.ADMIN,
            capabilities=capabilities_for(ProjectRole.ADMIN),
            membership_version=1,
            request_id="private-run-file-authority-mount",
        )
    )


def _tree(tmp_path: Path) -> RuntimeOwnedMaterializedRunSkillTree:
    owner_id = uuid.uuid4()
    owner_root = tmp_path / owner_id.hex
    worker_root = owner_root / "tree"
    worker_root.mkdir(parents=True)
    return RuntimeOwnedMaterializedRunSkillTree(
        owner_root=owner_root,
        source=RunReadonlyMountSource(
            owner_id=owner_id,
            worker_root=worker_root,
        ),
        manifests=(),
        skills=(),
    )


async def _real_tree(
    tmp_path: Path,
) -> tuple[RunSkillTreeMaterializer, RuntimeOwnedMaterializedRunSkillTree]:
    materializer = RunSkillTreeMaterializer(
        materialization_root=tmp_path / "materializations",
        worker_config=WorkerConfig(),
    )
    builder = await materializer.begin_attempt(
        MaterializationAttemptIdentity(
            job_id=uuid.uuid4(),
            attempt_id=uuid.uuid4(),
            worker_id=uuid.uuid4(),
        )
    )
    await builder.write_file(
        "custom/skill/SKILL.md",
        b"---\nname: exact-skill\n---\n",
    )
    pending = await builder.publish(manifests=(), skills=())
    owner = _Owner()
    tree = pending.transfer_to(owner)
    assert owner.tree is tree
    return materializer, tree


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind",
    ("commit", "cancel"),
)
async def test_transaction_a_ack_failure_after_acquiring_stays_orphaned(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    materializer, tree = await _real_tree(tmp_path)
    owner_root = tree.source.worker_root.parent
    context = _context()
    boundary = (
        _TransactionAFailsAfterPersist(
            RuntimeError("commit acknowledgement unavailable"),
        )
        if failure_kind == "commit"
        else _TransactionACancelsAfterPersist()
    )
    authority = PrivateRunFileAuthority(
        PrivateFileRunScope(
            context,
            thread_id="thread-1",
            run_id="run-1",
            authorization_boundary=boundary,
        ),
        object(),
        object(),
        run_skill_tree=tree,
        skill_container_path="/mnt/skills",
        provider=object(),  # type: ignore[arg-type]
    )

    if failure_kind == "commit":
        with pytest.raises(RuntimeError, match="commit acknowledgement unavailable"):
            await authority.restore()
    else:
        restore = asyncio.create_task(authority.restore())
        with pytest.raises(asyncio.CancelledError):
            await restore

    acquiring = await materializer.inspect_owner(tree.source.owner_id)
    assert acquiring.state == "acquiring"
    assert acquiring.state_generation == 3
    outcome = await authority.release()
    assert type(outcome) is Orphaned
    assert outcome.last_lifecycle_state == "acquiring"
    await tree.finalize(outcome)
    assert owner_root.exists()
    handed_off = await materializer.inspect_owner(tree.source.owner_id)
    assert handed_off.state == "release_pending"


@pytest.mark.asyncio
async def test_provider_invocation_failure_cannot_use_pre_provider_rollback(
    tmp_path: Path,
) -> None:
    materializer, tree = await _real_tree(tmp_path)
    owner_root = tree.source.worker_root.parent
    provider = _AcquireFailsProvider()
    authority = PrivateRunFileAuthority(
        PrivateFileRunScope(
            _context(),
            thread_id="thread-1",
            run_id="run-1",
            authorization_boundary=_PersistingBoundary(),
        ),
        object(),
        object(),
        run_skill_tree=tree,
        skill_container_path="/mnt/skills",
        provider=provider,  # type: ignore[arg-type]
    )

    with pytest.raises(PrivateWorkUnavailable):
        await authority.restore()

    assert provider.acquire_calls == 1
    assert (await materializer.inspect_owner(tree.source.owner_id)).state == "acquiring"
    outcome = await authority.release()
    assert type(outcome) is Orphaned
    assert outcome.last_lifecycle_state == "acquiring"
    await tree.finalize(outcome)
    assert owner_root.exists()
    assert (await materializer.inspect_owner(tree.source.owner_id)).state == "release_pending"


def _authority(
    tmp_path: Path,
    *,
    release_kind: str,
) -> tuple[
    PrivateRunFileAuthority,
    RuntimeOwnedMaterializedRunSkillTree,
    _Provider,
    list[str],
]:
    events: list[str] = []
    tree = _tree(tmp_path)
    lease = ProviderRunMountLease(
        owner_id=tree.source.owner_id,
        provider_kind="test",
        sandbox_id="sandbox-1",
        mount_lease_id="mount-lease-1",
    )
    release_outcome = (
        Released(proof=ProviderMountAbsentProof.from_lease(lease))
        if release_kind == "released"
        else Orphaned.from_lease(
            lease,
            reason_code="release_readback_unknown",
            last_lifecycle_state="release_pending",
        )
    )
    provider = _Provider(events, lease, release_outcome)
    context = _context()
    run_id = "run-1"
    authority = PrivateRunFileAuthority(
        PrivateFileRunScope(
            context,
            thread_id="thread-1",
            run_id=run_id,
            authorization_boundary=_Boundary(events),
        ),
        _Projection(events, run_id),
        object(),
        run_skill_tree=tree,
        skill_container_path="/mnt/skills",
        provider=provider,  # type: ignore[arg-type]
    )
    return authority, tree, provider, events


@pytest.mark.asyncio
async def test_v4_mount_fences_provider_and_caches_released_proof(
    tmp_path: Path,
) -> None:
    authority, _tree_token, provider, events = _authority(
        tmp_path,
        release_kind="released",
    )

    await authority.restore()

    assert events == [
        "transaction-a",
        "provider-acquire",
        "provider-readback",
        "transaction-b",
        "projection",
    ]
    assert authority.authorizes_run_read_only_mount_path(
        run_id="run-1",
        path="/mnt/skills/custom/ppt-master/SKILL.md",
    )
    first = await authority.release()
    second = await authority.release()
    assert type(first) is Released
    assert second is first
    assert provider.release_calls == 1


@pytest.mark.asyncio
async def test_v4_release_before_provider_acquire_is_typed_not_acquired(
    tmp_path: Path,
) -> None:
    authority, tree, provider, _events = _authority(
        tmp_path,
        release_kind="released",
    )

    first = await authority.release()
    second = await authority.release()

    assert type(first) is NotAcquired
    assert first.matches_source(tree.source)
    assert second is first
    assert provider.release_calls == 0


@pytest.mark.asyncio
async def test_v4_orphaned_release_preserves_durable_mounted_state(
    tmp_path: Path,
) -> None:
    authority, _tree_token, provider, _events = _authority(
        tmp_path,
        release_kind="orphaned",
    )
    await authority.restore()

    first = await authority.release()
    second = await authority.release()

    assert type(first) is Orphaned
    assert first.last_lifecycle_state == "mounted"
    assert type(second) is Orphaned
    assert second.last_lifecycle_state == "mounted"
    assert provider.release_calls == 2


@pytest.mark.asyncio
async def test_v4_repeated_release_only_upgrades_orphaned_to_released(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    _materializer, tree = await _real_tree(tmp_path)
    lease = ProviderRunMountLease(
        owner_id=tree.source.owner_id,
        provider_kind="test",
        sandbox_id="sandbox-1",
        mount_lease_id="mount-lease-1",
    )
    first_orphaned = Orphaned.from_lease(
        lease,
        reason_code="first_readback_unknown",
        last_lifecycle_state="mounted",
    )
    later_orphaned = Orphaned.from_lease(
        lease,
        reason_code="later_readback_unknown",
        last_lifecycle_state="mounted",
    )
    released = Released(proof=ProviderMountAbsentProof.from_lease(lease))
    provider = _SequencedReleaseProvider(
        events,
        lease,
        (first_orphaned, later_orphaned, released),
    )
    authority = PrivateRunFileAuthority(
        PrivateFileRunScope(
            _context(),
            thread_id="thread-1",
            run_id="run-1",
            authorization_boundary=_PersistingSuccessfulBoundary(events),
        ),
        _Projection(events, "run-1"),
        object(),
        run_skill_tree=tree,
        skill_container_path="/mnt/skills",
        provider=provider,  # type: ignore[arg-type]
    )
    await authority.restore()

    first = await authority.release()
    second = await authority.release()
    third = await authority.release()
    fourth = await authority.release()

    assert first == first_orphaned
    assert second is first
    assert third is released
    assert fourth is third
    assert provider.release_calls == 3
