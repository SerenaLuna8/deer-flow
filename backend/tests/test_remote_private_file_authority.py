from __future__ import annotations

import base64
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from deerflow.community.remote_file_authority import (
    PRIVATE_GUEST_REQUEST_ENV,
    PRIVATE_GUEST_SCRIPT,
    PRIVATE_ROOT_BOOTSTRAP_SCRIPT,
    RemotePrivateFileAuthority,
)

_INIT_ACTION = "init_private_roots"
_INIT_REQUEST = {
    "version": 1,
    "action": _INIT_ACTION,
    "root": "/mnt/user-data",
    "path": "/mnt/user-data",
    "display_path": "/mnt/user-data",
}
_MOUNT_ROOT_MARKER = 'PRIVATE_MOUNT_ROOT = "/mnt"'
_RUNTIME_USER_MARKER = 'PRIVATE_RUNTIME_USER = "gem"'
_RUNTIME_EUID_MARKER = "PRIVATE_RUNTIME_EUID = os.geteuid()"
_RUNTIME_EGID_MARKER = "PRIVATE_RUNTIME_EGID = os.getegid()"


def _run_guest_init(
    mount_root: Path,
    *,
    request: dict[str, object] | None = None,
    runtime_euid: int | None = None,
    runtime_egid: int | None = None,
) -> dict[str, object]:
    assert _MOUNT_ROOT_MARKER in PRIVATE_GUEST_SCRIPT
    script = PRIVATE_GUEST_SCRIPT.replace(
        _MOUNT_ROOT_MARKER,
        f"PRIVATE_MOUNT_ROOT = {str(mount_root)!r}",
        1,
    )
    if runtime_euid is not None:
        assert _RUNTIME_EUID_MARKER in script
        script = script.replace(
            _RUNTIME_EUID_MARKER,
            f"PRIVATE_RUNTIME_EUID = {runtime_euid}",
            1,
        )
    if runtime_egid is not None:
        assert _RUNTIME_EGID_MARKER in script
        script = script.replace(
            _RUNTIME_EGID_MARKER,
            f"PRIVATE_RUNTIME_EGID = {runtime_egid}",
            1,
        )
    encoded = base64.b64encode(
        json.dumps(request or _INIT_REQUEST, separators=(",", ":")).encode(),
    ).decode()
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, PRIVATE_GUEST_REQUEST_ENV: encoded},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    response = json.loads(result.stdout)
    assert isinstance(response, dict)
    return response


