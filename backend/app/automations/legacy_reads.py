from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from deerflow.utils.time import coerce_iso

_TASK_FIELDS = (
    "id",
    "user_id",
    "thread_id",
    "context_mode",
    "assistant_id",
    "title",
    "prompt",
    "schedule_type",
    "schedule_spec",
    "timezone",
    "status",
    "overlap_policy",
    "next_run_at",
    "last_run_at",
    "last_run_id",
    "last_thread_id",
    "last_error",
    "lease_owner",
    "lease_expires_at",
    "run_count",
    "created_at",
    "updated_at",
)
_TASK_TIMESTAMPS = (
    "created_at",
    "updated_at",
    "next_run_at",
    "last_run_at",
    "lease_expires_at",
)
_RUN_FIELDS = (
    "id",
    "task_id",
    "thread_id",
    "run_id",
    "scheduled_for",
    "trigger",
    "status",
    "error",
    "started_at",
    "finished_at",
    "created_at",
)
_RUN_TIMESTAMPS = (
    "scheduled_for",
    "started_at",
    "finished_at",
    "created_at",
)

_EXPAND_TASK_COLUMNS = """
task.id,task.user_id,task.thread_id,task.context_mode,task.assistant_id,
task.title,task.prompt,task.schedule_type,task.schedule_spec,task.timezone,
task.status,task.overlap_policy,task.next_run_at,task.last_run_at,
task.last_run_id,task.last_thread_id,task.last_error,task.lease_owner,
task.lease_expires_at,task.run_count,task.created_at,task.updated_at
"""
_FINAL_OWNER_SCOPE = """
WITH owner_scope AS (
    SELECT min(project_id::text)::uuid AS project_id
    FROM scheduled_tasks
    WHERE owner_user_id=:user_id
    GROUP BY owner_user_id
    HAVING count(DISTINCT project_id)=1
)
"""
_FINAL_TASK_COLUMNS = """
task.id,task.owner_user_id AS user_id,task.thread_id,task.context_mode,
NULL::varchar(128) AS assistant_id,task.title,task.prompt,task.schedule_type,
task.schedule_spec,task.timezone,task.status,task.overlap_policy,
task.next_run_at,task.last_run_at,NULL::varchar(64) AS last_run_id,
NULL::varchar(64) AS last_thread_id,task.last_error_code AS last_error,
NULL::varchar(128) AS lease_owner,
NULL::timestamptz AS lease_expires_at,task.run_count,task.created_at,
task.updated_at
"""
_EXPAND_RUN_COLUMNS = """
occurrence.id,occurrence.task_id,occurrence.thread_id,occurrence.run_id,
occurrence.scheduled_for,occurrence.trigger,occurrence.status,occurrence.error,
occurrence.started_at,occurrence.finished_at,occurrence.created_at
"""
_FINAL_RUN_COLUMNS = """
occurrence.id,occurrence.task_id,occurrence.thread_id,occurrence.run_id,
occurrence.scheduled_for,occurrence.trigger,occurrence.status,
occurrence.error_code AS error,occurrence.started_at,occurrence.finished_at,
occurrence.created_at
"""


