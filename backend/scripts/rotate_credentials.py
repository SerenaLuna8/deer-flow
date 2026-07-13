#!/usr/bin/env python3
# ruff: noqa: E402
"""显式轮换 PostgreSQL credential envelopes。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.shared_assets.crypto import EncryptedEnvelope, decrypt_credential_payload, encrypt_credential_payload
from app.shared_assets.keyring import CredentialKeyring, CredentialKeyringInvalid
from deerflow.config.database_config import DatabaseConfig
from deerflow.persistence.shared_assets import CredentialEnvelopeRow, CredentialRow, CredentialVersionRow


class CredentialRotationError(RuntimeError):
    """不泄漏 credential 内容的轮换失败。"""


@dataclass(frozen=True)
class RotationCursor:
    version_id: uuid.UUID


def _cursor(value: str) -> RotationCursor:
    try:
        return RotationCursor(uuid.UUID(value))
    except (AttributeError, TypeError, ValueError):
        raise argparse.ArgumentTypeError("resume cursor must be a UUID") from None


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("batch size must be a positive integer") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError("batch size must be a positive integer")
    return parsed


def build_rotation_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="轮换 credential envelope；默认 batch size 为 100")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--batch-size", type=_positive_int, default=100)
    parser.add_argument("--resume-cursor", type=_cursor)
    return parser


def keyring_for_target(keyring: CredentialKeyring, key_id: str) -> CredentialKeyring:
    try:
        if keyring.active_key_id != key_id:
            raise KeyError
        key = keyring.key_for(key_id)
        return CredentialKeyring(active_key_id=key_id, _keys={key_id: key})
    except (KeyError, CredentialKeyringInvalid):
        raise CredentialKeyringInvalid() from None


def build_rotation_selection(*, target_key_id: str, cursor: RotationCursor | None, batch_size: int):
    statement = (
        select(CredentialVersionRow, CredentialRow, CredentialEnvelopeRow)
        .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
        .join(
            CredentialEnvelopeRow,
            (CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id) & CredentialEnvelopeRow.is_active.is_(True),
        )
        .where(CredentialEnvelopeRow.key_id != target_key_id, CredentialVersionRow.status != "revoked")
        .where(CredentialRow.status == "active")
    )
    # ``SKIP LOCKED`` may temporarily omit a smaller UUID.  Completion is
    # therefore proved by the target-key predicate, never by a UUID high-water
    # mark.  ``cursor`` remains operator/audit metadata only; every batch and
    # resumed run rescans all still-eligible rows.
    del cursor
    return (
        statement.order_by(CredentialVersionRow.id)
        .limit(batch_size)
        .with_for_update(
            of=CredentialVersionRow,
            skip_locked=True,
        )
    )


def build_rotation_pending_selection(*, target_key_id: str):
    """Blocking completion barrier for targets hidden by ``SKIP LOCKED``.

    An empty worker page cannot distinguish exhaustion from contention.  This
    query waits for the first remaining target lock and only reports complete
    when no eligible row exists.
    """

    return (
        select(CredentialVersionRow.id)
        .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
        .join(
            CredentialEnvelopeRow,
            (CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id) & CredentialEnvelopeRow.is_active.is_(True),
        )
        .where(
            CredentialEnvelopeRow.key_id != target_key_id,
            CredentialVersionRow.status != "revoked",
            CredentialRow.status == "active",
        )
        .order_by(CredentialVersionRow.id)
        .limit(1)
        .with_for_update(of=CredentialVersionRow)
    )


def build_rotation_plan_count(*, target_key_id: str):
    return (
        select(func.count())
        .select_from(CredentialVersionRow)
        .join(CredentialRow, CredentialRow.id == CredentialVersionRow.credential_id)
        .join(
            CredentialEnvelopeRow,
            (CredentialEnvelopeRow.credential_version_id == CredentialVersionRow.id) & CredentialEnvelopeRow.is_active.is_(True),
        )
        .where(
            CredentialEnvelopeRow.key_id != target_key_id,
            CredentialVersionRow.status != "revoked",
            CredentialRow.status == "active",
        )
    )


def validate_payload_schema(payload: Mapping[str, object], schema: Mapping[str, object]) -> None:
    try:
        actual = {section: sorted(values) for section, values in payload.items() if isinstance(values, Mapping)}
        expected = {section: sorted(values) for section, values in schema.items()}
        if actual != expected:
            raise ValueError
    except Exception:
        raise CredentialRotationError("credential payload schema validation failed") from None


@dataclass(frozen=True)
class RotationResult:
    planned: int = 0
    rotated: int = 0
    resume_cursor: RotationCursor | None = None
    batches: int = 0


class RotationLedger:
    """Append-only, secret-free operator ledger for one execute run."""

    def __init__(self, root: Path):
        self.run_id = uuid.uuid4()
        self.run_dir = root / str(self.run_id)
        if any(path.is_symlink() for path in (root, *root.parents)):
            raise CredentialRotationError("credential rotation ledger path invalid")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.run_dir.mkdir(mode=0o700)
        os.chmod(self.run_dir, 0o700)
        self.path = self.run_dir / "ledger.jsonl"

    def record(self, payload: Mapping[str, object]) -> None:
        content = (json.dumps(dict(payload), separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            raise CredentialRotationError("credential rotation ledger write failed") from None


class CredentialRotationRunner:
    def __init__(
        self,
        session_factory,
        keyring: CredentialKeyring,
        *,
        target_key_id: str,
        batch_size: int = 100,
        ledger: RotationLedger | None = None,
    ):
        if batch_size < 1:
            raise CredentialRotationError("credential rotation batch size invalid")
        self.session_factory = session_factory
        self.keyring = keyring
        self.target_key_id = target_key_id
        self.batch_size = batch_size
        self.target_keyring = keyring_for_target(keyring, target_key_id)
        self.ledger = ledger

    async def _lock_active_envelope(self, session, version_id: uuid.UUID) -> CredentialEnvelopeRow:
        row = (
            await session.execute(
                select(CredentialEnvelopeRow)
                .where(
                    CredentialEnvelopeRow.credential_version_id == version_id,
                    CredentialEnvelopeRow.is_active.is_(True),
                )
                .with_for_update(of=CredentialEnvelopeRow)
            )
        ).scalar_one_or_none()
        if row is None:
            raise CredentialRotationError("credential active envelope unavailable")
        return row

    async def _rotate_one(
        self,
        session,
        version: CredentialVersionRow,
        credential: CredentialRow,
        selected_envelope: CredentialEnvelopeRow,
    ) -> None:
        active = await self._lock_active_envelope(session, version.id)
        if active.id != selected_envelope.id or active.key_id == self.target_key_id:
            raise CredentialRotationError("credential envelope changed during rotation")
        try:
            payload = decrypt_credential_payload(
                EncryptedEnvelope(key_id=active.key_id, nonce=bytes(active.nonce), ciphertext=bytes(active.ciphertext)),
                credential.scope,
                credential.project_id,
                uuid.UUID(str(version.id)),
                self.keyring,
            )
            validate_payload_schema(payload, version.payload_schema)
            encrypted = encrypt_credential_payload(
                payload,
                credential.scope,
                credential.project_id,
                uuid.UUID(str(version.id)),
                self.target_keyring,
            )
            # Verify before any database state changes.
            verified = decrypt_credential_payload(
                encrypted,
                credential.scope,
                credential.project_id,
                uuid.UUID(str(version.id)),
                self.target_keyring,
            )
            validate_payload_schema(verified, version.payload_schema)
        except CredentialRotationError:
            raise
        except Exception:
            raise CredentialRotationError("credential envelope authentication failed") from None
        generation = int((await session.execute(select(func.coalesce(func.max(CredentialEnvelopeRow.envelope_generation), 0) + 1).where(CredentialEnvelopeRow.credential_version_id == version.id))).scalar_one())
        active.is_active = False
        await session.flush()
        replacement = CredentialEnvelopeRow(
            credential_version_id=version.id,
            envelope_generation=generation,
            key_id=encrypted.key_id,
            nonce=encrypted.nonce,
            ciphertext=encrypted.ciphertext,
            is_active=True,
            created_by_user_id=active.created_by_user_id,
            rotated_from_envelope_id=active.id,
            activated_at=datetime.now(UTC),
        )
        session.add(replacement)
        await session.flush()

    async def run(
        self,
        *,
        execute: bool,
        resume_cursor: RotationCursor | None = None,
        max_batches: int | None = None,
    ) -> RotationResult:
        if max_batches is not None and max_batches < 1:
            raise CredentialRotationError("credential rotation max batches invalid")
        if not execute:
            try:
                async with self.session_factory() as session:
                    async with session.begin():
                        planned = int((await session.execute(build_rotation_plan_count(target_key_id=self.target_key_id))).scalar_one())
                return RotationResult(planned=planned, resume_cursor=resume_cursor)
            except Exception:
                raise CredentialRotationError("credential rotation database operation failed") from None
        cursor = resume_cursor
        planned = 0
        rotated = 0
        batches = 0
        completed = False
        try:
            while max_batches is None or batches < max_batches:
                rows: tuple[tuple[CredentialVersionRow, CredentialRow, CredentialEnvelopeRow], ...]
                async with self.session_factory() as session:
                    try:
                        async with session.begin():
                            rows = tuple(
                                (
                                    await session.execute(
                                        build_rotation_selection(
                                            target_key_id=self.target_key_id,
                                            cursor=cursor,
                                            batch_size=self.batch_size,
                                        )
                                    )
                                ).tuples()
                            )
                            if not rows:
                                pending = (
                                    await session.execute(
                                        build_rotation_pending_selection(
                                            target_key_id=self.target_key_id,
                                        )
                                    )
                                ).scalar_one_or_none()
                                if pending is None:
                                    completed = True
                                    break
                                continue
                            for version, credential, envelope in rows:
                                await self._rotate_one(session, version, credential, envelope)
                            planned += len(rows)
                            rotated += len(rows)
                            cursor = RotationCursor(uuid.UUID(str(rows[-1][0].id)))
                    except CredentialRotationError:
                        raise
                    except Exception:
                        raise CredentialRotationError("credential rotation database operation failed") from None
                batches += 1
                if self.ledger is not None:
                    self.ledger.record(
                        {
                            "status": "batch_committed",
                            "batch": batches,
                            "rotated": len(rows),
                            "resume_cursor": str(cursor.version_id) if cursor else None,
                        }
                    )
        except CredentialRotationError:
            if execute and self.ledger is not None:
                self.ledger.record(
                    {
                        "status": "failed",
                        "batches": batches,
                        "rotated": rotated,
                        "resume_cursor": str(cursor.version_id) if cursor else None,
                    }
                )
            raise
        if self.ledger is not None:
            self.ledger.record(
                {
                    "status": "completed" if completed else "incomplete",
                    "batches": batches,
                    "planned": planned,
                    "rotated": rotated,
                    "resume_cursor": str(cursor.version_id) if cursor else None,
                }
            )
        return RotationResult(planned=planned, rotated=rotated, resume_cursor=cursor, batches=batches)


async def _run_cli(args: argparse.Namespace) -> RotationResult:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise CredentialRotationError("DATABASE_URL is required")
    keyring = CredentialKeyring.from_environment()
    config = DatabaseConfig(url=database_url)
    engine = create_async_engine(config.sqlalchemy_url)
    try:
        ledger = RotationLedger(Path(__file__).resolve().parents[2] / ".deer-flow/migrations/credentials") if args.execute else None
        runner = CredentialRotationRunner(
            async_sessionmaker(engine, expire_on_commit=False),
            keyring,
            target_key_id=args.key_id,
            batch_size=args.batch_size,
            ledger=ledger,
        )
        return await runner.run(execute=args.execute, resume_cursor=args.resume_cursor)
    finally:
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_rotation_parser().parse_args(argv)
    try:
        result = asyncio.run(_run_cli(args))
        print(
            json.dumps(
                {
                    "mode": "execute" if args.execute else "dry-run",
                    "key_id": args.key_id,
                    "planned": result.planned,
                    "rotated": result.rotated,
                    "batches": result.batches,
                    "resume_cursor": str(result.resume_cursor.version_id) if result.resume_cursor else None,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0
    except (CredentialKeyringInvalid, CredentialRotationError):
        print("credential rotation failed safely", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
