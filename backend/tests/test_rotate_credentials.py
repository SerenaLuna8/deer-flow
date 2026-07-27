from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.shared_assets.crypto import encrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from deerflow.persistence.shared_assets import CredentialEnvelopeRow, CredentialVersionRow
from scripts.rotate_credentials import (
    CredentialRotationError,
    CredentialRotationRunner,
    RotationCursor,
    RotationLedger,
    build_rotation_parser,
    build_rotation_pending_selection,
    build_rotation_plan_count,
    build_rotation_selection,
    keyring_for_target,
    validate_payload_schema,
)


def _keyring() -> CredentialKeyring:
    return CredentialKeyring(active_key_id="next", _keys={"old": b"o" * 32, "next": b"n" * 32})


def test_target_key_must_exist_and_dry_run_is_explicit() -> None:
    parser = build_rotation_parser()
    args = parser.parse_args(["--dry-run", "--key-id", "next"])
    assert args.dry_run is True
    assert args.execute is False
    assert args.batch_size == 100
    with pytest.raises(CredentialKeyringInvalid):
        keyring_for_target(_keyring(), "missing")
    assert keyring_for_target(_keyring(), "next").active_key_id == "next"
    wrong_active = CredentialKeyring(active_key_id="old", _keys={"old": b"o" * 32, "next": b"n" * 32})
    with pytest.raises(CredentialKeyringInvalid):
        keyring_for_target(wrong_active, "next")


def test_rotation_query_orders_uuid_and_uses_skip_locked() -> None:
    cursor = RotationCursor(uuid.uuid4())
    statement = build_rotation_selection(target_key_id="next", cursor=cursor, batch_size=17)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    normalized = " ".join(sql.upper().split())
    assert "ORDER BY CREDENTIAL_VERSIONS.ID" in normalized
    assert "FOR UPDATE OF CREDENTIAL_VERSIONS SKIP LOCKED" in normalized
    assert "LIMIT 17" in normalized
    assert str(cursor.version_id) not in sql


def test_resume_cursor_does_not_exclude_a_previously_locked_lower_uuid() -> None:
    """A cursor is audit metadata; target-key state, not UUID high-water, proves completion."""
    skipped = uuid.UUID(int=1)
    cursor = RotationCursor(uuid.UUID(int=2))

    statement = build_rotation_selection(target_key_id="next", cursor=cursor, batch_size=17)
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    assert str(skipped) not in sql  # The query must remain eligible to select every non-target row.
    assert str(cursor.version_id) not in sql
    assert "credential_versions.id >" not in sql.lower()


def test_empty_skip_locked_batch_requires_authoritative_pending_barrier() -> None:
    """An empty SKIP LOCKED page is not completion until a blocking probe sees no target."""
    statement = build_rotation_pending_selection(target_key_id="next")
    sql = str(statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    normalized = " ".join(sql.upper().split())

    assert "SKIP LOCKED" not in normalized
    assert "FOR UPDATE" in normalized
    assert "LIMIT 1" in normalized
    assert "CREDENTIAL_ENVELOPES.KEY_ID != 'NEXT'" in normalized


@pytest.mark.parametrize(
    "statement",
    (
        build_rotation_selection(
            target_key_id="next",
            cursor=None,
            batch_size=17,
        ),
        build_rotation_pending_selection(target_key_id="next"),
        build_rotation_plan_count(target_key_id="next"),
    ),
)
def test_rotation_queries_exclude_logically_deleted_credentials(statement) -> None:
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "credentials.is_delete IS false" in sql


def test_resume_cursor_requires_uuid_and_batch_is_bounded() -> None:
    parser = build_rotation_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--key-id", "next", "--resume-cursor", "not-a-uuid"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--dry-run", "--key-id", "next", "--batch-size", "0"])


def test_payload_schema_tamper_fails_closed_without_values_in_error() -> None:
    payload = {"headers": {"Authorization": "plain-token"}}
    with pytest.raises(CredentialRotationError) as exc_info:
        validate_payload_schema(payload, {"headers": ["X-Other"]})
    assert "plain-token" not in str(exc_info.value)


def test_keyring_environment_example_never_requires_plaintext() -> None:
    encoded = base64.b64encode(b"n" * 32).decode("ascii")
    parsed = json.loads(json.dumps({"next": encoded}))
    assert "next" in parsed


