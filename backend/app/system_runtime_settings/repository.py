"""Caller-transaction PostgreSQL repository for runtime policy."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.system_runtime_settings.models import RuntimePolicySection
from deerflow.persistence.system_runtime_settings import (
    RunRuntimePolicySnapshotRow,
    SystemRuntimePolicyCatalogStateRow,
    SystemRuntimePolicyRow,
    SystemRuntimePolicyVersionRow,
)


class SystemRuntimePolicyRepositoryInvariant(Exception):
    pass


class SystemRuntimePolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def catalog_state(
        self,
        *,
        for_update: bool = False,
    ) -> SystemRuntimePolicyCatalogStateRow:
        statement = select(SystemRuntimePolicyCatalogStateRow).where(
            SystemRuntimePolicyCatalogStateRow.id == 1,
        )
        if for_update:
            statement = statement.with_for_update(of=SystemRuntimePolicyCatalogStateRow)
        row = (await self.session.execute(statement)).scalar_one_or_none()
        if row is None:
            raise SystemRuntimePolicyRepositoryInvariant
        return row

    async def current(
        self,
        section: RuntimePolicySection | str,
        *,
        for_update: bool = False,
    ) -> tuple[SystemRuntimePolicyRow, SystemRuntimePolicyVersionRow]:
        try:
            parsed_section = RuntimePolicySection(section)
        except ValueError:
            raise SystemRuntimePolicyRepositoryInvariant from None
        statement = (
            select(SystemRuntimePolicyRow, SystemRuntimePolicyVersionRow)
            .join(
                SystemRuntimePolicyVersionRow,
                (SystemRuntimePolicyVersionRow.section == SystemRuntimePolicyRow.section) & (SystemRuntimePolicyVersionRow.id == SystemRuntimePolicyRow.current_version_id),
            )
            .where(SystemRuntimePolicyRow.section == parsed_section.value)
        )
        if for_update:
            statement = statement.with_for_update(
                of=(SystemRuntimePolicyRow, SystemRuntimePolicyVersionRow),
            )
        result = (await self.session.execute(statement)).one_or_none()
        if result is None:
            raise SystemRuntimePolicyRepositoryInvariant
        policy, version = result
        if policy.current_version_id != version.id or policy.section != version.section:
            raise SystemRuntimePolicyRepositoryInvariant
        return policy, version

    async def list_current(
        self,
    ) -> tuple[tuple[SystemRuntimePolicyRow, SystemRuntimePolicyVersionRow], ...]:
        rows = tuple(
            (policy, version)
            for policy, version in (
                await self.session.execute(
                    select(SystemRuntimePolicyRow, SystemRuntimePolicyVersionRow)
                    .join(
                        SystemRuntimePolicyVersionRow,
                        (SystemRuntimePolicyVersionRow.section == SystemRuntimePolicyRow.section) & (SystemRuntimePolicyVersionRow.id == SystemRuntimePolicyRow.current_version_id),
                    )
                    .order_by(SystemRuntimePolicyRow.section)
                )
            ).all()
        )
        if {policy.section for policy, _version in rows} != {section.value for section in RuntimePolicySection}:
            raise SystemRuntimePolicyRepositoryInvariant
        return rows

    async def add_version(
        self,
        policy: SystemRuntimePolicyRow,
        version: SystemRuntimePolicyVersionRow,
    ) -> None:
        self.session.add(version)
        await self.session.flush()
        policy.current_version_id = version.id
        await self.session.flush()

    async def existing_snapshot(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        section: RuntimePolicySection,
    ) -> RunRuntimePolicySnapshotRow | None:
        return (
            await self.session.execute(
                select(RunRuntimePolicySnapshotRow)
                .where(
                    RunRuntimePolicySnapshotRow.project_id == project_id,
                    RunRuntimePolicySnapshotRow.owner_user_id == owner_user_id,
                    RunRuntimePolicySnapshotRow.run_id == run_id,
                    RunRuntimePolicySnapshotRow.section == section.value,
                )
                .with_for_update(read=True, of=RunRuntimePolicySnapshotRow)
            )
        ).scalar_one_or_none()

    async def add_snapshot(self, snapshot: RunRuntimePolicySnapshotRow) -> None:
        self.session.add(snapshot)
        await self.session.flush()

    async def snapshot_material(
        self,
        *,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        section: RuntimePolicySection,
    ) -> tuple[RunRuntimePolicySnapshotRow, SystemRuntimePolicyVersionRow] | None:
        statement = (
            select(RunRuntimePolicySnapshotRow, SystemRuntimePolicyVersionRow)
            .join(
                SystemRuntimePolicyVersionRow,
                (SystemRuntimePolicyVersionRow.section == RunRuntimePolicySnapshotRow.section)
                & (SystemRuntimePolicyVersionRow.id == RunRuntimePolicySnapshotRow.policy_version_id)
                & (SystemRuntimePolicyVersionRow.schema_version == RunRuntimePolicySnapshotRow.schema_version)
                & (SystemRuntimePolicyVersionRow.payload_checksum == RunRuntimePolicySnapshotRow.payload_checksum),
            )
            .where(
                RunRuntimePolicySnapshotRow.project_id == project_id,
                RunRuntimePolicySnapshotRow.owner_user_id == owner_user_id,
                RunRuntimePolicySnapshotRow.run_id == run_id,
                RunRuntimePolicySnapshotRow.section == section.value,
            )
            .with_for_update(
                read=True,
                of=(RunRuntimePolicySnapshotRow, SystemRuntimePolicyVersionRow),
            )
        )
        result = (await self.session.execute(statement)).one_or_none()
        if result is None:
            return None
        return result


__all__ = [
    "SystemRuntimePolicyRepository",
    "SystemRuntimePolicyRepositoryInvariant",
]
