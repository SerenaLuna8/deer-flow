"""Durable, owner-private activity stream for Agent Builder."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.agent_design_repository import AgentDesignRepository
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from deerflow.persistence.shared_assets import (
    AgentDesignActivityRow,
    AgentDesignOperationRow,
)

MAX_AGENT_DESIGN_ACTIVITY_BYTES_PER_OPERATION = 4 * 1024 * 1024
_TERMINAL_RESERVE_BYTES = 1_024


class AgentDesignActivityKind(StrEnum):
    TURN_ACCEPTED = "turn_accepted"
    ATTEMPT_STARTED = "attempt_started"
    REASONING = "reasoning"
    CANDIDATE_GENERATED = "candidate_generated"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    REPAIR_STARTED = "repair_started"
    TURN_TERMINAL = "turn_terminal"
    COMMIT_ACCEPTED = "commit_accepted"
    COMMIT_VALIDATION_STARTED = "commit_validation_started"
    COMMIT_VALIDATION_PASSED = "commit_validation_passed"
    COMMIT_PERSISTENCE_STARTED = "commit_persistence_started"
    COMMIT_PERSISTENCE_COMPLETED = "commit_persistence_completed"
    COMMIT_TERMINAL = "commit_terminal"


_TERMINAL_KINDS = frozenset(
    {
        AgentDesignActivityKind.TURN_TERMINAL,
        AgentDesignActivityKind.COMMIT_TERMINAL,
    }
)


class AgentDesignActivityLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AgentDesignActivity:
    seq: int
    operation_id: uuid.UUID
    kind: AgentDesignActivityKind
    attempt: int | None
    payload: dict[str, object]
    created_at: datetime


class AgentDesignActivityRepository:
    """Append/read the Builder-only stream without sharing Thread event state."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _require_context(context: ProjectContext) -> None:
        if not isinstance(context, ProjectContext):
            raise AssetForbidden(getattr(context, "request_id", "unknown"))

    async def append(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        operation_id: uuid.UUID,
        kind: AgentDesignActivityKind,
        payload: dict[str, object] | None = None,
        attempt: int | None = None,
    ) -> AgentDesignActivityRow:
        self._require_context(context)
        if type(kind) is not AgentDesignActivityKind:
            raise TypeError("kind must be AgentDesignActivityKind")
        if attempt not in {None, 1, 2}:
            raise TypeError("attempt must be 1, 2, or None")
        public_payload = dict(payload or {})
        if kind is AgentDesignActivityKind.REASONING:
            text = public_payload.get("text")
            if set(public_payload) != {"text"} or not isinstance(text, str) or not text:
                raise TypeError("reasoning payload requires text only")
            public_payload = {"text": text}
        payload_bytes = len(
            json.dumps(
                public_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        # Streaming callbacks run after the HTTP admission transaction. Re-lock
        # the current project membership before every durable side effect.
        await AgentDesignRepository(self.session).lock_context(context)
        operation = (
            await self.session.execute(
                select(AgentDesignOperationRow)
                .where(
                    AgentDesignOperationRow.id == operation_id,
                    AgentDesignOperationRow.project_id == context.project_id,
                    AgentDesignOperationRow.owner_user_id == str(context.user_id),
                    AgentDesignOperationRow.session_id == session_id,
                )
                .with_for_update(of=AgentDesignOperationRow)
            )
        ).scalar_one_or_none()
        if operation is None:
            raise AssetNotFound(context.request_id)
        used_bytes = await self.session.scalar(
            select(
                func.coalesce(
                    func.sum(func.octet_length(AgentDesignActivityRow.payload_json.cast(Text))),
                    0,
                )
            ).where(AgentDesignActivityRow.operation_id == operation_id)
        )
        byte_limit = MAX_AGENT_DESIGN_ACTIVITY_BYTES_PER_OPERATION
        if kind not in _TERMINAL_KINDS:
            byte_limit -= _TERMINAL_RESERVE_BYTES
        if int(used_bytes or 0) + payload_bytes > byte_limit:
            raise AgentDesignActivityLimitExceeded
        row = AgentDesignActivityRow(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            session_id=session_id,
            operation_id=operation_id,
            attempt=attempt,
            kind=kind.value,
            payload_json=public_payload,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_after(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
        after_seq: int,
        limit: int,
    ) -> tuple[AgentDesignActivityRow, ...]:
        self._require_context(context)
        await AgentDesignRepository(self.session).get(
            context,
            session_id,
            for_update=False,
        )
        statement = (
            select(AgentDesignActivityRow)
            .where(
                AgentDesignActivityRow.project_id == context.project_id,
                AgentDesignActivityRow.owner_user_id == str(context.user_id),
                AgentDesignActivityRow.session_id == session_id,
                AgentDesignActivityRow.seq > after_seq,
            )
            .order_by(AgentDesignActivityRow.seq)
            .limit(limit)
        )
        return tuple((await self.session.execute(statement)).scalars())

    async def clear_session(
        self,
        context: ProjectContext,
        *,
        session_id: uuid.UUID,
    ) -> None:
        self._require_context(context)
        await self.session.execute(
            delete(AgentDesignActivityRow).where(
                AgentDesignActivityRow.project_id == context.project_id,
                AgentDesignActivityRow.owner_user_id == str(context.user_id),
                AgentDesignActivityRow.session_id == session_id,
            )
        )


def activity_view(row: AgentDesignActivityRow) -> AgentDesignActivity:
    kind = AgentDesignActivityKind(row.kind)
    payload = dict(row.payload_json)
    if kind is AgentDesignActivityKind.REASONING:
        text = payload.get("text")
        if set(payload) != {"text"} or not isinstance(text, str) or not text:
            raise TypeError("persisted reasoning activity requires text only")
        payload = {"text": text}
    return AgentDesignActivity(
        seq=int(row.seq),
        operation_id=uuid.UUID(str(row.operation_id)),
        kind=kind,
        attempt=row.attempt,
        payload=payload,
        created_at=row.created_at,
    )


__all__ = [
    "AgentDesignActivity",
    "AgentDesignActivityKind",
    "AgentDesignActivityLimitExceeded",
    "AgentDesignActivityRepository",
    "MAX_AGENT_DESIGN_ACTIVITY_BYTES_PER_OPERATION",
    "activity_view",
]