async def _seed_credential(
    engine,
    keyring: CredentialKeyring,
    *,
    tamper: bool = False,
    version_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    actor_id = str(uuid.uuid4())
    credential_id = uuid.uuid4()
    version_id = version_id or uuid.uuid4()
    old_ring = CredentialKeyring(active_key_id="old", _keys={"old": keyring.key_for("old")})
    encrypted = encrypt_credential_payload(
        {"headers": {"Authorization": "plain-token"}},
        "system",
        None,
        version_id,
        old_ring,
    )
    ciphertext = encrypted.ciphertext[:-1] + bytes([encrypted.ciphertext[-1] ^ 1]) if tamper else encrypted.ciphertext
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """INSERT INTO users (id,email,system_role,created_at,needs_setup,token_version)
                VALUES (:id,:email,'system_admin',:now,false,0)"""
            ),
            {"id": actor_id, "email": f"rotate-{actor_id}@example.com", "now": datetime.now(UTC)},
        )
        await connection.execute(
            text(
                """INSERT INTO credentials
                (id,scope,project_id,name,display_name,credential_type,status,current_version_id,created_by_user_id)
                VALUES (:id,'system',NULL,:name,:name,'headers','active',NULL,:actor)"""
            ),
            {"id": credential_id, "name": f"cred-{str(credential_id)[:8]}", "actor": actor_id},
        )
        await connection.execute(
            text(
                """INSERT INTO credential_versions
                (id,credential_id,version_number,status,payload_schema_version,payload_schema,created_by_user_id)
                VALUES (:id,:credential,1,'active',1,'{"headers":["Authorization"]}'::jsonb,:actor)"""
            ),
            {"id": version_id, "credential": credential_id, "actor": actor_id},
        )
        await connection.execute(
            text(
                """INSERT INTO credential_envelopes
                (id,credential_version_id,envelope_generation,key_id,nonce,ciphertext,is_active,created_by_user_id,activated_at)
                VALUES (:id,:version,1,'old',:nonce,:ciphertext,true,:actor,:now)"""
            ),
            {
                "id": uuid.uuid4(),
                "version": version_id,
                "nonce": encrypted.nonce,
                "ciphertext": ciphertext,
                "actor": actor_id,
                "now": datetime.now(UTC),
            },
        )
        await connection.execute(
            text("UPDATE credentials SET current_version_id=:version WHERE id=:credential"),
            {"version": version_id, "credential": credential_id},
        )
    return credential_id, version_id


@pytest.mark.asyncio
async def test_rotation_dry_run_writes_nothing_and_success_retires_old_envelope(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _, version_id = await _seed_credential(engine, _keyring())
    runner = CredentialRotationRunner(factory, _keyring(), target_key_id="next", batch_size=1)

    dry = await runner.run(execute=False)
    async with factory() as session:
        assert int((await session.execute(select(func.count()).select_from(CredentialEnvelopeRow))).scalar_one()) == 1
    assert dry.planned == 1 and dry.rotated == 0

    done = await runner.run(execute=True)
    assert done.rotated == 1 and done.resume_cursor == RotationCursor(version_id)
    async with factory() as session:
        envelopes = tuple((await session.execute(select(CredentialEnvelopeRow).where(CredentialEnvelopeRow.credential_version_id == version_id).order_by(CredentialEnvelopeRow.envelope_generation))).scalars().all())
        assert [(item.key_id, item.is_active) for item in envelopes] == [("old", False), ("next", True)]
    await engine.dispose()


@pytest.mark.asyncio
async def test_rotation_batches_resume_and_tamper_rolls_back_current_batch(
    migrated_postgres_database_url: str,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _, first_id = await _seed_credential(engine, _keyring(), version_id=uuid.UUID(int=1))
    _, tampered_id = await _seed_credential(engine, _keyring(), tamper=True, version_id=uuid.UUID(int=2))
    ordered = sorted((first_id, tampered_id))
    runner = CredentialRotationRunner(factory, _keyring(), target_key_id="next", batch_size=1)

    first = await runner.run(execute=True, max_batches=1)
    assert first.rotated == 1 and first.resume_cursor == RotationCursor(ordered[0])
    with pytest.raises(CredentialRotationError) as exc_info:
        await runner.run(execute=True, resume_cursor=first.resume_cursor)
    assert "plain-token" not in str(exc_info.value)

    async with factory() as session:
        first_active = (
            await session.execute(
                select(CredentialEnvelopeRow).where(
                    CredentialEnvelopeRow.credential_version_id == ordered[0],
                    CredentialEnvelopeRow.is_active.is_(True),
                )
            )
        ).scalar_one()
        bad_envelopes = tuple((await session.execute(select(CredentialEnvelopeRow).where(CredentialEnvelopeRow.credential_version_id == ordered[1]))).scalars().all())
        assert first_active.key_id == "next"
        assert len(bad_envelopes) == 1 and bad_envelopes[0].is_active is True and bad_envelopes[0].key_id == "old"
    await engine.dispose()


@pytest.mark.asyncio
async def test_rotation_waits_for_skipped_locked_version_before_completed(
    migrated_postgres_database_url: str,
    tmp_path,
) -> None:
    engine = create_async_engine(migrated_postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _, version_id = await _seed_credential(engine, _keyring(), version_id=uuid.UUID(int=1))
    ledger = RotationLedger(tmp_path / "rotation-ledger")
    runner = CredentialRotationRunner(
        factory,
        _keyring(),
        target_key_id="next",
        batch_size=1,
        ledger=ledger,
    )

    async with factory() as lock_session:
        async with lock_session.begin():
            await lock_session.execute(select(CredentialVersionRow).where(CredentialVersionRow.id == version_id).with_for_update(of=CredentialVersionRow))
            rotation = asyncio.create_task(runner.run(execute=True))
            await asyncio.sleep(0.1)
            assert not rotation.done()
            assert not ledger.path.exists()  # no false completed/incomplete record while target is locked

        result = await asyncio.wait_for(rotation, timeout=5)

    assert result.rotated == 1
    async with factory() as session:
        envelopes = tuple((await session.execute(select(CredentialEnvelopeRow).where(CredentialEnvelopeRow.credential_version_id == version_id).order_by(CredentialEnvelopeRow.envelope_generation))).scalars().all())
    assert [(envelope.key_id, envelope.is_active) for envelope in envelopes] == [("old", False), ("next", True)]
    records = [json.loads(line) for line in ledger.path.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["status"] == "completed"
    assert sum(record.get("rotated", 0) for record in records if record["status"] == "batch_committed") == 1
    await engine.dispose()
