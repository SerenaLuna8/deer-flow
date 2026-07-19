#!/usr/bin/env python3
# ruff: noqa: E402
"""Restore one authenticated archive into a new operator-owned database."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.recovery import (
    BackupKeyInvalid,
    BackupKeyMissing,
    UnsupportedArchiveSchema,
    load_backup_key,
)
from app.recovery.journal import TombstoneJournal, TombstoneJournalUnavailable, load_journal_key
from app.recovery.restore import (
    RecoveryProbeFailed,
    RestoreAuthenticationFailed,
    RestoreCommandFailed,
    RestoreConfig,
    Restorer,
    RestoreResult,
    RestoreTargetRejected,
)
from app.reliability.owner_refs import AuditHmacKeyring, AuditHmacKeyringInvalid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore an authenticated DeerFlow archive into a new empty database")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    return parser


def public_restore_result(result: RestoreResult) -> dict[str, object]:
    return {
        "archive_id": result.archive_id,
        "archive_schema_version": result.archive_schema_version,
        "schema_revision": result.schema_revision,
        "schema_digest": result.schema_digest,
        "table_count": result.table_count,
        "status": result.status,
        "checksum": result.checksum[:16],
        "proof": str(result.proof_id),
    }


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print("RESTORE_EXECUTE_REQUIRED", file=sys.stderr)
        return 2
    try:
        current_url = os.environ.get("DATABASE_URL")
        if not current_url:
            raise RestoreTargetRejected
        backup_key = load_backup_key(database_url=current_url)
        journal_key = load_journal_key(database_url=current_url)
        result = await Restorer(
            RestoreConfig(
                archive=args.archive,
                target_database_url=args.target_url,
                current_database_url=current_url,
                journal=TombstoneJournal(args.journal, journal_key),
                backup_key=backup_key,
                keyring=AuditHmacKeyring.from_environment(),
            )
        ).restore()
    except UnsupportedArchiveSchema:
        print("UNSUPPORTED_ARCHIVE_SCHEMA", file=sys.stderr)
        return 1
    except (
        AuditHmacKeyringInvalid,
        BackupKeyInvalid,
        BackupKeyMissing,
        RecoveryProbeFailed,
        RestoreAuthenticationFailed,
        RestoreCommandFailed,
        RestoreTargetRejected,
        TombstoneJournalUnavailable,
    ):
        print("RESTORE_FAILED", file=sys.stderr)
        return 1
    print(json.dumps(public_restore_result(result), sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":  # pragma: no cover - Make entrypoint
    raise SystemExit(main())
