from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.persistence.quotas.model import (
    ProjectQuotaRow,
    ProjectUsageCounterRow,
    ProjectUsageLedgerRow,
)


class QuotaRepository:
    """Session-bound quota persistence with caller-owned transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def policy(self, project_id: uuid.UUID) -> ProjectQuotaRow | None:
        return await self.session.get(ProjectQuotaRow, project_id)

    async def lock_counter(
        self,
        project_id: uuid.UUID,
        dimension: str,
        bucket: str,
    ) -> ProjectUsageCounterRow:
        await self.session.execute(
            pg_insert(ProjectUsageCounterRow)
            .values(
                project_id=project_id,
                dimension=dimension,
                bucket=bucket,
                used=0,
                reserved=0,
                version=1,
            )
            .on_conflict_do_nothing(
                index_elements=("project_id", "dimension", "bucket"),
            )
        )
        row = (
            await self.session.execute(
                select(ProjectUsageCounterRow)
                .where(
                    ProjectUsageCounterRow.project_id == project_id,
                    ProjectUsageCounterRow.dimension == dimension,
                    ProjectUsageCounterRow.bucket == bucket,
                )
                .with_for_update(of=ProjectUsageCounterRow)
            )
        ).scalar_one()
        return row

    async def counter(
        self,
        project_id: uuid.UUID,
        dimension: str,
        bucket: str,
    ) -> ProjectUsageCounterRow | None:
        return (
            await self.session.execute(
                select(ProjectUsageCounterRow).where(
                    ProjectUsageCounterRow.project_id == project_id,
                    ProjectUsageCounterRow.dimension == dimension,
                    ProjectUsageCounterRow.bucket == bucket,
                )
            )
        ).scalar_one_or_none()

    async def ledger_entry(
        self,
        project_id: uuid.UUID,
        dimension: str,
        idempotency_key: str,
    ) -> ProjectUsageLedgerRow | None:
        return (
            await self.session.execute(
                select(ProjectUsageLedgerRow).where(
                    ProjectUsageLedgerRow.project_id == project_id,
                    ProjectUsageLedgerRow.dimension == dimension,
                    ProjectUsageLedgerRow.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def threshold_recorded(
        self,
        project_id: uuid.UUID,
        dimension: str,
        bucket: str,
    ) -> bool:
        return bool(
            await self.session.scalar(
                select(ProjectUsageLedgerRow.id).where(
                    ProjectUsageLedgerRow.project_id == project_id,
                    ProjectUsageLedgerRow.dimension == dimension,
                    ProjectUsageLedgerRow.bucket == bucket,
                    ProjectUsageLedgerRow.source_kind.in_(
                        (
                            "consume_threshold",
                            "policy_threshold",
                            "reconcile_threshold",
                            "reserve_threshold",
                        ),
                    ),
                )
            )
        )

    async def append_ledger(
        self,
        *,
        project_id: uuid.UUID,
        dimension: str,
        delta: int,
        bucket: str,
        source_kind: str,
        source_ref_key_id: str,
        source_ref_hmac: str,
        idempotency_key: str,
        request_id: str | None,
        occurred_at: datetime,
    ) -> ProjectUsageLedgerRow:
        row = ProjectUsageLedgerRow(
            project_id=project_id,
            dimension=dimension,
            delta=delta,
            bucket=bucket,
            source_kind=source_kind,
            source_ref_key_id=source_ref_key_id,
            source_ref_hmac=source_ref_hmac,
            idempotency_key=idempotency_key,
            request_id=request_id,
            occurred_at=occurred_at.astimezone(UTC),
        )
        self.session.add(row)
        await self.session.flush()
        return row


__all__ = ["QuotaRepository"]
