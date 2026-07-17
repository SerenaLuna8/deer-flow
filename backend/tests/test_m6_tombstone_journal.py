from __future__ import annotations

import asyncio
import base64
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.recovery.journal import (
    TombstoneAuthenticationFailed,
    TombstoneJournal,
    TombstoneJournalUnavailable,
    TombstoneRecord,
    TombstoneSequenceGap,
    TombstoneSequenceRollback,
    load_journal_key,
)

SOURCE_ID = "a" * 64


@pytest.fixture
def journal_key() -> bytes:
    return b"j" * 32


def _record(index: int = 1) -> TombstoneRecord:
    return TombstoneRecord(
        resource_kind="file",
        project_id="11111111-1111-1111-1111-111111111111",
        owner_user_id="22222222-2222-2222-2222-222222222222",
        file_id=f"33333333-3333-3333-3333-{index:012d}",
        project_ids=(),
        idempotency_key=f"purge-{index}",
    )


def test_journal_is_aead_hash_chained_monotonic_and_contains_no_plaintext(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    path = tmp_path / "operator" / "tombstones.jsonl"
    journal = TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID)

    first = journal.append_and_fsync(_record(1), committed_sequence=0)
    second = journal.append_and_fsync(_record(2), committed_sequence=1)

    assert (first.sequence, second.sequence) == (1, 2)
    assert second.previous_digest == first.record_digest
    raw = path.read_text(encoding="utf-8")
    assert "11111111-1111-1111-1111-111111111111" not in raw
    assert "22222222-2222-2222-2222-222222222222" not in raw
    assert "33333333-3333-3333-3333-000000000001" not in raw
    snapshot = journal.snapshot()
    assert snapshot.high_watermark == 2
    assert [entry.record for entry in snapshot.entries] == [_record(1), _record(2)]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_retry_reuses_the_same_sequence_and_conflicting_idempotency_fails_closed(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", journal_key, source_installation_id=SOURCE_ID)
    receipt = journal.append_and_fsync(_record(1), committed_sequence=0)

    assert journal.append_and_fsync(_record(1), committed_sequence=0) == receipt
    conflicting = TombstoneRecord(
        resource_kind="file",
        project_id=_record(1).project_id,
        owner_user_id=_record(1).owner_user_id,
        file_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        project_ids=(),
        idempotency_key=_record(1).idempotency_key,
    )
    with pytest.raises(TombstoneJournalUnavailable):
        journal.append_and_fsync(conflicting, committed_sequence=0)
    assert journal.snapshot().high_watermark == 1


def test_concurrent_allocation_produces_one_contiguous_prefix(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    path = tmp_path / "journal" / "tombstones.jsonl"

    def append(index: int):
        # Each process/thread constructs its own journal handle. The file lock is
        # therefore the authority, not one Python object lock.
        journal = TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID)
        while True:
            committed = journal.snapshot().high_watermark
            try:
                return journal.append_and_fsync(_record(index), committed_sequence=committed)
            except TombstoneJournalUnavailable:
                continue

    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(append, range(1, 17)))

    assert sorted(receipt.sequence for receipt in receipts) == list(range(1, 17))
    snapshot = TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID).snapshot()
    assert [entry.sequence for entry in snapshot.entries] == list(range(1, 17))


def test_wrong_key_tamper_gap_and_high_watermark_rollback_fail_closed(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    path = tmp_path / "journal" / "tombstones.jsonl"
    journal = TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID)
    journal.append_and_fsync(_record(1), committed_sequence=0)
    journal.append_and_fsync(_record(2), committed_sequence=1)

    with pytest.raises(TombstoneAuthenticationFailed):
        TombstoneJournal(path, b"x" * 32, source_installation_id=SOURCE_ID).snapshot()
    with pytest.raises(TombstoneSequenceRollback):
        journal.replay_after(3)

    lines = path.read_text(encoding="utf-8").splitlines()
    second = json.loads(lines[2])
    second["sequence"] = 3
    lines[2] = json.dumps(second, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(TombstoneSequenceGap):
        journal.snapshot()


def test_ciphertext_bit_flip_is_rejected_before_record_release(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    path = tmp_path / "journal" / "tombstones.jsonl"
    TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID).append_and_fsync(_record(), committed_sequence=0)
    lines = path.read_text(encoding="utf-8").splitlines()
    envelope = json.loads(lines[1])
    ciphertext = bytearray(base64.b64decode(envelope["ciphertext"], validate=True))
    ciphertext[0] ^= 1
    envelope["ciphertext"] = base64.b64encode(ciphertext).decode("ascii")
    lines[1] = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(TombstoneAuthenticationFailed):
        TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID).snapshot()


def test_fsync_failure_is_reported_and_never_claimed_as_durable(
    tmp_path: Path,
    journal_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.recovery.journal as journal_module

    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", journal_key, source_installation_id=SOURCE_ID)
    journal.snapshot()  # Create and durably publish the header first.
    real_fsync = journal_module.os.fsync
    calls = 0

    def fail_record_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_record_fsync)
    with pytest.raises(TombstoneJournalUnavailable):
        journal.append_and_fsync(_record(), committed_sequence=0)


