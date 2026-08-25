from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.community.boxlite import provider as boxlite_provider_module
from deerflow.community.boxlite.provider import BoxliteProvider
from deerflow.community.remote_file_authority import PRIVATE_GUEST_REQUEST_ENV
from deerflow.config.paths import Paths
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox import (
    Orphaned,
    ProviderRunMountOwnerAbsentProof,
    ProviderRunMountOwnerUnknown,
    Released,
    RunReadonlyMountSource,
)
from deerflow.sandbox.exceptions import SandboxRuntimeError


@dataclass(slots=True)
class _BoxInfo:
    id: str
    name: str


@dataclass(slots=True)
class _Execution:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


class _BoxliteControlPlane:
    def __init__(self) -> None:
        self.infos: dict[str, _BoxInfo] = {}
        self.created: list[dict[str, object]] = []
        self.guest_requests: list[tuple[str | None, dict[str, object]]] = []
        self.list_error = False

    def remove(self, id_or_name: str) -> None:
        for name, info in tuple(self.infos.items()):
            if id_or_name in {name, info.id}:
                self.infos.pop(name, None)
                return


def _fake_boxlite_sdk(control: _BoxliteControlPlane):
    class FakeSimpleBox:
        def __init__(
            self,
            *,
            name: str,
            volumes: tuple[tuple[str, str, str], ...] = (),
            auto_remove: bool = True,
            **options: object,
        ) -> None:
            self.name = name
            self.id = f"box-{len(control.created) + 1}"
            self.volumes = volumes
            self.auto_remove = auto_remove
            self.options = options

        async def start(self) -> None:
            control.infos[self.name] = _BoxInfo(id=self.id, name=self.name)
            control.created.append(
                {
                    "id": self.id,
                    "name": self.name,
                    "volumes": self.volumes,
                }
            )

        async def exec(
            self,
            _command: str,
            *_args: str,
            env: dict[str, str] | None = None,
            user: str | None = None,
            **_kwargs: object,
        ) -> _Execution:
            encoded = (env or {}).get(PRIVATE_GUEST_REQUEST_ENV)
            if encoded is None:
                return _Execution()
            request = json.loads(base64.b64decode(encoded).decode("utf-8"))
            control.guest_requests.append((user, request))
            action = request["action"]
            if action == "scan":
                data: dict[str, object] = {"entries": []}
            elif action == "probe_run_readonly_mount":
                data = {
                    "euid": 1000,
                    "owner_id": request["expected_owner_id"],
                    "readable": True,
                    "writable": False,
                }
            else:
                raise AssertionError(f"unexpected private guest action: {action}")
            return _Execution(
                stdout=json.dumps({"ok": True, "data": data}),
            )

        async def stop(self) -> None:
            if self.auto_remove:
                control.remove(self.name)

    class FakeSyncBox:
        def __init__(self, info: _BoxInfo) -> None:
            self._info = info

        def stop(self) -> None:
            control.remove(self._info.id)

    class FakeSyncRuntime:
        @classmethod
        def default(cls):
            return cls()

        def start(self):
            return self

        def stop(self) -> None:
            return None

        def list_info(self) -> list[_BoxInfo]:
            if control.list_error:
                raise RuntimeError("BoxLite registry unavailable")
            return list(control.infos.values())

        def get(self, id_or_name: str):
            for info in control.infos.values():
                if id_or_name in {info.id, info.name}:
                    return FakeSyncBox(info)
            return None

        def remove(self, id_or_name: str, force: bool = False) -> None:
            del force
            control.remove(id_or_name)

    return FakeSimpleBox, FakeSyncRuntime


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id="project-boxlite",
        owner_user_id="owner-boxlite",
        membership_version=1,
    )


def _source(paths: Paths, owner_id: uuid.UUID) -> RunReadonlyMountSource:
    owner_root = paths.run_skill_materialization_root() / owner_id.hex
    tree = owner_root / "tree"
    skill = tree / "custom" / "skill-one" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: skill-one\n---\n", encoding="utf-8")
    (tree / ".actweave-run-mount.json").write_text(
        f'{{"owner_id":"{owner_id}","schema_version":1}}\n',
        encoding="utf-8",
    )
    for path in tree.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    tree.chmod(0o555)
    owner_root.chmod(0o700)
    return RunReadonlyMountSource(owner_id=owner_id, worker_root=tree)


