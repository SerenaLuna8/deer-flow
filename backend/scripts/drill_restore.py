#!/usr/bin/env python3
# ruff: noqa: E402
"""Run an authenticated restore drill and always remove its generated database."""

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

from app.recovery import BackupKeyInvalid, BackupKeyMissing, load_backup_key
from app.recovery.journal import TombstoneJournal, TombstoneJournalUnavailable, load_journal_key
from app.recovery.restore import (
    RecoveryProbeFailed,
    RestoreAuthenticationFailed,
    RestoreCommandFailed,
    RestoreTargetRejected,
    drill_restore,
)
from app.reliability.owner_refs import AuditHmacKeyring, AuditHmacKeyringInvalid
from scripts.restore_postgres import public_restore_result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a disposable DeerFlow restore drill")
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        current_url = os.environ.get("DATABASE_URL")
        if not current_url:
            raise RestoreTargetRejected
        result = await drill_restore(
            current_database_url=current_url,
            archive=args.archive,
            journal=TombstoneJournal(
                args.journal,
                load_journal_key(database_url=current_url),
            ),
            backup_key=load_backup_key(database_url=current_url),
            keyring=AuditHmacKeyring.from_environment(),
        )
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
        print("RESTORE_DRILL_FAILED", file=sys.stderr)
        return 1
    payload = public_restore_result(result)
    payload["status"] = "drill_verified"
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":  # pragma: no cover - Make entrypoint
    raise SystemExit(main())