def _run_root_bootstrap(mount_root: Path) -> subprocess.CompletedProcess[str]:
    assert _MOUNT_ROOT_MARKER in PRIVATE_ROOT_BOOTSTRAP_SCRIPT
    assert _RUNTIME_USER_MARKER in PRIVATE_ROOT_BOOTSTRAP_SCRIPT
    script = PRIVATE_ROOT_BOOTSTRAP_SCRIPT.replace(
        _MOUNT_ROOT_MARKER,
        f"PRIVATE_MOUNT_ROOT = {str(mount_root)!r}",
        1,
    ).replace(
        _RUNTIME_USER_MARKER,
        f"PRIVATE_RUNTIME_USER = {pwd.getpwuid(os.getuid()).pw_name!r}",
        1,
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_private_root_bootstrap_creates_fixed_owned_roots(
    tmp_path: Path,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)

    result = _run_root_bootstrap(mount_root)
    repeated = _run_root_bootstrap(mount_root)

    assert result.returncode == 0, result.stderr
    assert repeated.returncode == 0, repeated.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert repeated.stdout == ""
    assert repeated.stderr == ""
    for relative in (
        "user-data",
        "user-data/workspace",
        "user-data/uploads",
        "user-data/outputs",
        "acp-workspace",
    ):
        path = mount_root / relative
        metadata = path.stat()
        assert path.is_dir()
        assert not path.is_symlink()
        assert metadata.st_uid == os.getuid()
        assert metadata.st_gid == os.getgid()
        assert stat.S_IMODE(metadata.st_mode) == 0o700


def test_private_root_bootstrap_rejects_existing_symlink(
    tmp_path: Path,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    (mount_root / "user-data").symlink_to(outside, target_is_directory=True)

    result = _run_root_bootstrap(mount_root)

    assert result.returncode != 0
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "preloaded_relative",
    [
        "user-data/workspace/preloaded.txt",
        "acp-workspace/preloaded.txt",
    ],
)
def test_private_root_bootstrap_rejects_preloaded_image_bytes(
    tmp_path: Path,
    preloaded_relative: str,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    preloaded = mount_root / preloaded_relative
    preloaded.parent.mkdir(parents=True, mode=0o700)
    preloaded.write_text("image bytes", encoding="utf-8")

    result = _run_root_bootstrap(mount_root)

    assert result.returncode != 0
    assert preloaded.read_text(encoding="utf-8") == "image bytes"


def test_remote_authority_uses_exact_fixed_request_to_initialize_private_roots() -> None:
    requests: list[dict[str, object]] = []
    authority = RemotePrivateFileAuthority(
        execute=lambda request: requests.append(request) or {"ok": True, "data": {}},
        resolve_path=lambda path: path,
    )

    authority.initialize_private_roots()

    assert requests == [_INIT_REQUEST]


def test_remote_authority_passes_exact_scan_exclusions_to_guest() -> None:
    requests: list[dict[str, object]] = []
    authority = RemotePrivateFileAuthority(
        execute=lambda request: requests.append(request) or {"ok": True, "data": {"entries": []}},
        resolve_path=lambda path: path,
    )

    assert (
        tuple(
            authority.list_secure_files(
                "/mnt/user-data/workspace",
                max_entries=10,
                excluded_root_names=(".venv",),
            )
        )
        == ()
    )
    assert requests == [
        {
            "version": 1,
            "action": "scan",
            "root": "/mnt/user-data",
            "path": "/mnt/user-data/workspace",
            "display_path": "/mnt/user-data/workspace",
            "max_entries": 10,
            "excluded_root_names": [".venv"],
        }
    ]


def test_private_guest_prunes_excluded_workspace_runtime_tree_before_entry_limit(
    tmp_path: Path,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr
    workspace = mount_root / "user-data" / "workspace"
    runtime_bin = workspace / ".venv" / "bin"
    runtime_bin.mkdir(parents=True)
    (runtime_bin / "python3").symlink_to("/usr/bin/python3")
    (workspace / "result.txt").write_text("kept", encoding="utf-8")

    response = _run_guest_init(
        mount_root,
        request={
            "version": 1,
            "action": "scan",
            "root": str(workspace),
            "path": str(workspace),
            "display_path": "/mnt/user-data/workspace",
            "max_entries": 1,
            "excluded_root_names": [".venv"],
        },
    )

    assert response == {
        "ok": True,
        "data": {
            "entries": [
                {
                    "path": "/mnt/user-data/workspace/result.txt",
                    "size": 4,
                    "file_type": "regular",
                }
            ]
        },
    }


def test_private_guest_verifies_all_fixed_roots_idempotently(
    tmp_path: Path,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr

    assert _run_guest_init(mount_root) == {
        "ok": True,
        "data": {"initialized": True},
    }
    assert _run_guest_init(mount_root) == {
        "ok": True,
        "data": {"initialized": True},
    }

    for relative in (
        "user-data",
        "user-data/workspace",
        "user-data/uploads",
        "user-data/outputs",
        "acp-workspace",
    ):
        path = mount_root / relative
        assert path.is_dir()
        assert not path.is_symlink()
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


def test_private_guest_refuses_root_execution(
    tmp_path: Path,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr

    response = _run_guest_init(mount_root, runtime_euid=0)

    assert response["ok"] is False
    assert response["error"] == "permission"


@pytest.mark.parametrize(
    ("runtime_euid", "runtime_egid"),
    [
        (os.geteuid() + 1, os.getegid()),
        (os.geteuid(), os.getegid() + 1),
    ],
)
def test_private_guest_rejects_root_owner_mismatch(
    tmp_path: Path,
    runtime_euid: int,
    runtime_egid: int,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr

    response = _run_guest_init(
        mount_root,
        runtime_euid=runtime_euid,
        runtime_egid=runtime_egid,
    )

    assert response["ok"] is False
    assert response["error"] == "unsafe"


@pytest.mark.parametrize(
    "relative",
    [
        "user-data",
        "user-data/workspace",
        "user-data/uploads",
        "user-data/outputs",
        "acp-workspace",
    ],
)
def test_private_guest_rejects_and_does_not_repair_root_mode(
    tmp_path: Path,
    relative: str,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr
    private_root = mount_root / relative
    private_root.chmod(0o750)

    response = _run_guest_init(mount_root)

    assert response["ok"] is False
    assert response["error"] == "unsafe"
    assert stat.S_IMODE(private_root.stat().st_mode) == 0o750


@pytest.mark.parametrize(
    "preloaded_relative",
    [
        "user-data/uploads/preloaded.txt",
        "acp-workspace/preloaded.txt",
    ],
)
def test_private_guest_rejects_preloaded_root_bytes(
    tmp_path: Path,
    preloaded_relative: str,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr
    preloaded = mount_root / preloaded_relative
    preloaded.write_text("image bytes", encoding="utf-8")

    response = _run_guest_init(mount_root)

    assert response["ok"] is False
    assert response["error"] == "unsafe"


@pytest.mark.parametrize(
    "unsafe_kind",
    ["symlink", "regular"],
)
def test_private_guest_rejects_unsafe_existing_private_root(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    unsafe = mount_root / "user-data"
    if unsafe_kind == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        unsafe.symlink_to(target, target_is_directory=True)
    else:
        unsafe.write_text("not a directory", encoding="utf-8")

    response = _run_guest_init(mount_root)

    assert response["ok"] is False
    assert response["error"] == "unsafe"
    assert not (mount_root / "user-data" / "workspace").exists()


@pytest.mark.parametrize(
    ("relative", "unsafe_kind"),
    [
        ("user-data/workspace", "symlink"),
        ("user-data/uploads", "regular"),
        ("user-data/outputs", "symlink"),
        ("acp-workspace", "regular"),
    ],
)
def test_private_guest_rejects_unsafe_existing_child_root(
    tmp_path: Path,
    relative: str,
    unsafe_kind: str,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    bootstrap = _run_root_bootstrap(mount_root)
    assert bootstrap.returncode == 0, bootstrap.stderr
    unsafe = mount_root / relative
    unsafe.rmdir()
    if unsafe_kind == "symlink":
        outside = tmp_path / f"outside-{unsafe.name}"
        outside.mkdir()
        unsafe.symlink_to(outside, target_is_directory=True)
    else:
        unsafe.write_text("not a directory", encoding="utf-8")

    response = _run_guest_init(mount_root)

    assert response["ok"] is False
    assert response["error"] == "unsafe"


def test_private_guest_rejects_tampered_initialization_request(
    tmp_path: Path,
) -> None:
    mount_root = tmp_path / "mnt"
    mount_root.mkdir(mode=0o700)
    request = {**_INIT_REQUEST, "path": "/tmp"}

    response = _run_guest_init(mount_root, request=request)

    assert response["ok"] is False
    assert response["error"] == "protocol"
    assert list(mount_root.iterdir()) == []
