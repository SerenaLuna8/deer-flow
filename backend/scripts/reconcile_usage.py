#!/usr/bin/env python3
"""Preview or execute aggregate-safe M6 quota reconciliation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.quotas.models import _issue_quota_reconciliation_authority
from app.quotas.reconciliation import QuotaReconciler
from app.quotas.service import QuotaService
from app.reliability.owner_refs import AuditHmacKeyring, AuditHmacKeyringInvalid
from deerflow.config.quota_config import QuotaConfig


class UsageReconciliationError(RuntimeError):
    """Content-free operator error."""


@dataclass(frozen=True, slots=True)
class UsageReconciliationSummary:
    mode: str
    project_count: int
    difference_count: int
    repaired_project_count: int


async def reconcile_usage(
    database_url: str,
    *,
    execute: bool,
    project_id: uuid.UUID | None,
) -> UsageReconciliationSummary:
    if execute and project_id is None:
        raise UsageReconciliationError("execute requires an explicit project id")
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        try:
            keyring = AuditHmacKeyring.from_environment()
        except AuditHmacKeyringInvalid:
            raise UsageReconciliationError("audit HMAC keyring is unavailable") from None
        service = QuotaService(factory, QuotaConfig(), source_ref_hasher=keyring)
        reconciler = QuotaReconciler(factory, service)
        async with engine.connect() as connection:
            if project_id is None:
                projects = list((await connection.execute(text("SELECT id FROM projects ORDER BY id"))).scalars())
            else:
                exists = await connection.scalar(text("SELECT EXISTS (SELECT 1 FROM projects WHERE id=:id)"), {"id": project_id})
                if not exists:
                    raise UsageReconciliationError("project is unavailable")
                projects = [project_id]
        difference_count = 0
        repaired_count = 0
        for selected in projects:
            authority = _issue_quota_reconciliation_authority(
                selected,
                operation="quota_repair",
            )
            report = await (reconciler.execute(authority) if execute else reconciler.preview(authority))
            difference_count += len(report.differences)
            repaired_count += int(report.applied)
        return UsageReconciliationSummary(
            mode="execute" if execute else "dry-run",
            project_count=len(projects),
            difference_count=difference_count,
            repaired_project_count=repaired_count,
        )
    except UsageReconciliationError:
        raise
    except Exception as error:
        raise UsageReconciliationError(f"usage reconciliation failed: {type(error).__name__}") from None
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--project-id", type=uuid.UUID)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("error: DATABASE_URL is required", file=os.sys.stderr)
        return 2
    try:
        report = asyncio.run(
            reconcile_usage(
                database_url,
                execute=args.execute,
                project_id=args.project_id,
            )
        )
    except UsageReconciliationError as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "difference_count": report.difference_count,
                "mode": report.mode,
                "project_count": report.project_count,
                "repaired_project_count": report.repaired_project_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
