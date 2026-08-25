"""SQLAlchemy-backed RunStore implementation.

Each method acquires and releases its own short-lived session.
Run status updates happen from background workers that may live
minutes -- we don't hold connections across long execution.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from deerflow.persistence.run.model import RunRow
from deerflow.runtime.private_scope import PrivateResourceScope
from deerflow.runtime.runs.store.base import RunStore
from deerflow.runtime.user_context import AUTO, _AutoSentinel
from deerflow.utils.time import coerce_iso


class RunRepository(RunStore):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @staticmethod
    def _coordinates(scope: PrivateResourceScope) -> tuple[uuid.UUID, str]:
        if type(scope) is not PrivateResourceScope:
            raise ValueError("private run scope is required")
        try:
            return uuid.UUID(scope.project_id), str(uuid.UUID(scope.owner_user_id))
        except (TypeError, ValueError):
            raise ValueError("private run scope is invalid") from None

    @classmethod
    def _scope_predicates(cls, scope: PrivateResourceScope):
        project_id, owner_user_id = cls._coordinates(scope)
        return (
            RunRow.project_id == project_id,
            RunRow.owner_user_id == owner_user_id,
        )

    @staticmethod
    def _normalize_model_name(model_name: str | None) -> str | None:
        """Normalize model_name for storage: strip whitespace, truncate to 128 chars."""
        if model_name is None:
            return None
        if not isinstance(model_name, str):
            model_name = str(model_name)
        normalized = model_name.strip()
        if len(normalized) > 128:
            normalized = normalized[:128]
        return normalized

    @staticmethod
    def _safe_json(obj: Any) -> Any:
        """Ensure obj is JSON-serializable. Falls back to model_dump() or str()."""
        if obj is None:
            return None
        if isinstance(obj, (str, int, float, bool)):
            return obj
        if isinstance(obj, dict):
            return {k: RunRepository._safe_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [RunRepository._safe_json(v) for v in obj]
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump()
            except Exception:
                pass
        if hasattr(obj, "dict"):
            try:
                return obj.dict()
            except Exception:
                pass
        try:
            json.dumps(obj)
            return obj
        except (TypeError, ValueError):
            return str(obj)

    @staticmethod
    def _row_to_dict(
        row: RunRow,
        *,
        scope: PrivateResourceScope | None = None,
    ) -> dict[str, Any]:
        d = row.to_dict()
        # Remap JSON columns to match RunStore interface
        d["metadata"] = d.pop("metadata_json", {})
        d["kwargs"] = d.pop("kwargs_json", {})
        # Convert datetime to the RunStore API's ISO representation;
        # ``coerce_iso`` also normalizes legacy naive timestamps as UTC.
        for key in ("created_at", "updated_at"):
            val = d.get(key)
            if isinstance(val, datetime):
                d[key] = coerce_iso(val)
        d["scope"] = scope or PrivateResourceScope(
            project_id=str(row.project_id),
            owner_user_id=row.owner_user_id,
            membership_version=0,
        )
        return d

    async def put(
        self,
        run_id,
        *,
        thread_id,
        assistant_id=None,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
        model_name: str | None = None,
        status="pending",
        multitask_strategy="reject",
        metadata=None,
        kwargs=None,
        error=None,
        created_at=None,
        follow_up_to_run_id=None,
    ):
        """Insert or update a run row.

        ``RunManager`` may retry ``put`` after transient persistence failures.
        Making this operation idempotent prevents a successful-but-unacknowledged first
        commit from turning the retry into a primary-key failure.
        """
        if scope is None:
            raise ValueError("private run scope is required")
        project_id, owner_user_id = self._coordinates(scope)
        now = datetime.now(UTC)
        values = {
            "thread_id": thread_id,
            "assistant_id": assistant_id,
            "owner_user_id": owner_user_id,
            "project_id": project_id,
            "model_name": self._normalize_model_name(model_name),
            "status": status,
            "multitask_strategy": multitask_strategy,
            "metadata_json": self._safe_json(metadata) or {},
            "kwargs_json": self._safe_json(kwargs) or {},
            "error": error,
            "follow_up_to_run_id": follow_up_to_run_id,
            "updated_at": now,
        }
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(RunRow).where(
                        RunRow.run_id == run_id,
                        *self._scope_predicates(scope),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ValueError(
                    "new executable Runs require snapshot admission",
                )
            if row.asset_closure_sealed is not True or row.job_id is None:
                raise ValueError(
                    "executable Run requires sealed snapshot admission",
                )
            for key, value in values.items():
                setattr(row, key, value)
            await session.commit()

    async def get(
        self,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return None
        self._coordinates(scope)
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(RunRow).where(
                        RunRow.run_id == run_id,
                        *self._scope_predicates(scope),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._row_to_dict(row, scope=scope)

    async def list_by_thread(
        self,
        thread_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
        limit=100,
    ):
        if scope is None:
            return []
        self._coordinates(scope)
        stmt = select(RunRow).where(
            RunRow.thread_id == thread_id,
            *self._scope_predicates(scope),
        )
        stmt = stmt.order_by(RunRow.created_at.desc()).limit(limit)
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r, scope=scope) for r in result.scalars()]

    async def update_status(self, run_id, status, *, error=None, scope=None) -> bool:
        outcome = await self.update_status_authoritative(
            run_id,
            status,
            error=error,
            scope=scope,
        )
        return outcome is not False

    async def update_status_authoritative(
        self,
        run_id,
        status,
        *,
        error=None,
        scope=None,
    ) -> dict[str, Any] | bool:
        """Atomically return the CASE-resolved status written by PostgreSQL."""

        if scope is None:
            return False
        revoked = RunRow.authorization_cancel_requested_at.is_not(None)
        values: dict[str, Any] = {
            "status": case((revoked, "interrupted"), else_=status),
            "updated_at": datetime.now(UTC),
        }
        if error is not None:
            values["error"] = case((revoked, "authorization_revoked"), else_=error)
        else:
            values["error"] = case((revoked, "authorization_revoked"), else_=RunRow.error)
        async with self._sf() as session:
            result = await session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == run_id,
                    *self._scope_predicates(scope),
                )
                .values(**values)
                .returning(RunRow.status, RunRow.error)
            )
            row = result.one_or_none()
            await session.commit()
            if row is None:
                return False
            return {"status": row.status, "error": row.error}

    async def update_model_name(self, run_id, model_name, *, scope=None):
        if scope is None:
            return
        async with self._sf() as session:
            await session.execute(update(RunRow).where(RunRow.run_id == run_id, *self._scope_predicates(scope)).values(model_name=self._normalize_model_name(model_name), updated_at=datetime.now(UTC)))
            await session.commit()

    async def delete(
        self,
        run_id,
        *,
        user_id: str | None | _AutoSentinel = AUTO,
        scope: PrivateResourceScope | None = None,
    ):
        if scope is None:
            return
        self._coordinates(scope)
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(RunRow).where(
                        RunRow.run_id == run_id,
                        *self._scope_predicates(scope),
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return
            await session.delete(row)
            await session.commit()

    async def list_pending(self, *, before=None, scope=None):
        if scope is None:
            return []
        if before is None:
            before_dt = datetime.now(UTC)
        elif isinstance(before, datetime):
            before_dt = before
        else:
            before_dt = datetime.fromisoformat(before)
        stmt = (
            select(RunRow)
            .where(
                RunRow.status == "pending",
                RunRow.created_at <= before_dt,
                *self._scope_predicates(scope),
            )
            .order_by(RunRow.created_at.asc())
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r, scope=scope) for r in result.scalars()]

    async def list_inflight(self, *, before=None, scope=None):
        """Return persisted active runs for startup recovery."""
        if scope is None:
            return []
        if before is None:
            before_dt = datetime.now(UTC)
        elif isinstance(before, datetime):
            before_dt = before
        else:
            before_dt = datetime.fromisoformat(before)
        stmt = (
            select(RunRow)
            .where(
                RunRow.status.in_(("pending", "running")),
                RunRow.created_at <= before_dt,
                *self._scope_predicates(scope),
            )
            .order_by(RunRow.created_at.asc())
        )
        async with self._sf() as session:
            result = await session.execute(stmt)
            return [self._row_to_dict(r, scope=scope) for r in result.scalars()]

    async def list_inflight_trusted_unscoped(self, *, before=None):
        """Trusted startup recovery scan with no product-facing scope parameter."""
        if before is None:
            before_dt = datetime.now(UTC)
        elif isinstance(before, datetime):
            before_dt = before
        else:
            before_dt = datetime.fromisoformat(before)
        statement = (
            select(RunRow)
            .where(
                RunRow.status.in_(("pending", "running")),
                RunRow.created_at <= before_dt,
            )
            .order_by(RunRow.created_at.asc())
        )
        async with self._sf() as session:
            rows = (await session.execute(statement)).scalars()
            return [self._row_to_dict(row) for row in rows]

    async def update_run_completion(
        self,
        run_id: str,
        *,
        status: str,
        total_input_tokens: int = 0,
        total_output_tokens: int = 0,
        total_tokens: int = 0,
        llm_call_count: int = 0,
        lead_agent_tokens: int = 0,
        subagent_tokens: int = 0,
        middleware_tokens: int = 0,
        token_usage_by_model: dict[str, dict[str, int]] | None = None,
        message_count: int = 0,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        error: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> bool:
        """Update status + token usage + convenience fields on run completion.

        Returns ``False`` when no run row matched the requested ``run_id``.
        """
        if scope is None:
            return False
        revoked = RunRow.authorization_cancel_requested_at.is_not(None)
        values: dict[str, Any] = {
            "status": case((revoked, "interrupted"), else_=status),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "llm_call_count": llm_call_count,
            "lead_agent_tokens": lead_agent_tokens,
            "subagent_tokens": subagent_tokens,
            "middleware_tokens": middleware_tokens,
            "token_usage_by_model": self._safe_json(token_usage_by_model) or {},
            "message_count": message_count,
            "updated_at": datetime.now(UTC),
        }
        if last_ai_message is not None:
            values["last_ai_message"] = last_ai_message[:2000]
        if first_human_message is not None:
            values["first_human_message"] = first_human_message[:2000]
        if error is not None:
            values["error"] = case((revoked, "authorization_revoked"), else_=error)
        else:
            values["error"] = case((revoked, "authorization_revoked"), else_=RunRow.error)
        async with self._sf() as session:
            result = await session.execute(update(RunRow).where(RunRow.run_id == run_id, *self._scope_predicates(scope)).values(**values))
            await session.commit()
            return result.rowcount != 0

    async def update_run_progress(
        self,
        run_id: str,
        *,
        total_input_tokens: int | None = None,
        total_output_tokens: int | None = None,
        total_tokens: int | None = None,
        llm_call_count: int | None = None,
        lead_agent_tokens: int | None = None,
        subagent_tokens: int | None = None,
        middleware_tokens: int | None = None,
        token_usage_by_model: dict[str, dict[str, int]] | None = None,
        message_count: int | None = None,
        last_ai_message: str | None = None,
        first_human_message: str | None = None,
        scope: PrivateResourceScope | None = None,
    ) -> None:
        """Update token usage + convenience fields while a run is still active."""
        if scope is None:
            return
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        optional_counters = {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "llm_call_count": llm_call_count,
            "lead_agent_tokens": lead_agent_tokens,
            "subagent_tokens": subagent_tokens,
            "middleware_tokens": middleware_tokens,
            "message_count": message_count,
        }
        for key, value in optional_counters.items():
            if value is not None:
                values[key] = value
        if token_usage_by_model is not None:
            values["token_usage_by_model"] = self._safe_json(token_usage_by_model) or {}
        if last_ai_message is not None:
            values["last_ai_message"] = last_ai_message[:2000]
        if first_human_message is not None:
            values["first_human_message"] = first_human_message[:2000]
        async with self._sf() as session:
            await session.execute(
                update(RunRow)
                .where(
                    RunRow.run_id == run_id,
                    RunRow.status == "running",
                    *self._scope_predicates(scope),
                )
                .values(**values)
            )
            await session.commit()

    async def aggregate_tokens_by_thread(
        self,
        thread_id: str,
        *,
        include_active: bool = False,
        included_run_ids: Collection[str] | None = None,
        scope=None,
    ) -> dict[str, Any]:
        """Aggregate token usage for a thread.

        ``by_model`` is reduced in Python from each row's ``token_usage_by_model``
        JSON column so subagent / middleware tokens land on the model that
        actually produced them (issue #3645). Rows written before that column
        existed fall back to ``RunRow.model_name`` + ``RunRow.total_tokens``,
        preserving the legacy lead-only behavior instead of dropping the data.

        Headline totals (``total_tokens``, ``total_input_tokens``,
        ``total_output_tokens``) and the ``by_caller`` bucket are summed from
        their own columns and are therefore unaffected by the JSON column being
        empty.
        """
        if scope is None:
            raise ValueError("private run scope is required")
        terminal_statuses = ("success", "error", "timeout", "interrupted")
        statuses = (*terminal_statuses, "running") if include_active else terminal_statuses
        _completed = RunRow.status.in_(statuses)
        _thread = RunRow.thread_id == thread_id

        stmt = select(
            RunRow.model_name,
            RunRow.total_tokens,
            RunRow.total_input_tokens,
            RunRow.total_output_tokens,
            RunRow.lead_agent_tokens,
            RunRow.subagent_tokens,
            RunRow.middleware_tokens,
            RunRow.token_usage_by_model,
        ).where(_thread, _completed, *self._scope_predicates(scope))
        if included_run_ids is not None:
            selected = {run_id for run_id in included_run_ids if isinstance(run_id, str) and run_id}
            stmt = stmt.where(RunRow.run_id.in_(selected))

        async with self._sf() as session:
            rows = (await session.execute(stmt)).all()

        total_tokens = total_input = total_output = total_runs = 0
        lead_agent = subagent = middleware = 0
        by_model: dict[str, dict] = {}
        for r in rows:
            total_runs += 1
            total_tokens += r.total_tokens
            total_input += r.total_input_tokens
            total_output += r.total_output_tokens
            lead_agent += r.lead_agent_tokens
            subagent += r.subagent_tokens
            middleware += r.middleware_tokens

            # ``or {}`` covers rows written before ``token_usage_by_model``
            # existed (the column is NULL on a manual ALTER ADD COLUMN without
            # backfill); fresh rows always carry the journal-produced dict.
            usage_by_model = r.token_usage_by_model or {}
            if usage_by_model:
                for model, usage in usage_by_model.items():
                    entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                    entry["tokens"] += usage.get("total_tokens", 0)
                    entry["runs"] += 1
            else:
                model = r.model_name or "unknown"
                entry = by_model.setdefault(model, {"tokens": 0, "runs": 0})
                entry["tokens"] += r.total_tokens
                entry["runs"] += 1

        return {
            "total_tokens": total_tokens,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_runs": total_runs,
            "by_model": by_model,
            "by_caller": {
                "lead_agent": lead_agent,
                "subagent": subagent,
                "middleware": middleware,
            },
        }
