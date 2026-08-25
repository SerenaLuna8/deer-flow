from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from deerflow.community.e2b_sandbox import e2b_sandbox_provider as e2b_provider_module
from deerflow.community.e2b_sandbox.e2b_sandbox_provider import (
    META_KEY_PROJECT,
    META_KEY_PROVIDER,
    META_KEY_RUN,
    META_KEY_THREAD,
    META_KEY_USER,
    META_VAL_PROVIDER,
    PRIVATE_EXECUTION_USER,
    E2BSandboxProvider,
)
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

_META_KEY_MOUNT_OWNER = "deer_flow_run_mount_owner"


@dataclass(slots=True)
class _Execution:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass(slots=True)
class _SandboxInfo:
    sandbox_id: str
    metadata: dict[str, str]


class _Paginator:
    def __init__(self, items: list[_SandboxInfo]) -> None:
        self._items = items
        self._delivered = False

    @property
    def has_next(self) -> bool:
        return not self._delivered

    def next_items(self, **_kwargs: object) -> list[_SandboxInfo]:
        if self._delivered:
            return []
        self._delivered = True
        return list(self._items)


class _E2BControlPlane:
    def __init__(self) -> None:
        self.entries: dict[str, _SandboxInfo] = {}
        self.created: list[_SandboxInfo] = []
        self.file_writes: list[tuple[str, bytes, str | None]] = []
        self.commands: list[tuple[str, str | None]] = []
        self.guest_requests: list[tuple[str | None, dict[str, object]]] = []
        self.list_error = False

    @staticmethod
    def _matches(
        metadata: dict[str, str],
        expected: dict[str, str],
    ) -> bool:
        return all(metadata.get(key) == value for key, value in expected.items())


def _fake_e2b_sdk(control: _E2BControlPlane):
    class FakeFiles:
        def make_dir(self, _path: str, *, user: str | None = None) -> None:
            del user

        def write(
            self,
            path: str,
            content: bytes,
            *,
            user: str | None = None,
        ) -> None:
            if user == PRIVATE_EXECUTION_USER and (path == "/mnt/skills" or path.startswith("/mnt/skills/")):
                raise PermissionError("read-only")
            control.file_writes.append((path, bytes(content), user))

    class FakeCommands:
        def run(
            self,
            command: str,
            *,
            user: str | None = None,
            envs: dict[str, str] | None = None,
            **_kwargs: object,
        ) -> _Execution:
            control.commands.append((command, user))
            encoded = (envs or {}).get(PRIVATE_GUEST_REQUEST_ENV)
            if encoded is not None:
                request = json.loads(base64.b64decode(encoded).decode("utf-8"))
                control.guest_requests.append((user, request))
                if request["action"] == "scan":
                    data: dict[str, object] = {"entries": []}
                elif request["action"] == "probe_run_readonly_mount":
                    data = {
                        "euid": 1000,
                        "owner_id": request["expected_owner_id"],
                        "readable": True,
                        "writable": False,
                    }
                else:
                    raise AssertionError(
                        f"unexpected guest action: {request['action']}",
                    )
                return _Execution(
                    stdout=json.dumps({"ok": True, "data": data}),
                )
            if "NoNewPrivs" in command and "sudo_returncode" in command:
                return _Execution(
                    stdout=json.dumps(
                        {
                            "uid": 1000,
                            "groups": ["deerflow_agent"],
                            "cap_eff": 0,
                            "no_new_privs": 1,
                            "sudo_returncode": None,
                        }
                    ),
                )
            return _Execution()

    class FakeClient:
        def __init__(self, sandbox_id: str) -> None:
            self.sandbox_id = sandbox_id
            self.files = FakeFiles()
            self.commands = FakeCommands()

        def kill(self) -> bool:
            control.entries.pop(self.sandbox_id, None)
            return True

        def close(self) -> None:
            return None

        def set_timeout(self, _seconds: int) -> None:
            return None

    class FakeSandbox:
        @classmethod
        def create(cls, **kwargs: object) -> FakeClient:
            sandbox_id = f"e2b-{len(control.created) + 1}"
            metadata = dict(kwargs["metadata"])
            info = _SandboxInfo(
                sandbox_id=sandbox_id,
                metadata=metadata,
            )
            control.entries[sandbox_id] = info
            control.created.append(info)
            return FakeClient(sandbox_id)

        @classmethod
        def connect(cls, sandbox_id: str, **_kwargs: object) -> FakeClient:
            if sandbox_id not in control.entries:
                raise RuntimeError("sandbox not found")
            return FakeClient(sandbox_id)

        @classmethod
        def list(
            cls,
            query: object | None = None,
            **_kwargs: object,
        ) -> _Paginator:
            if control.list_error:
                raise RuntimeError("E2B control plane unavailable")
            raw_metadata = getattr(query, "metadata", None)
            expected = dict(raw_metadata or {})
            return _Paginator([info for info in control.entries.values() if control._matches(info.metadata, expected)])

    return FakeSandbox


