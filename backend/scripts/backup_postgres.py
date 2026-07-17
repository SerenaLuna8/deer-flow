#!/usr/bin/env python3
# ruff: noqa: E402
"""Create an operator-only encrypted PostgreSQL backup archive."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.recovery.archive import BackupCommandFailed, BackupConfig, BackupKeyInvalid, BackupKeyMissing, create_backup, load_backup_key


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create an authenticated encrypted PostgreSQL backup archive")
    parser.add_argument("--output", required=True, type=Path, help="External operator archive directory")
    parser.add_argument(
        "--pre-m6-cutover",
        action="store_true",
        help="Create an externally committed revision-0013 archive before the M6 database audit schema exists",
    )
    return parser


def _prepare_external_root(root: Path) -> Path:
    expanded = root.expanduser().absolute()
    expanded.mkdir(parents=True, exist_ok=True)
    if stat.S_ISLNK(os.lstat(expanded).st_mode):
        raise ValueError("BACKUP_OUTPUT_MUST_NOT_BE_SYMLINK")
    resolved = expanded.resolve(strict=True)
    repository_root = BACKEND_ROOT.parent.resolve()
    if resolved == repository_root or repository_root in resolved.parents:
        raise ValueError("BACKUP_OUTPUT_MUST_BE_EXTERNAL")
    os.chmod(resolved, 0o700)
    return resolved


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise BackupCommandFailed
        key = load_backup_key(database_url=database_url)
        root = await asyncio.to_thread(_prepare_external_root, args.output)
        archive_id = str(uuid.uuid4())
        archive = root / f"{archive_id}.dfba"
        manifest = await create_backup(
            BackupConfig(
                database_url=database_url,
                output=archive,
                key=key,
                archive_id=archive_id,
                pre_m6_cutover=args.pre_m6_cutover,
                commit_proof=(root / f"{archive.name}.commit.json" if args.pre_m6_cutover else None),
            )
        )
    except (BackupCommandFailed, BackupKeyInvalid, BackupKeyMissing, ValueError, OSError):
        print("BACKUP_FAILED", file=sys.stderr)
        return 1
    digest = hashlib.sha256(json.dumps(manifest.as_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    payload: dict[str, object] = {
        "archive_id": manifest.archive_id,
        "schema_revision": manifest.schema_revision,
        "chunk_count": manifest.chunk_count,
        "checksum": digest,
    }
    if args.pre_m6_cutover:
        payload["commit_proof"] = f"{manifest.archive_id}.dfba.commit.json"
    print(json.dumps(payload, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":  # pragma: no cover - exercised by make backup-db
    raise SystemExit(main())
