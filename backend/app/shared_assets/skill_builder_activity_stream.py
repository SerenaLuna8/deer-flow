"""Project Skill Builder stream projection into its isolated Activity log."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.private_work.context import (
    PrivateWorkContext,
    require_issued_private_work_context,
)
from app.projects.capabilities import Capability
from app.projects.context import (
    ProjectContext,
    resolve_project_context_in_transaction,
)
from app.shared_assets.skill_builder_agent_runtime import (
    SKILL_BUILDER_TOOL_NAMES,
)
from app.shared_assets.skill_design_activity import (
    SkillDesignActivityKind,
    SkillDesignActivityLimitExceeded,
    SkillDesignActivityRepository,
    reasoning_text,
)
from app.shared_assets.skill_design_repository import SkillDesignRepository
from deerflow.persistence.jobs.model import JobAttemptRow
from deerflow.persistence.jobs.sql import JobClaim

_SAFE_TOOL_NAMES = frozenset(SKILL_BUILDER_TOOL_NAMES)


class SkillBuilderActivityEmitter:
    """Append public-safe events for one exact Worker Job Attempt."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
        run_id: str,
        attempt: int,
    ) -> None:
        self._session_factory = session_factory
        self._context = context
        self.session_id = session_id
        self.operation_id = operation_id
        self.run_id = run_id
        self.attempt = attempt
        self._terminal_written = False

    @classmethod
    async def create(
        cls,
        session_factory: async_sessionmaker[AsyncSession],
        context: PrivateWorkContext,
        claim: JobClaim,
    ) -> SkillBuilderActivityEmitter:
        context = require_issued_private_work_context(context)
        if claim.run_id is None:
            raise ValueError("Skill Builder Job requires a Run")
        async with session_factory() as session, session.begin():
            current = await resolve_project_context_in_transaction(
                session,
                context.user_id,
                context.project_id,
                context.request_id,
            )
            current.require(Capability.SHARED_ASSETS_READ)
            current.require(Capability.SHARED_ASSETS_EDIT)
            operation = await SkillDesignRepository(session).operation_by_run(
                current,
                claim.run_id,
                for_update=False,
            )
            attempt = await session.scalar(
                select(JobAttemptRow.attempt_number).where(
                    JobAttemptRow.id == claim.attempt_id,
                    JobAttemptRow.job_id == claim.job_id,
                )
            )
        if operation is None or attempt is None:
            raise ValueError("Skill Builder operation attempt is missing")
        emitter = cls(
            session_factory,
            current,
            session_id=operation.session_id,
            operation_id=operation.id,
            run_id=claim.run_id,
            attempt=int(attempt),
        )
        await emitter.append(
            SkillDesignActivityKind.ATTEMPT_STARTED,
            source_event_id=f"attempt:{claim.attempt_id}",
        )
        if int(attempt) > 1:
            await emitter.append(
                SkillDesignActivityKind.REPAIR_STARTED,
                source_event_id=f"repair:{claim.attempt_id}",
            )
        return emitter

    async def append(
        self,
        kind: SkillDesignActivityKind,
        *,
        payload: dict[str, object] | None = None,
        source_event_id: str | None = None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await SkillDesignActivityRepository(session).append(
                self._context,
                session_id=self.session_id,
                operation_id=self.operation_id,
                run_id=self.run_id,
                attempt=self.attempt,
                kind=kind,
                payload=payload,
                source_event_id=source_event_id,
            )

    async def terminal(
        self,
        *,
        status: str,
        code: str | None = None,
    ) -> None:
        if self._terminal_written:
            return
        payload: dict[str, object] = {"status": status}
        if code:
            payload["code"] = code
        await self.append(
            SkillDesignActivityKind.RUN_TERMINAL,
            payload=payload,
            source_event_id="run-terminal",
        )
        self._terminal_written = True


def _record(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _message(data: Any) -> Mapping[str, Any] | None:
    if not isinstance(data, list) or not data:
        return None
    return _record(data[0])


def _message_type(message: Mapping[str, Any]) -> str:
    value = message.get("type")
    return value.lower() if isinstance(value, str) else ""


def _safe_tool_call(call: Any) -> tuple[str, str] | None:
    if not isinstance(call, Mapping):
        return None
    call_id = call.get("id")
    name = call.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or name not in _SAFE_TOOL_NAMES:
        return None
    return call_id, name


class SkillBuilderActivityStreamBridge:
    """Keep raw Run streaming intact while projecting only safe Builder data."""

    def __init__(self, bridge: Any, emitter: SkillBuilderActivityEmitter) -> None:
        self._bridge = bridge
        self._emitter = emitter
        self._tool_names: dict[str, str] = {}

    @property
    def supports_cross_process(self) -> bool:
        return bool(getattr(self._bridge, "supports_cross_process", False))

    async def publish(self, run_id: str, event: str, data: Any) -> None:
        await self._bridge.publish(run_id, event, data)
        if event != "messages":
            return
        message = _message(data)
        if message is None:
            return
        kind = _message_type(message)
        if kind in {"ai", "aimessage", "aimessagechunk"}:
            reasoning = reasoning_text(dict(message))
            if reasoning:
                await self._emitter.append(
                    SkillDesignActivityKind.REASONING,
                    payload={"text": reasoning},
                )
            calls = message.get("tool_calls")
            if isinstance(calls, list):
                for raw_call in calls:
                    call = _safe_tool_call(raw_call)
                    if call is None:
                        continue
                    call_id, tool_name = call
                    self._tool_names[call_id] = tool_name
                    await self._emitter.append(
                        SkillDesignActivityKind.TOOL_STARTED,
                        payload={
                            "tool_call_id": call_id,
                            "tool_name": tool_name,
                        },
                        source_event_id=f"tool-started:{call_id}",
                    )
            return
        if kind not in {"tool", "toolmessage", "toolmessagechunk"}:
            return
        call_id = message.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        raw_name = message.get("name")
        tool_name = raw_name if isinstance(raw_name, str) and raw_name in _SAFE_TOOL_NAMES else self._tool_names.get(call_id)
        if tool_name is None:
            return
        failed = message.get("status") in {"error", "failed"}
        await self._emitter.append(
            (SkillDesignActivityKind.TOOL_FAILED if failed else SkillDesignActivityKind.TOOL_COMPLETED),
            payload={"tool_call_id": call_id, "tool_name": tool_name},
            source_event_id=f"tool-{'failed' if failed else 'completed'}:{call_id}",
        )

    async def publish_end(self, run_id: str) -> None:
        await self._bridge.publish_end(run_id)

    def subscribe(self, *args: Any, **kwargs: Any):
        return self._bridge.subscribe(*args, **kwargs)

    async def cleanup(self, *args: Any, **kwargs: Any) -> None:
        await self._bridge.cleanup(*args, **kwargs)


__all__ = [
    "SkillBuilderActivityEmitter",
    "SkillBuilderActivityStreamBridge",
    "SkillDesignActivityLimitExceeded",
]