def _scope() -> PrivateResourceScope:
    return PrivateResourceScope(
        project_id="project-e2b",
        owner_user_id="owner-e2b",
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
) -> tuple[E2BSandboxProvider, _E2BControlPlane, Paths]:
    control = _E2BControlPlane()
    fake_sandbox = _fake_e2b_sdk(control)
    paths = Paths(tmp_path / "state")
    config = SimpleNamespace(
        sandbox=SimpleNamespace(
            api_key="test-only-key",
            template="code-interpreter-v1",
            domain=None,
            home_dir=None,
            idle_timeout=300,
            replicas=8,
            mounts=[],
            environment={},
            e2b_p05_v1_verified=False,
        ),
        skills=SimpleNamespace(container_path="/mnt/skills"),
    )
    monkeypatch.setattr(e2b_provider_module, "get_app_config", lambda: config)
    monkeypatch.setattr(
        E2BSandboxProvider,
        "_get_sandbox_cls",
        lambda _self: fake_sandbox,
    )
    monkeypatch.setattr(
        E2BSandboxProvider,
        "_register_signal_handlers",
        lambda _self: None,
    )
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    return E2BSandboxProvider(), control, paths


def test_e2b_typed_run_mount_uploads_with_exact_labels_and_absent_proof(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("70000000-0000-0000-0000-000000000001")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-e2b",
            scope=_scope(),
            run_id="run-e2b",
            source=source,
        )

        assert lease.owner_id == owner_id
        assert lease.provider_kind == "e2b"
        assert control.created[0].metadata == {
            META_KEY_PROVIDER: META_VAL_PROVIDER,
            META_KEY_PROJECT: "project-e2b",
            META_KEY_USER: "owner-e2b",
            META_KEY_THREAD: "thread-e2b",
            META_KEY_RUN: "run-e2b",
            _META_KEY_MOUNT_OWNER: owner_id.hex,
        }
        uploaded_paths = {path for path, _content, user in control.file_writes if user == "root"}
        assert "/mnt/skills/.actweave-run-mount.json" in uploaded_paths
        assert "/mnt/skills/custom/skill-one/SKILL.md" in uploaded_paths
        probe_requests = [(user, request) for user, request in control.guest_requests if request["action"] == "probe_run_readonly_mount"]
        assert probe_requests == [
            (
                PRIVATE_EXECUTION_USER,
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
        assert control.entries == {}
    finally:
        provider.shutdown()
        _restore_source(source)


def test_e2b_reconciles_exact_owner_sandbox_from_persisted_lease(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("70000000-0000-0000-0000-000000000002")
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
        assert result.provider_kind == "e2b"
        assert control.entries == {}
        assert provider.get(lease.sandbox_id) is None
    finally:
        provider.shutdown()
        _restore_source(source)


def test_e2b_release_remains_orphaned_until_exact_absence_readback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("70000000-0000-0000-0000-000000000003")
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
        assert control.entries

        control.list_error = False
        second = provider.release_run_readonly_mount(lease)

        assert type(second) is Released
        assert second.matches_lease(lease)
        assert control.entries == {}
    finally:
        provider.shutdown()
        _restore_source(source)


def test_e2b_owner_reconciliation_is_unknown_when_control_plane_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("70000000-0000-0000-0000-000000000006")
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


def test_e2b_release_clears_tracking_when_sandbox_is_already_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("70000000-0000-0000-0000-000000000005")
    source = _source(paths, owner_id)
    try:
        lease = provider.prepare_run_readonly_mount(
            "thread-absent",
            scope=_scope(),
            run_id="run-absent",
            source=source,
        )
        control.entries.clear()

        outcome = provider.release_run_readonly_mount(lease)

        assert type(outcome) is Released
        assert outcome.matches_lease(lease)
        assert provider.get(lease.sandbox_id) is None
    finally:
        provider.shutdown()
        _restore_source(source)


def test_e2b_rejects_source_outside_trusted_materialization_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, _paths = _provider(monkeypatch, tmp_path)
    owner_id = uuid.UUID("70000000-0000-0000-0000-000000000004")
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
@pytest.mark.p05_e2b
def test_real_e2b_typed_mount_guest_probe_and_exact_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if os.environ.get("ACTWEAVE_REQUIRE_PROVIDER_INTEGRATION") != "1":
        pytest.skip("requires the disposable P-05 E2B provider environment")
    if not os.environ.get("ACT_WEAVE_CONFIG_PATH"):
        pytest.fail("ACT_WEAVE_CONFIG_PATH is required for the P-05 provider probe")

    paths = Paths(tmp_path / "p05-state")
    monkeypatch.setattr("deerflow.config.paths.get_paths", lambda: paths)
    owner_id = uuid.uuid4()
    source = _source(paths, owner_id)
    provider: E2BSandboxProvider | None = None
    lease = None
    try:
        provider = E2BSandboxProvider()
        assert provider.run_readonly_mounts_ready() is True
        lease = provider.prepare_run_readonly_mount(
            f"thread-p05-{uuid.uuid4().hex}",
            scope=PrivateResourceScope(
                project_id=f"project-p05-{uuid.uuid4().hex}",
                owner_user_id=f"owner-p05-{uuid.uuid4().hex}",
                membership_version=1,
            ),
            run_id=f"run-p05-{uuid.uuid4().hex}",
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


def test_e2b_v4_readiness_is_fail_closed_without_real_p05_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    provider, control, _paths = _provider(monkeypatch, tmp_path)
    try:
        assert provider.run_readonly_mounts_ready() is False
        config = e2b_provider_module.get_app_config()
        config.sandbox.e2b_p05_v1_verified = "true"
        assert provider.run_readonly_mounts_ready() is False

        config.sandbox.e2b_p05_v1_verified = True
        assert provider.run_readonly_mounts_ready() is True

        provider._config["api_key"] = None
        assert provider.run_readonly_mounts_ready() is False
        provider._config["api_key"] = "test-only-key"

        control.list_error = True
        assert provider.run_readonly_mounts_ready() is False
    finally:
        provider.shutdown()
