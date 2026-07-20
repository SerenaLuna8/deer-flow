from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from scripts.release_acceptance.ownership import (
    DatabaseIdentity,
    HostProcessProbe,
    OwnershipError,
    OwnershipLedger,
)


class FakeProcessProbe:
    def __init__(self) -> None:
        self.identities: dict[int, str | None] = {}
        self.groups: dict[int, int | None] = {}
        self.members: dict[int, tuple[int, ...]] = {}
        self.signals: list[tuple[int, int]] = []
        self.raise_pid: int | None = None

    def start_identity(self, pid: int) -> str | None:
        if pid == self.raise_pid:
            raise RuntimeError("raw process probe failure")
        return self.identities.get(pid)

    def process_group(self, pid: int) -> int | None:
        return self.groups.get(pid)

    def group_members(self, pgid: int) -> tuple[int, ...]:
        return self.members.get(pgid, ())

    def signal_group(self, pgid: int, signal_number: int) -> None:
        self.signals.append((pgid, signal_number))
        self.members[pgid] = ()


class FakeDatabaseProbe:
    def __init__(self) -> None:
        self.identities: dict[str, DatabaseIdentity | None] = {}
        self.dropped = []

    async def identity(self, name: str) -> DatabaseIdentity | None:
        return self.identities.get(name)

    async def drop(self, owned) -> None:
        self.dropped.append(owned)


def _ledger(tmp_path: Path, *, process: FakeProcessProbe | None = None, database: FakeDatabaseProbe | None = None) -> OwnershipLedger:
    return OwnershipLedger(
        repository=tmp_path,
        acceptance_run_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        process_probe=process or FakeProcessProbe(),
        database_probe=database or FakeDatabaseProbe(),
    )


def test_cleanup_refuses_reused_pid(tmp_path: Path) -> None:
    process = FakeProcessProbe()
    process.identities[41001] = "first"
    process.groups[41001] = 41001
    ledger = _ledger(tmp_path, process=process)
    owned = ledger.register_process(pid=41001, pgid=41001, start_identity="first")
    process.identities[41001] = "second"
    result = ledger.stop_process(owned)
    assert result.status == "identity_mismatch"
    assert process.signals == []


def test_cleanup_refuses_changed_process_group(tmp_path: Path) -> None:
    process = FakeProcessProbe()
    process.identities[41002] = "first"
    process.groups[41002] = 99999
    ledger = _ledger(tmp_path, process=process)
    owned = ledger.register_process(pid=41002, pgid=41002, start_identity="first")
    result = ledger.stop_process(owned)
    assert result.status == "identity_mismatch"
    assert process.signals == []


def test_cleanup_stops_owned_group_after_leader_exits(tmp_path: Path) -> None:
    process = FakeProcessProbe()
    process.identities[41003] = None
    process.groups[41003] = None
    process.members[41003] = (41004,)
    ledger = _ledger(tmp_path, process=process)
    owned = ledger.register_process(pid=41003, pgid=41003, start_identity="first")
    result = ledger.stop_process(owned)
    assert result.status == "removed"
    assert process.signals == [(41003, 15)]


def test_host_process_identity_ignores_command_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    outputs = iter(
        (
            "S Mon Jul 21 01:00:00 2026 41001 /usr/bin/make start\n",
            "S Mon Jul 21 01:00:00 2026 41001 /usr/bin/python worker.py\n",
        )
    )

    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=next(outputs))

    monkeypatch.setattr("scripts.release_acceptance.ownership.subprocess.run", fake_run)
    probe = HostProcessProbe()
    assert probe.start_identity(41001) == probe.start_identity(41001)


def test_host_process_identity_treats_zombie_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="Z Mon Jul 21 01:00:00 2026 41001 <defunct>\n",
        )

    monkeypatch.setattr("scripts.release_acceptance.ownership.subprocess.run", fake_run)
    assert HostProcessProbe().start_identity(41001) is None


def test_host_process_group_excludes_zombie_members(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="41001 41001 Z\n41002 41001 S\n41003 99999 S\n",
        )

    monkeypatch.setattr("scripts.release_acceptance.ownership.subprocess.run", fake_run)
    assert HostProcessProbe().group_members(41001) == (41002,)


def test_path_cleanup_refuses_inode_replacement(tmp_path: Path) -> None:
    target = tmp_path / "temporary.txt"
    target.write_text("first", encoding="utf-8")
    ledger = _ledger(tmp_path)
    owned = ledger.register_path(target, disposition="temporary")
    target.unlink()
    target.write_text("second", encoding="utf-8")
    result = ledger.remove_path(owned)
    assert result.status == "identity_mismatch"
    assert target.read_text(encoding="utf-8") == "second"


