"""Inner probe executed by two distinct Compose Worker containers for P-03."""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path

from deerflow.config.paths import get_paths
from deerflow.private_scope import PrivateResourceScope
from deerflow.sandbox.sandbox_provider import (
    ProviderRunMountLease,
    ProviderRunMountOwnerAbsentProof,
    RunReadonlyMountSource,
    get_sandbox_provider,
    run_readonly_mount_manifest_text,
)

_LEASE_FILE = "p03-lease.json"


def _owner_id() -> uuid.UUID:
    raw = os.environ.get("ACTWEAVE_P03_OWNER_ID", "")
    try:
        owner_id = uuid.UUID(hex=raw)
    except (AttributeError, ValueError):
        raise RuntimeError("P-03 owner identity is unavailable") from None
    if owner_id.hex != raw:
        raise RuntimeError("P-03 owner identity is unavailable")
    return owner_id


def _owner_root(owner_id: uuid.UUID) -> Path:
    return get_paths().run_skill_materialization_root() / owner_id.hex


def _prepare_source(owner_id: uuid.UUID) -> RunReadonlyMountSource:
    owner_root = _owner_root(owner_id)
    tree = owner_root / "tree"
    skill = tree / "custom" / "p03-probe" / "SKILL.md"
    skill.parent.mkdir(mode=0o700, parents=True)
    skill.write_text(
        "---\nname: p03-probe\ndescription: P-03 mount probe.\n---\n",
        encoding="utf-8",
    )
    (tree / ".actweave-run-mount.json").write_text(
        run_readonly_mount_manifest_text(owner_id),
        encoding="utf-8",
    )
    for path in tree.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    tree.chmod(0o555)
    owner_root.chmod(0o700)
    return RunReadonlyMountSource(owner_id=owner_id, worker_root=tree)


def _write_lease(lease: ProviderRunMountLease) -> None:
    path = _owner_root(lease.owner_id) / _LEASE_FILE
    path.write_text(
        json.dumps(
            {
                "mount_lease_id": lease.mount_lease_id,
                "provider_kind": lease.provider_kind,
                "sandbox_id": lease.sandbox_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _read_lease(owner_id: uuid.UUID) -> ProviderRunMountLease | None:
    path = _owner_root(owner_id) / _LEASE_FILE
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "mount_lease_id",
        "provider_kind",
        "sandbox_id",
    }:
        raise RuntimeError("P-03 persisted lease is invalid")
    return ProviderRunMountLease(
        owner_id=owner_id,
        provider_kind=payload["provider_kind"],
        sandbox_id=payload["sandbox_id"],
        mount_lease_id=payload["mount_lease_id"],
    )


def _remove_owner_root(owner_id: uuid.UUID) -> None:
    owner_root = _owner_root(owner_id)
    if not owner_root.exists():
        return
    for path in owner_root.rglob("*"):
        try:
            path.chmod(0o700 if path.is_dir() else 0o600)
        except FileNotFoundError:
            continue
    owner_root.chmod(0o700)
    shutil.rmtree(owner_root)


def acquire() -> None:
    owner_id = _owner_id()
    provider = get_sandbox_provider()
    source = _prepare_source(owner_id)
    lease = provider.prepare_run_readonly_mount(
        "p03-compose-thread",
        scope=PrivateResourceScope(
            project_id="p03-compose-project",
            owner_user_id="p03-compose-owner",
            membership_version=1,
        ),
        run_id="p03-compose-run",
        source=source,
    )
    if provider.readback_run_readonly_mount(lease) != lease:
        raise RuntimeError("P-03 mount readback is unavailable")
    _write_lease(lease)
    print("P03_ACQUIRED", flush=True)
    os._exit(0)


def reconcile() -> None:
    owner_id = _owner_id()
    provider = get_sandbox_provider()
    proof = provider.ensure_run_readonly_mount_owner_absent(
        owner_id,
        persisted_lease=_read_lease(owner_id),
    )
    if type(proof) is not ProviderRunMountOwnerAbsentProof or not proof.matches_owner(owner_id):
        raise RuntimeError("P-03 owner absence is unavailable")
    _remove_owner_root(owner_id)
    print("P03_RECONCILED", flush=True)


def main() -> None:
    operation = sys.argv[1] if len(sys.argv) == 2 else ""
    if operation == "acquire":
        acquire()
        return
    if operation in {"reconcile", "cleanup"}:
        reconcile()
        return
    raise RuntimeError("P-03 probe operation is invalid")


if __name__ == "__main__":
    main()
