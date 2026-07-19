from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import ValidationError
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
    BackupManifest,
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


def _valid_manifest_payload(tmp_path: Path) -> dict[str, object]:
    output = tmp_path / f"manifest-{uuid.uuid4()}.dfba"
    with BackupArchiveWriter.atomic(
        output,
        BACKUP_KEY,
        source_installation_id=SOURCE_ID,
    ) as writer:
        writer.write_chunk(b"PGDMPstrict-manifest")
        return writer.finalize(
            database_high_watermark=0,
            tombstone_journal_sequence=0,
        ).as_dict()


def _legacy_aad(
    archive_id: str,
    schema_revision: str,
    source_installation_id: str,
    index: int,
) -> bytes:
    return archive_module._canonical_json(
        {
            "archive_id": archive_id,
            "chunk_index": index,
            "schema_revision": schema_revision,
            "source_installation_id": source_installation_id,
        }
    )


def _authenticated_archive(
    root: Path,
    *,
    old_shape: bool,
    bad_hmac: bool = False,
    malformed_v7: bool = False,
) -> Path:
    archive = root / f"archive-{uuid.uuid4()}.dfba"
    chunks = archive / "chunks"
    chunks.mkdir(parents=True)
    archive_id = str(uuid.uuid4())
    salt = bytes(range(32))
    revision = "0015_project_reliability_finalize" if old_shape else "0001_project_saas_baseline"
    digest = "f" * 64 if malformed_v7 else archive_module.M7_CANONICAL_SCHEMA_DIGEST
    plaintext = b"PGDMPauthenticated-old-shape"
    chunk_key = archive_module._derive_key(
        BACKUP_KEY,
        archive_module._CHUNK_INFO,
        salt=salt,
        archive_id=archive_id,
    )
    aad = (
        _legacy_aad(archive_id, revision, SOURCE_ID, 0)
        if old_shape
        else archive_module._aad(
            archive_id,
            archive_schema_version=7,
            schema_revision=revision,
            schema_digest=digest,
            source_installation_id=SOURCE_ID,
            index=0,
        )
    )
    ciphertext = AESGCM(chunk_key).encrypt(bytes(12), plaintext, aad)
    (chunks / "00000000.bin").write_bytes(ciphertext)
    body: dict[str, object] = {
        "archive_id": archive_id,
        "archive_format_version": 1,
        "archive_salt": base64.b64encode(salt).decode("ascii"),
        "schema_revision": revision,
        "source_installation_id": SOURCE_ID,
        "chunk_bytes": archive_module.CHUNK_SIZE,
        "chunks": [
            {
                "index": 0,
                "nonce": base64.b64encode(bytes(12)).decode("ascii"),
                "plaintext_bytes": len(plaintext),
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
                "ciphertext_bytes": len(ciphertext),
            }
        ],
        "total_plaintext_bytes": len(plaintext),
        "total_ciphertext_bytes": len(ciphertext),
        "database_high_watermark": 0,
        "tombstone_journal_sequence": 0,
        "table_count": 1,
        "pg_dump_version": "pg_dump (PostgreSQL) 14.19",
        "tool": "pg_dump --format=custom --no-owner --no-acl",
    }
    if not old_shape:
        body["archive_schema_version"] = 7
        body["schema_digest"] = digest
    signature = archive_module._manifest_signature(BACKUP_KEY, body)
    if bad_hmac:
        signature = ("0" if signature[0] != "0" else "1") + signature[1:]
    (archive / "manifest.json").write_bytes(archive_module._canonical_json({"manifest": body, "signature": signature}))
    return archive


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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("archive_schema_version", 6),
        ("schema_revision", "0015_project_reliability_finalize"),
        ("schema_digest", "f" * 64),
    ),
)
def test_manifest_model_rejects_noncanonical_schema_contract(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    payload = _valid_manifest_payload(tmp_path)
    payload[field] = value

    with pytest.raises(ValidationError):
        BackupManifest.model_validate(payload)
    with pytest.raises(ValidationError):
        BackupManifest(**payload)


def test_manifest_model_is_strict_frozen_and_forbids_extra(tmp_path: Path) -> None:
    payload = _valid_manifest_payload(tmp_path)
    payload["unexpected"] = "legacy"
    with pytest.raises(ValidationError):
        BackupManifest.model_validate(payload)

    payload.pop("unexpected")
    payload["archive_schema_version"] = "7"
    with pytest.raises(ValidationError):
        BackupManifest.model_validate(payload)

    manifest = BackupManifest.model_validate(_valid_manifest_payload(tmp_path))
    with pytest.raises(ValidationError):
        manifest.archive_schema_version = 6


def test_manifest_json_schema_exposes_fixed_version_and_revision() -> None:
    properties = BackupManifest.model_json_schema()["properties"]

    assert properties["archive_schema_version"]["const"] == 7
    assert properties["schema_revision"]["const"] == "0001_project_saas_baseline"


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


def test_catalog_digest_placeholder_only_normalizes_current_restore_proof_digest() -> None:
    canonical = f"CHECK (archive_schema_version = 7 AND schema_revision::text = '0001_project_saas_baseline'::text AND schema_digest = '{schema_contract.M7_CANONICAL_SCHEMA_DIGEST}'::bpchar)"
    placeholder = canonical.replace(
        schema_contract.M7_CANONICAL_SCHEMA_DIGEST,
        "__M7_CANONICAL_SCHEMA_DIGEST__",
    )
    wrong = canonical.replace(schema_contract.M7_CANONICAL_SCHEMA_DIGEST, "f" * 64)

    assert schema_contract.M7_CANONICAL_SCHEMA_DIGEST != "f" * 64
    assert schema_contract._rows_digest(((canonical,),)) == schema_contract._rows_digest(((placeholder,),))
    assert schema_contract._rows_digest(((wrong,),)) != schema_contract._rows_digest(((canonical,),))
    assert schema_contract.M7_CANONICAL_SCHEMA_DIGEST == schema_contract._catalog_signature_digest(schema_contract.FINAL_M7_CATALOG_SIGNATURE)


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


def test_authenticated_old_shape_archive_is_unsupported(tmp_path: Path) -> None:
    archive = _authenticated_archive(tmp_path, old_shape=True)

    with pytest.raises(
        archive_module.UnsupportedArchiveSchema,
        match="UNSUPPORTED_ARCHIVE_SCHEMA",
    ):
        list(BackupArchiveReader(BACKUP_KEY).verified_chunks(archive))


def test_old_shape_bad_hmac_and_malformed_v7_remain_authentication_failures(
    tmp_path: Path,
) -> None:
    old_bad_hmac = _authenticated_archive(
        tmp_path,
        old_shape=True,
        bad_hmac=True,
    )
    malformed_v7 = _authenticated_archive(
        tmp_path,
        old_shape=False,
        malformed_v7=True,
    )

    for archive in (old_bad_hmac, malformed_v7):
        with pytest.raises(BackupAuthenticationFailed):
            list(BackupArchiveReader(BACKUP_KEY).verified_chunks(archive))


@pytest.mark.anyio
async def test_genuine_old_archive_rejected_before_target_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = _authenticated_archive(tmp_path, old_shape=True)
    target_calls = 0

    def fail_target_resolution(_config: RestoreConfig) -> str:
        nonlocal target_calls
        target_calls += 1
        raise AssertionError("old archive reached target parsing")

    async def fail_target_lookup(*_args: object, **_kwargs: object) -> bool:
        nonlocal target_calls
        target_calls += 1
        raise AssertionError("old archive reached target lookup")

    monkeypatch.setattr(restore_module, "_validate_target", fail_target_resolution)
    monkeypatch.setattr(restore_module, "_database_exists", fail_target_lookup)
    journal = TombstoneJournal(
        tmp_path / "journal" / "tombstones.jsonl",
        JOURNAL_KEY,
        source_installation_id=SOURCE_ID,
    )

    with pytest.raises(
        archive_module.UnsupportedArchiveSchema,
        match="UNSUPPORTED_ARCHIVE_SCHEMA",
    ):
        await Restorer(
            RestoreConfig(
                archive=archive,
                target_database_url=TARGET_URL,
                current_database_url=SOURCE_URL,
                journal=journal,
                backup_key=BACKUP_KEY,
                keyring=_keyring(),
            )
        ).restore()

    assert target_calls == 0


async def _install_langgraph_schema(database_url: str) -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from langgraph.store.postgres.aio import AsyncPostgresStore

    psycopg_url = database_url.replace(
        "postgresql+asyncpg://",
        "postgresql://",
        1,
    )
    async with AsyncPostgresSaver.from_conn_string(psycopg_url) as saver:
        await saver.setup()
    async with AsyncPostgresStore.from_conn_string(psycopg_url) as store:
        await store.setup()


@pytest.mark.parametrize(
    "drift_sql",
    (
        "CREATE SEQUENCE rogue_job_attempt_sequence OWNED BY jobs.attempt_count",
        "CREATE INDEX rogue_checkpoints_thread_idx ON checkpoints (thread_id)",
        "CREATE FUNCTION rogue_backup_fn() RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 1'",
    ),
    ids=("owned-sequence", "langgraph-index", "routine"),
)
@pytest.mark.postgres
@pytest.mark.anyio
async def test_exported_snapshot_rejects_root_inventory_toctou_before_pg_dump(
    migrated_postgres_database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift_sql: str,
) -> None:
    await _install_langgraph_schema(migrated_postgres_database_url)
    real_precheck = archive_module._require_exact_m7_source

    async def precheck_then_inject(database_url: str) -> None:
        await real_precheck(database_url)
        engine = create_async_engine(database_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(drift_sql))
        finally:
            await engine.dispose()

    async def fake_version() -> str:
        return "pg_dump (PostgreSQL) 14.19"

    spawned = 0

    async def fail_spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned += 1
        raise AssertionError("pg_dump spawned after exported-snapshot drift")

    monkeypatch.setenv("AUTH_JWT_SECRET", "task9-toctou-distinct-auth-secret")
    monkeypatch.setattr(
        archive_module,
        "_require_exact_m7_source",
        precheck_then_inject,
    )
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