class LegacyAutomationReadAdapter:
    """Authenticated, lifecycle-bound projection for legacy reads only.

    The adapter deliberately has no mutation, claim, lease, or dispatch
    methods. Expand rows are filtered by the retained ``user_id`` column.
    Final-schema rows are filtered by ``owner_user_id`` and are visible only
    while all rows for that owner resolve to one exact project, matching the
    M5 migration's one-owner-to-one-project invariant.
    """

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._session_factory: Callable[[], AsyncSession] | None = session_factory
        self._mode: Literal["expand", "final"] | None = None

    @property
    def closed(self) -> bool:
        return self._session_factory is None

    async def aclose(self) -> None:
        self._session_factory = None
        self._mode = None

    def _factory(self) -> Callable[[], AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("legacy Automation read adapter is closed")
        return self._session_factory

    async def _schema_mode(
        self,
        session: AsyncSession,
    ) -> Literal["expand", "final"]:
        if self._mode is not None:
            return self._mode
        retains_legacy_owner = bool(
            await session.scalar(
                text(
                    """SELECT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema=current_schema()
                          AND table_name='scheduled_tasks'
                          AND column_name='user_id'
                    )"""
                )
            )
        )
        self._mode = "expand" if retains_legacy_owner else "final"
        return self._mode

    @staticmethod
    def _dto(
        row: dict[str, Any],
        *,
        fields: tuple[str, ...],
        timestamps: tuple[str, ...],
    ) -> dict[str, Any]:
        result = {field: row[field] for field in fields}
        for field in timestamps:
            if result[field] is not None:
                result[field] = coerce_iso(result[field])
        return result

    @classmethod
    def _task_dto(cls, row: dict[str, Any]) -> dict[str, Any]:
        return cls._dto(
            row,
            fields=_TASK_FIELDS,
            timestamps=_TASK_TIMESTAMPS,
        )

    @classmethod
    def _run_dto(cls, row: dict[str, Any]) -> dict[str, Any]:
        return cls._dto(
            row,
            fields=_RUN_FIELDS,
            timestamps=_RUN_TIMESTAMPS,
        )

    async def list_by_user(self, user_id: str) -> list[dict[str, Any]]:
        factory = self._factory()
        async with factory() as session:
            mode = await self._schema_mode(session)
            if mode == "expand":
                statement = text(
                    f"""SELECT {_EXPAND_TASK_COLUMNS}
                    FROM scheduled_tasks task
                    WHERE task.user_id=:user_id
                    ORDER BY task.created_at DESC,task.id DESC"""  # noqa: S608 - fixed internal projection
                )
            else:
                statement = text(
                    f"""{_FINAL_OWNER_SCOPE}
                    SELECT {_FINAL_TASK_COLUMNS}
                    FROM scheduled_tasks task
                    JOIN owner_scope scope ON scope.project_id=task.project_id
                    WHERE task.owner_user_id=:user_id
                      AND task.deleted_at IS NULL
                    ORDER BY task.created_at DESC,task.id DESC"""  # noqa: S608 - fixed internal projection
                )
            rows = (await session.execute(statement, {"user_id": user_id})).mappings().all()
            return [self._task_dto(dict(row)) for row in rows]

    async def get(
        self,
        task_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        factory = self._factory()
        async with factory() as session:
            mode = await self._schema_mode(session)
            if mode == "expand":
                statement = text(
                    f"""SELECT {_EXPAND_TASK_COLUMNS}
                    FROM scheduled_tasks task
                    WHERE task.id=:task_id AND task.user_id=:user_id"""  # noqa: S608 - fixed internal projection
                )
            else:
                statement = text(
                    f"""{_FINAL_OWNER_SCOPE}
                    SELECT {_FINAL_TASK_COLUMNS}
                    FROM scheduled_tasks task
                    JOIN owner_scope scope ON scope.project_id=task.project_id
                    WHERE task.id=:task_id
                      AND task.owner_user_id=:user_id
                      AND task.deleted_at IS NULL"""  # noqa: S608 - fixed internal projection
                )
            row = (
                (
                    await session.execute(
                        statement,
                        {"task_id": task_id, "user_id": user_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            return None if row is None else self._task_dto(dict(row))

    async def list_by_user_and_thread(
        self,
        user_id: str,
        thread_id: str,
    ) -> list[dict[str, Any]]:
        factory = self._factory()
        async with factory() as session:
            mode = await self._schema_mode(session)
            if mode == "expand":
                statement = text(
                    f"""SELECT {_EXPAND_TASK_COLUMNS}
                    FROM scheduled_tasks task
                    WHERE task.user_id=:user_id AND task.thread_id=:thread_id
                    ORDER BY task.created_at DESC,task.id DESC"""  # noqa: S608 - fixed internal projection
                )
            else:
                statement = text(
                    f"""{_FINAL_OWNER_SCOPE}
                    SELECT {_FINAL_TASK_COLUMNS}
                    FROM scheduled_tasks task
                    JOIN owner_scope scope ON scope.project_id=task.project_id
                    WHERE task.owner_user_id=:user_id
                      AND task.thread_id=:thread_id
                      AND task.deleted_at IS NULL
                    ORDER BY task.created_at DESC,task.id DESC"""  # noqa: S608 - fixed internal projection
                )
            rows = (
                (
                    await session.execute(
                        statement,
                        {"user_id": user_id, "thread_id": thread_id},
                    )
                )
                .mappings()
                .all()
            )
            return [self._task_dto(dict(row)) for row in rows]

    async def list_by_task(
        self,
        task_id: str,
        *,
        user_id: str,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        factory = self._factory()
        async with factory() as session:
            mode = await self._schema_mode(session)
            if mode == "expand":
                statement = text(
                    f"""SELECT {_EXPAND_RUN_COLUMNS}
                    FROM scheduled_task_runs occurrence
                    JOIN scheduled_tasks task ON task.id=occurrence.task_id
                    WHERE occurrence.task_id=:task_id
                      AND task.user_id=:user_id
                    ORDER BY occurrence.created_at DESC,occurrence.id DESC
                    LIMIT :limit OFFSET :offset"""  # noqa: S608 - fixed internal projection
                )
            else:
                statement = text(
                    f"""{_FINAL_OWNER_SCOPE}
                    SELECT {_FINAL_RUN_COLUMNS}
                    FROM scheduled_task_runs occurrence
                    JOIN scheduled_tasks task
                      ON task.project_id=occurrence.project_id
                     AND task.owner_user_id=occurrence.owner_user_id
                     AND task.id=occurrence.task_id
                    JOIN owner_scope scope ON scope.project_id=task.project_id
                    WHERE occurrence.task_id=:task_id
                      AND task.owner_user_id=:user_id
                      AND task.deleted_at IS NULL
                    ORDER BY occurrence.created_at DESC,occurrence.id DESC
                    LIMIT :limit OFFSET :offset"""  # noqa: S608 - fixed internal projection
                )
            rows = (
                (
                    await session.execute(
                        statement,
                        {
                            "task_id": task_id,
                            "user_id": user_id,
                            "limit": limit,
                            "offset": offset,
                        },
                    )
                )
                .mappings()
                .all()
            )
            return [self._run_dto(dict(row)) for row in rows]


__all__ = ["LegacyAutomationReadAdapter"]
