from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from scripts.release_acceptance import recovery_drill as recovery_module
from scripts.release_acceptance.recovery_drill import (
    ArchivePoint,
    ExpectedInventory,
    RecoveryOwnershipError,
    RecoverySwitchDrill,
    RestoreVerification,
    run_restore_phase,
)


@dataclass
class _FakeOperations:
    events: list[str] = field(default_factory=list)
    now_ns: int = 1_000_000_000

    async def inventory(self) -> ExpectedInventory:
        self.events.append("inventory")
        return ExpectedInventory(public_digest="a" * 64, restored_count=4)

    async def archive(self, _expected: ExpectedInventory) -> ArchivePoint:
        self.events.append("archive")
        return ArchivePoint(
            archive_schema_version=7,
            schema_revision="0001_project_saas_baseline",
        )

    async def journal_purge(self) -> int:
        self.events.append("journal_purge")
        return 1

    async def post_backup_row(self) -> None:
        self.events.append("post_backup_row")

    async def source_stop(self) -> None:
        self.events.append("source_stop")

    async def restore(self) -> object:
        self.events.append("restore")
        return object()

    async def restore_probe(
        self,
        _restored: object,
        _expected: ExpectedInventory,
    ) -> RestoreVerification:
        self.events.append("restore_probe")
        return RestoreVerification(
            proof_digest="b" * 64,
            restored_count=4,
            rpo_outcome="archive_point_confirmed",
        )

    async def restore_start(self, _restored: object) -> None:
        self.events.append("restore_start")

    async def browser_probe(self) -> None:
        self.events.append("browser_probe")
        self.now_ns += 25_000_000

    async def restore_stop(self) -> None:
        self.events.append("restore_stop")

    async def source_start(self) -> None:
        self.events.append("source_start")

    async def back_switch_probe(self) -> None:
        self.events.append("back_switch_probe")

    def monotonic_ns(self) -> int:
        return self.now_ns


def test_restore_database_name_matches_owned_database_contract() -> None:
    name = recovery_module._restore_database_name(
        process_id=12345,
        nonce="11111111111141118111111111111111",
    )

    assert name == "deerflow_restore_12345_11111111111141118111111111111111"


@pytest.mark.anyio
async def test_recovery_switch_order_is_archive_purge_stop_restore_switch_back() -> None:
    operations = _FakeOperations()

    summary = await RecoverySwitchDrill(operations).run()

    assert operations.events == [
        "inventory",
        "archive",
        "journal_purge",
        "post_backup_row",
        "source_stop",
        "restore",
        "restore_probe",
        "restore_start",
        "browser_probe",
        "restore_stop",
        "source_start",
        "back_switch_probe",
    ]
    assert summary.archive_schema_version == 7
    assert summary.schema_revision == "0001_project_saas_baseline"
    assert summary.tombstone_count == 1
    assert summary.proof_digest == "b" * 64
    assert summary.rto_ms == 25
    assert summary.rpo_outcome == "archive_point_confirmed"
    assert summary.restored_count == 4


@pytest.mark.anyio
async def test_unverified_restore_target_is_never_registered() -> None:
    events: list[str] = []

    class FakeRestorer:
        async def restore(self):
            events.append("restore")
            return object()

        def owns_verified_target(self, _result: object) -> bool:
            events.append("ownership")
            return False

    async def register(_result: object) -> object:
        events.append("register")
        return object()

    with pytest.raises(RecoveryOwnershipError, match="RESTORE_TARGET_NOT_OWNED"):
        await run_restore_phase(FakeRestorer(), register)

    assert events == ["restore", "ownership"]


@pytest.mark.anyio
async def test_failure_after_source_stop_restores_source_before_propagating() -> None:
    operations = _FakeOperations()

    async def fail_restore() -> object:
        operations.events.append("restore")
        raise RuntimeError("synthetic restore failure")

    operations.restore = fail_restore  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="synthetic restore failure"):
        await RecoverySwitchDrill(operations).run()

    assert operations.events[-1] == "source_start"
    assert "restore_start" not in operations.events


@pytest.mark.anyio
async def test_cancellation_after_restore_start_stops_restore_and_recovers_source() -> None:
    operations = _FakeOperations()

    async def cancel_browser() -> None:
        operations.events.append("browser_probe")
        raise asyncio.CancelledError

    operations.browser_probe = cancel_browser  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await RecoverySwitchDrill(operations).run()

    assert operations.events[-2:] == ["restore_stop", "source_start"]