def test_entry_nonce_is_random_and_not_reused_after_failed_fsync(
    tmp_path: Path,
    journal_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.recovery.journal as journal_module

    path = tmp_path / "journal" / "tombstones.jsonl"
    journal = TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID)
    journal.snapshot()
    real_fsync = journal_module.os.fsync
    real_urandom = journal_module.os.urandom
    calls = 0
    nonces: list[bytes] = []

    def observed_urandom(size: int) -> bytes:
        value = real_urandom(size)
        if size == 12:
            nonces.append(value)
        return value

    def fail_once(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("disk full")
        real_fsync(fd)

    monkeypatch.setattr(journal_module.os, "fsync", fail_once)
    monkeypatch.setattr(journal_module.os, "urandom", observed_urandom)
    with pytest.raises(TombstoneJournalUnavailable):
        journal.append_and_fsync(_record(), committed_sequence=0)
    receipt = journal.append_and_fsync(_record(), committed_sequence=0)

    envelope = json.loads(path.read_text(encoding="utf-8").splitlines()[1])
    assert len(nonces) == 2
    assert nonces[0] != nonces[1]
    assert base64.b64decode(envelope["nonce"], validate=True) == nonces[1]
    assert base64.b64decode(envelope["nonce"], validate=True) != receipt.sequence.to_bytes(12, "big")


def test_authenticated_header_binds_the_source_installation(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    path = tmp_path / "journal" / "tombstones.jsonl"
    TombstoneJournal(path, journal_key, source_installation_id=SOURCE_ID).snapshot()

    with pytest.raises(TombstoneAuthenticationFailed):
        TombstoneJournal(path, journal_key, source_installation_id="b" * 64).snapshot()


@pytest.mark.anyio
async def test_async_journal_calls_offload_blocking_file_io(
    tmp_path: Path,
    journal_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", journal_key, source_installation_id=SOURCE_ID)
    loop_thread = __import__("threading").get_ident()
    append_thread: int | None = None
    real_append = journal.append_and_fsync

    def observed_append(record: TombstoneRecord, *, committed_sequence: int):
        nonlocal append_thread
        append_thread = __import__("threading").get_ident()
        return real_append(record, committed_sequence=committed_sequence)

    monkeypatch.setattr(journal, "append_and_fsync", observed_append)
    await journal.append(_record(), committed_sequence=0)
    assert append_thread is not None and append_thread != loop_thread


def test_journal_key_is_independent_and_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEER_FLOW_RECOVERY_JOURNAL_KEY", raising=False)
    with pytest.raises(TombstoneJournalUnavailable):
        load_journal_key()


def test_journal_key_rejects_credential_keyring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(b"r" * 32).decode("ascii")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    monkeypatch.setenv("AUTH_JWT_SECRET", "distinct-auth-secret")
    monkeypatch.setenv("DEER_FLOW_RECOVERY_JOURNAL_KEY", encoded)
    monkeypatch.setenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", json.dumps({"credential-v1": encoded}))

    with pytest.raises(TombstoneJournalUnavailable):
        load_journal_key()


def test_journal_key_rejects_persisted_auth_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = base64.b64encode(b"r" * 32).decode("ascii")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".jwt_secret").write_text(encoded, encoding="utf-8")
    monkeypatch.setenv("DEER_FLOW_HOME", str(home))
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    monkeypatch.setenv("DEER_FLOW_RECOVERY_JOURNAL_KEY", encoded)
    monkeypatch.delenv("DEER_FLOW_CREDENTIAL_KEYRING_JSON", raising=False)

    with pytest.raises(TombstoneJournalUnavailable):
        load_journal_key()

    encoded = base64.b64encode(b"r" * 32).decode("ascii")
    monkeypatch.setenv("DEER_FLOW_RECOVERY_JOURNAL_KEY", encoded)
    monkeypatch.setenv("DEER_FLOW_BACKUP_KEY", encoded)
    with pytest.raises(TombstoneJournalUnavailable):
        load_journal_key()


def test_repository_local_or_symlink_journal_is_rejected(
    tmp_path: Path,
    journal_key: bytes,
) -> None:
    repository_path = Path(__file__).parents[2] / ".task17-journal"
    with pytest.raises(TombstoneJournalUnavailable):
        TombstoneJournal(repository_path, journal_key, source_installation_id=SOURCE_ID).snapshot()

    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(TombstoneJournalUnavailable):
        TombstoneJournal(linked_parent / "journal", journal_key, source_installation_id=SOURCE_ID).snapshot()


def test_recovery_public_facade_exposes_task17_operator_contracts() -> None:
    import app.recovery as recovery

    assert recovery.TombstoneJournal is TombstoneJournal
    assert recovery.RetentionPurger.__name__ == "RetentionPurger"
    assert recovery.Restorer.__name__ == "Restorer"


@pytest.mark.anyio
async def test_cancelled_append_never_returns_a_false_success(
    tmp_path: Path,
    journal_key: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = TombstoneJournal(tmp_path / "journal" / "tombstones.jsonl", journal_key, source_installation_id=SOURCE_ID)
    entered = asyncio.Event()
    release = __import__("threading").Event()
    real_append = journal.append_and_fsync

    def delayed(record: TombstoneRecord, *, committed_sequence: int):
        asyncio.run_coroutine_threadsafe(_set_event(entered), loop).result()
        release.wait(timeout=5)
        return real_append(record, committed_sequence=committed_sequence)

    async def _set_event(event: asyncio.Event) -> None:
        event.set()

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(journal, "append_and_fsync", delayed)
    task = asyncio.create_task(journal.append(_record(), committed_sequence=0))
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