def test_path_registration_rejects_symlink_to_in_repository_target(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    with pytest.raises(OwnershipError, match="OWNED_PATH_SYMLINK_REJECTED"):
        _ledger(tmp_path).register_path(link, disposition="temporary")
    assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_database_cleanup_refuses_marker_or_owner_mismatch(tmp_path: Path) -> None:
    database = FakeDatabaseProbe()
    ledger = _ledger(tmp_path, database=database)
    owned = ledger.register_database(name="deerflow_test_abc123", owner="deerflow_app", marker_digest="a" * 64)
    database.identities[owned.name] = DatabaseIdentity(owner="other", marker_digest="b" * 64)
    result = await ledger.drop_database(owned)
    assert result.status == "identity_mismatch"
    assert database.dropped == []


def test_database_name_and_retained_path_are_strict(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    with pytest.raises(OwnershipError, match="OWNED_DATABASE_NAME_INVALID"):
        ledger.register_database(name="deerflow", owner="postgres", marker_digest="a" * 64)
    other = tmp_path / "keep"
    other.mkdir()
    with pytest.raises(OwnershipError, match="RETAINED_PATH_NOT_EVIDENCE"):
        ledger.register_path(other, disposition="retained_evidence")


def test_cleanup_port_probe_matches_reusable_server_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.release_acceptance.ownership as ownership_module

    reuse_values: list[int] = []
    monkeypatch.setattr(
        ownership_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=""),
    )

    class FakeSocket:
        def __init__(self, _family: int, _kind: int) -> None:
            pass

        def setsockopt(self, _level: int, _option: int, value: int) -> None:
            reuse_values.append(value)

        def bind(self, _address) -> None:
            return None

        def listen(self, _backlog: int) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(ownership_module.socket, "socket", FakeSocket)
    assert OwnershipLedger._port_is_free(2026) is True
    assert reuse_values == [1, 1]


def test_cleanup_port_probe_requires_reusable_listen(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.release_acceptance.ownership as ownership_module

    monkeypatch.setattr(
        ownership_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, stdout=""),
    )

    class FakeSocket:
        def __init__(self, _family: int, _kind: int) -> None:
            pass

        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return None

        def bind(self, _address) -> None:
            return None

        def listen(self, _backlog: int) -> None:
            raise OSError("synthetic active listener")

        def close(self) -> None:
            return None

    monkeypatch.setattr(ownership_module.socket, "socket", FakeSocket)
    assert OwnershipLedger._port_is_free(2026) is False


def test_cleanup_port_probe_rejects_lsof_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.release_acceptance.ownership as ownership_module

    class FakeSocket:
        def __init__(self, _family: int, _kind: int) -> None:
            pass

        def setsockopt(self, _level: int, _option: int, _value: int) -> None:
            return None

        def bind(self, _address) -> None:
            return None

        def listen(self, _backlog: int) -> None:
            return None

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        ownership_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 0, stdout="41001\n"),
    )
    monkeypatch.setattr(ownership_module.socket, "socket", FakeSocket)
    assert OwnershipLedger._port_is_free(2026) is False


@pytest.mark.asyncio
async def test_cleanup_counts_only_exact_owned_resources(tmp_path: Path) -> None:
    process = FakeProcessProbe()
    database = FakeDatabaseProbe()
    ledger = _ledger(tmp_path, process=process, database=database)
    process.identities[42001] = None
    ledger.register_process(pid=42001, pgid=42001, start_identity="gone")
    path = tmp_path / "temporary.txt"
    path.write_text("temporary", encoding="utf-8")
    ledger.register_path(path, disposition="temporary")
    evidence = tmp_path / ".release-evidence" / str(ledger.acceptance_run_id)
    evidence.mkdir(parents=True)
    ledger.register_path(evidence, disposition="retained_evidence")
    owned_database = ledger.register_database(name="deerflow_restore_abc123", owner="deerflow_app", marker_digest="c" * 64)
    database.identities[owned_database.name] = DatabaseIdentity(owner=owned_database.owner, marker_digest=owned_database.marker_digest)
    ledger.reserve_port(2026)

    summary = await ledger.cleanup()
    assert summary.residual_processes == 0
    assert summary.residual_databases == 0
    assert summary.residual_paths == 0
    assert summary.retained_evidence == 1
    assert not path.exists()
    assert evidence.is_dir()
    assert database.dropped == [owned_database]
    assert os.path.isdir(evidence)


@pytest.mark.asyncio
async def test_cleanup_continues_after_one_resource_probe_fails(tmp_path: Path) -> None:
    process = FakeProcessProbe()
    process.raise_pid = 43001
    ledger = _ledger(tmp_path, process=process)
    ledger.register_process(pid=43001, pgid=43001, start_identity="first")
    path = tmp_path / "remove-me.txt"
    path.write_text("temporary", encoding="utf-8")
    ledger.register_path(path, disposition="temporary")
    summary = await ledger.cleanup()
    assert summary.residual_processes == 1
    assert summary.residual_paths == 0
    assert not path.exists()
