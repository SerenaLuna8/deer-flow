"""SQLAlchemy-backed thread metadata repository."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.json_compat import json_match
from deerflow.persistence.thread_meta.base import InvalidMetadataFilterError
from deerflow.persistence.thread_meta.model import ThreadMetaRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.utils.time import coerce_iso

logger = logging.getLogger(__name__)


class ThreadMetaRepository:
    """PostgreSQL repository requiring explicit immutable project authority."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _thread_predicate(
        thread_id: str,
        scope: PrivateResourceScope,
    ):
        return (
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
            ThreadMetaRow.owner_user_id == scope.owner_user_id,
            ThreadMetaRow.deleted_at.is_(None),
            ThreadMetaRow.frozen_at.is_(None),
        )

    @staticmethod
    def _row_to_dict(row: ThreadMetaRow) -> dict[str, Any]:
        d = row.to_dict()
        d["user_id"] = d.get("owner_user_id")
        d["metadata"] = d.pop("metadata_json", None) or {}
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                # Normalize legacy naive values as UTC so the wire format always carries tz.
                d[key] = coerce_iso(val)
        return d

    async def create(
        self,
        thread_id: str,
        *,
        assistant_id: str | None = None,
        display_name: str | None = None,
        metadata: dict | None = None,
        scope: PrivateResourceScope,
        agent_asset_id: uuid.UUID,
        agent_scope: str,
    ) -> dict:
        if type(scope) is not PrivateResourceScope or agent_scope not in {"system", "project"}:
            raise RuntimeError("ThreadMetaRepository.create requires scoped final-schema authority")
        resolved_user_id = scope.owner_user_id
        try:
            project_id = uuid.UUID(scope.project_id)
        except (TypeError, ValueError):
            raise RuntimeError("invalid final-schema thread project scope") from None
        now = datetime.now(UTC)
        row = ThreadMetaRow(
            thread_id=thread_id,
            assistant_id=assistant_id,
            user_id=resolved_user_id,
            display_name=display_name,
            metadata_json=metadata or {},
            created_at=now,
            updated_at=now,
            project_id=project_id,
            agent_asset_id=agent_asset_id,
            agent_scope=agent_scope,
        )
        async with self._sf() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
    ) -> dict | None:
        async with self._sf() as session:
            statement = select(ThreadMetaRow).where(*self._thread_predicate(thread_id, scope))
            row = (await session.execute(statement)).scalar_one_or_none()
            return None if row is None else self._row_to_dict(row)

    async def check_access(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
    ) -> bool:
        """Return true only for an active Thread in the exact frozen scope."""
        async with self._sf() as session:
            statement = select(ThreadMetaRow.thread_id).where(*self._thread_predicate(thread_id, scope))
            return (await session.execute(statement)).scalar_one_or_none() is not None

    async def search(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        scope: PrivateResourceScope,
    ) -> list[dict[str, Any]]:
        """Search active Threads inside the exact project-owner scope."""
        stmt = (
            select(ThreadMetaRow)
            .where(
                ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
                ThreadMetaRow.owner_user_id == scope.owner_user_id,
                ThreadMetaRow.deleted_at.is_(None),
                ThreadMetaRow.frozen_at.is_(None),
            )
            .order_by(ThreadMetaRow.updated_at.desc(), ThreadMetaRow.thread_id.desc())
        )
        if status:
            stmt = stmt.where(ThreadMetaRow.status == status)

        if metadata:
            applied = 0
            for key, value in metadata.items():
                try:
                    stmt = stmt.where(json_match(ThreadMetaRow.metadata_json, key, value))
                    applied += 1
                except (ValueError, TypeError) as exc:
                    logger.warning("Skipping metadata filter key %s: %s", ascii(key), exc)
            if applied == 0:
                # Comma-separated plain string (no list repr / nested
                # quoting) so the 400 detail surfaced by the Gateway is
                # easy for clients to read. Sorted for determinism.
                rejected_keys = ", ".join(sorted(str(k) for k in metadata))
                raise InvalidMetadataFilterError(f"All metadata filter keys were rejected as unsafe: {rejected_keys}")

        stmt = stmt.limit(limit).offset(offset)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def update_display_name(
        self,
        thread_id: str,
        display_name: str,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        """Update the display_name (title) for a thread."""
        async with self._sf() as session:
            await session.execute(update(ThreadMetaRow).where(*self._thread_predicate(thread_id, scope)).values(display_name=display_name, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_status(
        self,
        thread_id: str,
        status: str,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        async with self._sf() as session:
            await session.execute(update(ThreadMetaRow).where(*self._thread_predicate(thread_id, scope)).values(status=status, updated_at=datetime.now(UTC)))
            await session.commit()

    async def update_metadata(
        self,
        thread_id: str,
        metadata: dict,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        """Merge ``metadata`` into ``metadata_json``.

        Read-modify-write inside a single session/transaction so concurrent
        callers see consistent state. No-op if the row does not exist or
        the user_id check fails.
        """
        async with self._sf() as session:
            statement = select(ThreadMetaRow).where(*self._thread_predicate(thread_id, scope)).with_for_update(of=ThreadMetaRow)
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                return
            merged = dict(row.metadata_json or {})
            merged.update(metadata)
            row.metadata_json = merged
            row.updated_at = datetime.now(UTC)
            await session.commit()

    async def delete(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
    ) -> None:
        async with self._sf() as session:
            await session.execute(sql_delete(ThreadMetaRow).where(*self._thread_predicate(thread_id, scope)))
            await session.commit()

    async def mark_deleted(
        self,
        thread_id: str,
        *,
        scope: PrivateResourceScope,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._sf() as session:
            result = await session.execute(
                update(ThreadMetaRow)
                .where(*self._thread_predicate(thread_id, scope))
                .values(
                    deleted_at=now,
                    checkpoint_delete_status="pending",
                    updated_at=now,
                    version=ThreadMetaRow.version + 1,
                )
                .returning(ThreadMetaRow.thread_id)
            )
            await session.commit()
            return result.scalar_one_or_none() is not None

    async def set_checkpoint_delete_status(
        self,
        thread_id: str,
        status: str,
        *,
        scope: PrivateResourceScope,
    ) -> bool:
        if status not in {"pending", "complete", "retry_required"}:
            raise ValueError("invalid checkpoint delete status")
        predicate = [
            ThreadMetaRow.thread_id == thread_id,
            ThreadMetaRow.deleted_at.is_not(None),
            ThreadMetaRow.project_id == uuid.UUID(scope.project_id),
            ThreadMetaRow.owner_user_id == scope.owner_user_id,
        ]
        async with self._sf() as session:
            result = await session.execute(
                update(ThreadMetaRow)
                .where(*predicate)
                .values(
                    checkpoint_delete_status=status,
                    updated_at=datetime.now(UTC),
                )
                .returning(ThreadMetaRow.thread_id)
            )
            await session.commit()
            return result.scalar_one_or_none() is not None
