from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import app.recovery.archive as archive_module
import app.recovery.restore as restore_module
import deerflow.persistence.final_schema_contract as schema_contract
from app.recovery.archive import (
    BackupArchiveReader,
    BackupArchiveWriter,
    BackupAuthenticationFailed,
    BackupCommandFailed,
    BackupConfig,
    create_backup,
)
from app.recovery.cleanup import OwnedFile
from app.recovery.journal import TombstoneJournal
from app.recovery.restore import RestoreConfig, Restorer
from app.reliability.owner_refs import AuditHmacKeyring

BACKUP_KEY = b"b" * 32
JOURNAL_KEY = b"j" * 32
SOURCE_ID = "1" * 64
TARGET_URL = "postgresql://operator@localhost/deerflow_restore_1_0123456789abcdef0123456789abcdef"
SOURCE_URL = "postgresql://operator@localhost/deerflow_source"


def _keyring() -> AuditHmacKeyring:
    return AuditHmacKeyring(
        active_key_id="audit-v1",
        _keys={"audit-v1": b"a" * 32},
    )


def test_archive_manifest_is_bound_to_the_canonical_m7_schema(tmp_path: Path) -> None:
    output = tmp_path / "m7.dfba"
    with BackupArchiveWriter.atomic(
        output,
        BACKUP_KEY,
        source_installation_id=SOURCE_ID,
    ) as writer:
        writer.write_chunk(b"PGDMPm7")
        manifest = writer.finalize(
            database_high_watermark=0,
            tombstone_journal_sequence=0,
        )

    assert archive_module.ARCHIVE_SCHEMA_VERSION == 7
    assert manifest.archive_schema_version == 7
    assert manifest.schema_revision == "0001_project_saas_baseline"
    assert manifest.schema_digest == archive_module.M7_CANONICAL_SCHEMA_DIGEST


def test_chunk_aad_binds_archive_schema_version_revision_and_digest() -> None:
    encoded = archive_module._aad(
        "00000000-0000-0000-0000-000000000001",
        archive_schema_version=7,
        schema_revision="0001_project_saas_baseline",
        schema_digest="a" * 64,
        source_installation_id=SOURCE_ID,
        index=3,
    )

    assert json.loads(encoded) == {
        "archive_id": "00000000-0000-0000-0000-000000000001",
        "archive_schema_version": 7,
        "chunk_index": 3,
        "schema_digest": "a" * 64,
        "schema_revision": "0001_project_saas_baseline",
        "source_installation_id": SOURCE_ID,
    }


def test_catalog_digest_normalizes_only_pg_restore_equivalent_casts() -> None:
    source = (("CHECK ((status)::text = ANY (ARRAY['queued'::character varying, 'running'::character varying]::text[]))",),)
    restored = (("CHECK ((status)::text = ANY (ARRAY['queued'::character varying::text, 'running'::character varying::text]))",),)

    assert schema_contract._rows_digest(source) == schema_contract._rows_digest(restored)