def _restore_source(source: RunReadonlyMountSource) -> None:
    for path in source.worker_root.rglob("*"):
        path.chmod(0o700 if path.is_dir() else 0o600)
    source.worker_root.chmod(0o700)


def _provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[BoxliteProvider, _BoxliteControlPlane, Paths]:
    control = _BoxliteControlPlane()
    simple_box, sync_runtime = _fake_boxlite_sdk(control)
    paths = Paths(tmp_path / "state")
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            image="python:3.12-slim",
            replicas=8,
            idle_timeout=0,
            environment={},
            health_check_skip_seconds=0,
            boxlite_p04_v1_verified=False,
        ),
        skills=SimpleNamespace(container_path="/mnt/skills"),
    )
    monkeypatch.setattr(boxlite_provider_module, "get_app_config", lambda: config)
    monkeypatch.setattr(boxlite_provider_module, "_import_simplebox", lambda: simple_box)
    monkeypatch.setattr(
        boxlite_provider_module,
        "_import_sync_boxlite_runtime",
        lambda: sync_runtime,
    )
    monkeypatch.setattr(
        boxlite_provider_module,
        "_boxlite_p04_target_capable",
        lambda: True,
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    return BoxliteProvider(), control, paths


def test_boxlite_typed_run_mount_uses_exact_owner_vm_and_absent_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000001")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-boxlite",
            scope=_scope(),
            run_id="run-boxlite",
            source=source,
        )

        assert lease.owner_id == owner_id
        assert lease.provider_kind == "boxlite"
        assert control.created[0]["volumes"] == ((str(source.worker_root), "/mnt/skills", "ro"),)
        assert control.created[0]["name"] == (f"deer-flow-boxlite-run-{owner_id.hex}-{lease.sandbox_id}")
        probe_requests = [(user, request) for user, request in control.guest_requests if request["action"] == "probe_run_readonly_mount"]
        assert probe_requests == [
            (
                "deerflow_agent",
                {
                    "version": 1,
                    "action": "probe_run_readonly_mount",
                    "root": "/mnt/skills",
                    "path": "/mnt/skills/.actweave-run-mount.json",
                    "display_path": "/mnt/skills/.actweave-run-mount.json",
                    "expected_owner_id": str(owner_id),
                    "expected_manifest": f'{{"owner_id":"{owner_id}","schema_version":1}}\n',
                },
            ),
        ]
        assert provider.readback_run_readonly_mount(lease) == lease

        outcome = provider.release_run_readonly_mount(lease)

        assert type(outcome) is Released
        assert outcome.matches_lease(lease)
        assert provider.release_run_readonly_mount(lease) is outcome
        assert control.infos == {}
    finally:
        provider.shutdown()
        _restore_source(source)


def test_boxlite_reconciles_exact_owner_vm_from_persisted_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000002")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-reconcile",
            scope=_scope(),
            run_id="run-reconcile",
            source=source,
        )

        result = provider.ensure_run_readonly_mount_owner_absent(
            owner_id,
            persisted_lease=lease,
        )

        assert type(result) is ProviderRunMountOwnerAbsentProof
        assert result.matches_owner(owner_id)
        assert result.provider_kind == "boxlite"
        assert control.infos == {}
        assert provider.get(lease.sandbox_id) is None
    finally:
        provider.shutdown()
        _restore_source(source)


def test_boxlite_release_remains_orphaned_until_exact_absence_readback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000003")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-orphan",
            scope=_scope(),
            run_id="run-orphan",
            source=source,
        )
        control.list_error = True

        first = provider.release_run_readonly_mount(lease)

        assert type(first) is Orphaned
        assert first.matches_lease(lease)
        assert control.infos

        control.list_error = False
        second = provider.release_run_readonly_mount(lease)

        assert type(second) is Released
        assert second.matches_lease(lease)
        assert control.infos == {}
    finally:
        provider.shutdown()
        _restore_source(source)


