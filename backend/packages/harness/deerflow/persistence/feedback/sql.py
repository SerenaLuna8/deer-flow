"""SQLAlchemy-backed feedback storage.

Each method acquires its own short-lived session.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.feedback.model import FeedbackRow
from deerflow.persistence.run.model import RunRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.user_context import AUTO, _AutoSentinel
from deerflow.utils.time import coerce_iso


class FeedbackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise ValueError("private feedback scope is required")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise ValueError("private feedback scope is invalid") from None

    @classmethod
    def _scope_predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls._coordinates(scope)
        return (
            FeedbackRow.project_id == project_id,
            FeedbackRow.owner_user_id == owner_user_id,
        )

    @classmethod
    async def _require_parent_run(
        cls,
        session: AsyncSession,
        *,
        scope: PrivateResourceScope,
        thread_id: str,
        run_id: str,
    ) -> tuple[uuid.UUID, str]:
        project_id, owner_user_id = cls._coordinates(scope)
        parent = (
            await session.execute(
                select(RunRow.project_id, RunRow.owner_user_id).where(
                    RunRow.project_id == project_id,
                    RunRow.owner_user_id == owner_user_id,
                    RunRow.thread_id == thread_id,
                    RunRow.run_id == run_id,
                )
            )
        ).one_or_none()
        if parent is None:
            raise ValueError("scoped parent run not found")
        return parent.project_id, parent.owner_user_id

    @staticmethod
    def _row_to_dict(row: FeedbackRow) -> dict:
        d = row.to_dict()
        val = d.get("created_at")
        if isinstance(val, datetime):
            # Normalize legacy naive values via ``coerce_iso`` so output is always tz-aware.
            d["created_at"] = coerce_iso(val)
        if isinstance(d.get("project_id"), uuid.UUID):
            d["project_id"] = str(d["project_id"])
        return d

    async def create(
        self,
        *,
        run_id: str,
        thread_id: str,
        rating: int,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
        message_id: str | None = None,
        comment: str | None = None,
    ) -> dict:
        """Create a feedback record. rating must be +1 or -1."""
        if rating not in (1, -1):
            raise ValueError(f"rating must be +1 or -1, got {rating}")
        if scope is None:
            raise ValueError("private feedback scope is required")
        async with self._sf() as session:
            project_id, owner_user_id = await self._require_parent_run(
                session,
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
            )
            row = FeedbackRow(
                feedback_id=str(uuid.uuid4()),
                run_id=run_id,
                thread_id=thread_id,
                project_id=project_id,
                owner_user_id=owner_user_id,
                message_id=message_id,
                rating=rating,
                comment=comment,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def get(
        self,
        feedback_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> dict | None:
        if scope is None:
            return None
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(FeedbackRow).where(
                        FeedbackRow.feedback_id == feedback_id,
                        *self._scope_predicates(scope),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_dict(row)

    async def list_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        limit: int = 100,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        if scope is None:
            return []
        stmt = select(FeedbackRow).where(
            FeedbackRow.thread_id == thread_id,
            FeedbackRow.run_id == run_id,
            *self._scope_predicates(scope),
        )
        stmt = stmt.order_by(FeedbackRow.created_at.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def list_by_thread(
        self,
        thread_id: str,
        *,
        limit: int = 100,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> list[dict]:
        if scope is None:
            return []
        stmt = select(FeedbackRow).where(
            FeedbackRow.thread_id == thread_id,
            *self._scope_predicates(scope),
        )
        stmt = stmt.order_by(FeedbackRow.created_at.asc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r) for r in result.scalars()]

    async def delete(
        self,
        feedback_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        if scope is None:
            return False
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(FeedbackRow).where(
                        FeedbackRow.feedback_id == feedback_id,
                        *self._scope_predicates(scope),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def upsert(
        self,
        *,
        run_id: str,
        thread_id: str,
        rating: int,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
        comment: str | None = None,
    ) -> dict:
        """Create or update feedback for (thread_id, run_id, user_id). rating must be +1 or -1."""
        if rating not in (1, -1):
            raise ValueError(f"rating must be +1 or -1, got {rating}")
        if scope is None:
            raise ValueError("private feedback scope is required")
        async with self._sf() as session:
            project_id, owner_user_id = await self._require_parent_run(
                session,
                scope=scope,
                thread_id=thread_id,
                run_id=run_id,
            )
            stmt = select(FeedbackRow).where(
                FeedbackRow.thread_id == thread_id,
                FeedbackRow.run_id == run_id,
                FeedbackRow.project_id == project_id,
                FeedbackRow.owner_user_id == owner_user_id,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is not None:
                row.rating = rating
                row.comment = comment
                row.created_at = datetime.now(UTC)
            else:
                row = FeedbackRow(
                    feedback_id=str(uuid.uuid4()),
                    run_id=run_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    owner_user_id=owner_user_id,
                    rating=rating,
                    comment=comment,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
            await session.commit()
            await session.refresh(row)
            return self._row_to_dict(row)

    async def delete_by_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        """Delete the current user's feedback for a run. Returns True if a record was deleted."""
        if scope is None:
            return False
        async with self._sf() as session:
            stmt = select(FeedbackRow).where(
                FeedbackRow.thread_id == thread_id,
                FeedbackRow.run_id == run_id,
                *self._scope_predicates(scope),
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def list_by_thread_grouped(
        self,
        thread_id: str,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ) -> dict[str, dict]:
        """Return feedback grouped by run_id for a thread: {run_id: feedback_dict}."""
        if scope is None:
            return {}
        stmt = select(FeedbackRow).where(
            FeedbackRow.thread_id == thread_id,
            *self._scope_predicates(scope),
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            return {row.run_id: self._row_to_dict(row) for row in result.scalars()}

    async def aggregate_by_run(
        self,
        thread_id: str,
        run_id: str,
        *,
        scope: PrivateResourceScope | None = None,
    ) -> dict:
        """Aggregate feedback stats for a run using database-side counting."""
        if scope is None:
            return {"run_id": run_id, "total": 0, "positive": 0, "negative": 0}
        stmt = select(
            func.count().label("total"),
            func.coalesce(func.sum(case((FeedbackRow.rating == 1, 1), else_=0)), 0).label("positive"),
            func.coalesce(func.sum(case((FeedbackRow.rating == -1, 1), else_=0)), 0).label("negative"),
        ).where(
            FeedbackRow.thread_id == thread_id,
            FeedbackRow.run_id == run_id,
            *self._scope_predicates(scope),
        )
        async with self._sf() as session:
            row = (await session.execute(stmt)).one()
            return {
                "run_id": run_id,
                "total": row.total,
                "positive": row.positive,
                "negative": row.negative,
            }