@pytest.mark.parametrize(
    "near_miss",
    (
        "CHECK ((status)::text = ANY (ARRAY['queued'::character varying::text, 'dead'::character varying::text]))",
        "CHECK ((status)::text = ANY (ARRAY['queued'::text, 'running'::text]))",
        "CHECK ((status)::text = ALL (ARRAY['queued'::character varying::text, 'running'::character varying::text]))",
        "CHECK ((state)::text = ANY (ARRAY['queued'::character varying::text, 'running'::character varying::text]))",
    ),
    ids=("element", "type", "operator", "predicate"),
)
def test_catalog_digest_rejects_near_miss_array_drift(near_miss: str) -> None:
    source = (("CHECK ((status)::text = ANY (ARRAY['queued'::character varying, 'running'::character varying]::text[]))",),)

    assert schema_contract._rows_digest(source) != schema_contract._rows_digest(((near_miss,),))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("archive_schema_version", 6),
        ("schema_revision", "0015_project_reliability_finalize"),
        ("schema_digest", "a" * 64),
    ),
)
def test_resigned_schema_field_tamper_still_fails_chunk_aad(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    output = tmp_path / field / "m7.dfba"
    output.parent.mkdir()
    with BackupArchiveWriter.atomic(
        output,
        BACKUP_KEY,
        source_installation_id=SOURCE_ID,
    ) as writer:
        writer.write_chunk(b"PGDMPm7-aad")
        writer.finalize(database_high_watermark=0, tombstone_journal_sequence=0)

    manifest_path = output / "manifest.json"
    envelope = json.loads(manifest_path.read_bytes())
    envelope["manifest"][field] = value
    envelope["signature"] = archive_module._manifest_signature(
        BACKUP_KEY,
        envelope["manifest"],
    )
    manifest_path.write_bytes(archive_module._canonical_json(envelope))

    with pytest.raises(BackupAuthenticationFailed):
        list(BackupArchiveReader(BACKUP_KEY).verified_chunks(output))


@pytest.mark.postgres
@pytest.mark.anyio
async def test_backup_rejects_noncanonical_m7_source_before_pg_dump(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE legacy_backup_source (id integer)"))
    finally:
        await engine.dispose()

    async def fake_version() -> str:
        return "pg_dump (PostgreSQL) 16.4"

    spawned = 0

    async def fail_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned += 1
        raise AssertionError("pg_dump must not start for a noncanonical source")

    monkeypatch.setenv("AUTH_JWT_SECRET", "task9-backup-auth-distinct")
    monkeypatch.setattr(archive_module, "_read_pg_dump_version", fake_version)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(BackupCommandFailed):
        await create_backup(
            BackupConfig(
                database_url=migrated_postgres_database_url,
                output=tmp_path / "must-not-exist.dfba",
                key=BACKUP_KEY,
            )
        )

    assert spawned == 0
    assert not (tmp_path / "must-not-exist.dfba").exists()


@pytest.mark.postgres
@pytest.mark.anyio
async def test_backup_rejects_wrong_source_head_before_pg_dump(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("UPDATE alembic_version SET version_num='0015_legacy'"))
    finally:
        await engine.dispose()

    spawned = 0

    async def fail_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned += 1
        raise AssertionError("pg_dump must not start for a pre-M7 source head")

    monkeypatch.setenv("AUTH_JWT_SECRET", "task9-backup-auth-distinct")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(BackupCommandFailed):
        await create_backup(
            BackupConfig(
                database_url=migrated_postgres_database_url,
                output=tmp_path / "must-not-exist.dfba",
                key=BACKUP_KEY,
            )
        )

    assert spawned == 0
    assert not (tmp_path / "must-not-exist.dfba").exists()


@pytest.mark.anyio
async def test_restore_rejects_pre_m7_before_target_resolution_or_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_authenticate(
        _archive: Path,
        _key: bytes,
        dump: OwnedFile,
    ) -> restore_module._AuthenticatedArchive:
        calls.append("authenticate")
        return restore_module._AuthenticatedArchive(
            archive_id="00000000-0000-0000-0000-000000000001",
            archive_schema_version=6,
            schema_revision="0015_project_reliability_finalize",
            schema_digest="a" * 64,
            source_installation_id=SOURCE_ID,
            tombstone_journal_sequence=0,
            table_count=1,
            archive_digest="b" * 64,
            dump_path=dump.path,
            dump_identity=dump.identity,
        )

    def fail_target_resolution(_config: RestoreConfig) -> str:
        calls.append("target-resolution")
        raise AssertionError("target was resolved before archive support was checked")

    async def fail_target_lookup(*_args: object, **_kwargs: object) -> bool:
        calls.append("target-lookup")
        raise AssertionError("target existence was checked before archive support")

    monkeypatch.setattr(restore_module, "_authenticate_archive", fake_authenticate)
    monkeypatch.setattr(restore_module, "_validate_target", fail_target_resolution)
    monkeypatch.setattr(restore_module, "_database_exists", fail_target_lookup)

    journal = TombstoneJournal(
        tmp_path / "journal" / "tombstones.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )
    unsupported = getattr(archive_module, "UnsupportedArchiveSchema", RuntimeError)
    with pytest.raises(unsupported, match="UNSUPPORTED_ARCHIVE_SCHEMA"):
        await Restorer(
            RestoreConfig(
                archive=tmp_path / "pre-m7.dfba",
                target_database_url=TARGET_URL,
                current_database_url=SOURCE_URL,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()

    assert calls == ["authenticate"]