def test_boxlite_owner_reconciliation_is_unknown_when_registry_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000006")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-unknown",
            scope=_scope(),
            run_id="run-unknown",
            source=source,
        )
        control.list_error = True

        result = provider.ensure_run_readonly_mount_owner_absent(
            owner_id,
            persisted_lease=lease,
        )

        assert type(result) is ProviderRunMountOwnerUnknown
        assert result.reason_code == "owner_readback_unknown"
        assert provider.get(lease.sandbox_id) is not None
    finally:
        control.list_error = False
        provider.ensure_run_readonly_mount_owner_absent(
            owner_id,
            persisted_lease=lease,
        )
        provider.shutdown()
        _restore_source(source)


def test_boxlite_release_clears_tracking_when_vm_is_already_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000005")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-absent",
            scope=_scope(),
            run_id="run-absent",
            source=source,
        )
        control.remove(str(control.created[0]["name"]))

        outcome = provider.release_run_readonly_mount(lease)

        assert type(outcome) is Released
        assert outcome.matches_lease(lease)
        assert provider.get(lease.sandbox_id) is None
    finally:
        provider.shutdown()
        _restore_source(source)


def test_boxlite_rejects_source_outside_trusted_materialization_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, _paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("60000000-0000-0000-0000-000000000004")
    source = _source(Paths(tmp_path / "untrusted-state"), owner_id)
    try:
        with pytest.raises(SandboxRuntimeError, match="run read-only mount source"):
            provider.prepare_run_readonly_mount(
                "thread-untrusted",
                scope=_scope(),
                run_id="run-untrusted",
                source=source,
            )
        assert control.created == []
    finally:
        provider.shutdown()
        _restore_source(source)


@pytest.mark.provider_integration
@pytest.mark.p04_boxlite
def test_real_boxlite_typed_mount_guest_probe_and_exact_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.environ.get("ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION") != "1":
        pytest.skip("requires the disposable P-04 BoxLite provider environment")
    if not os.environ.get("ACT_WEAVE_CONFIG_PATH"):
        pytest.fail("ACT_WEAVE_CONFIG_PATH is required for the P-04 provider probe")
    if not boxlite_provider_module._boxlite_p04_target_capable():
        pytest.fail("an accessible Linux /dev/kvm is required for the P-04 provider probe")
    try:
        import boxlite  # noqa: F401
    except ImportError:
        pytest.fail("the BoxLite dependency is required for the P-04 provider probe")

    paths = Paths(tmp_path / "p04-state")
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    owner_id = uuid.uuid4()
    source = _source(paths, owner_id)
    provider: BoxliteProvider | None = None
    lease = None
    try:
        provider = BoxliteProvider()
        assert provider.run_readonly_mounts_ready() is True
        lease = provider.prepare_run_readonly_mount(
            f"thread-p04-{uuid.uuid4().hex}",
            scope=PrivateResourceScope(
                project_id=f"project-p04-{uuid.uuid4().hex}",
                owner_user_id=f"owner-p04-{uuid.uuid4().hex}",
                membership_version=1,
            ),
            run_id=f"run-p04-{uuid.uuid4().hex}",
            source=source,
        )

        assert provider.readback_run_readonly_mount(lease) == lease
        outcome = provider.release_run_readonly_mount(lease)
        assert type(outcome) is Released
        assert outcome.matches_lease(lease)
        reconciliation = provider.ensure_run_readonly_mount_owner_absent(
            owner_id,
            persisted_lease=lease,
        )
        assert type(reconciliation) is ProviderRunMountOwnerAbsentProof
    finally:
        if provider is not None:
            provider.ensure_run_readonly_mount_owner_absent(
                owner_id,
                persisted_lease=lease,
            )
            provider.shutdown()
        _restore_source(source)


def test_boxlite_v4_readiness_is_fail_closed_without_real_p04_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, _control, _paths = _provider(monkeypatch, tmp_path)
    try:
        assert provider.run_readonly_mounts_ready() is False
        config = boxlite_provider_module.get_app_config()
        config.sandbox.boxlite_p04_v1_verified = "true"
        assert provider.run_readonly_mounts_ready() is False

        config.sandbox.boxlite_p04_v1_verified = True
        assert provider.run_readonly_mounts_ready() is True

        provider._p04_registry_ready = False
        assert provider.run_readonly_mounts_ready() is False

        provider._p04_registry_ready = True
        monkeypatch.setattr(
            boxlite_provider_module,
            "_boxlite_p04_target_capable",
            lambda: False,
        )
        assert provider.run_readonly_mounts_ready() is False
    finally:
        provider.shutdown()
