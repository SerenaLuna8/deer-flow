"""Durable, owner-private activity stream for Skill Builder."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Text, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetForbidden, AssetNotFound
from app.shared_assets.skill_design_repository import SkillDesignRepository
from deerflow.persistence.shared_assets import (
    SkillDesignActivityRow,
    SkillDesignOperationRow,
)

MAX_SKILL_DESIGN_ACTIVITY_BYTES_PER_OPERATION = 4 * 1024 * 1024
_TERMINAL_RESERVE_BYTES = 1_024


class SkillDesignActivityKind(StrEnum):
    REQUEST_ACCEPTED = "request_accepted"
    ATTEMPT_STARTED = "attempt_started"
    REASONING = "reasoning"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    CANDIDATE_GENERATED = "candidate_generated"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_PASSED = "validation_passed"
    VALIDATION_FAILED = "validation_failed"
    REPAIR_STARTED = "repair_started"
    RUN_TERMINAL = "run_terminal"
    COMMIT_ACCEPTED = "commit_accepted"
    COMMIT_VALIDATION_STARTED = "commit_validation_started"
    COMMIT_VALIDATION_PASSED = "commit_validation_passed"
    COMMIT_PERSISTENCE_STARTED = "commit_persistence_started"
    COMMIT_PERSISTENCE_COMPLETED = "commit_persistence_completed"
    COMMIT_TERMINAL = "commit_terminal"


_TERMINAL_KINDS = frozenset(
    {
        SkillDesignActivityKind.RUN_TERMINAL,
        SkillDesignActivityKind.COMMIT_TERMINAL,
    }
)


class SkillDesignActivityLimitExceeded(RuntimeError):
    """Raised before a non-terminal append exceeds the operation cap."""


@dataclass(frozen=True, slots=True)
class SkillDesignActivity:
    seq: int
    operation_id: uuid.UUID
    run_id: str | None
    kind: SkillDesignActivityKind
    attempt: int | None
    payload: dict[str, object]
    created_at: datetime


def _public_payload(
    kind: SkillDesignActivityKind,
    payload: dict[str, object] | None,
) -> dict[str, object]:
    value = dict(payload or {})
    if kind is SkillDesignActivityKind.REASONING:
        text = value.get("text")
        if not isinstance(text, str) or not text or len(text) > 65_536:
            raise TypeError("reasoning payload requires bounded text")
        return {"text": text}
    if kind in {
        SkillDesignActivityKind.TOOL_STARTED,
        SkillDesignActivityKind.TOOL_COMPLETED,
        SkillDesignActivityKind.TOOL_FAILED,
    }:
        call_id = value.get("tool_call_id")
        tool_name = value.get("tool_name")
        if not isinstance(call_id, str) or not call_id or len(call_id) > 512 or not isinstance(tool_name, str) or not tool_name or len(tool_name) > 255:
            raise TypeError("tool activity requires safe identity fields")
        return {"tool_call_id": call_id, "tool_name": tool_name}
    if kind in _TERMINAL_KINDS:
        status = value.get("status")
        if status not in {"completed", "failed", "stopped"}:
            raise TypeError("terminal activity requires a public status")
        code = value.get("code")
        result: dict[str, object] = {"status": status}
        if code is not None:
            if not isinstance(code, str) or not code or len(code) > 64:
                raise TypeError("terminal code must be bounded")
            result["code"] = code
        return result
    if value:
        raise TypeError("stage activity payload must be empty")
    return {}


class SkillDesignActivityRepository:
    """Append/read the Builder-only stream without sharing Run event state."""

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
        kind: SkillDesignActivityKind,
        payload: dict[str, object] | None = None,
        run_id: str | None = None,
        attempt: int | None = None,
        source_event_id: str | None = None,
    ) -> SkillDesignActivityRow:
        self._require_context(context)
        if type(kind) is not SkillDesignActivityKind:
            raise TypeError("kind must be SkillDesignActivityKind")
        if attempt is not None and (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1):
            raise TypeError("attempt must be a positive integer or None")
        if run_id is not None and (not isinstance(run_id, str) or not run_id or len(run_id) > 64):
            raise TypeError("run_id must be bounded")
        if source_event_id is not None and (not isinstance(source_event_id, str) or not source_event_id or len(source_event_id) > 255):
            raise TypeError("source_event_id must be bounded")
        public_payload = _public_payload(kind, payload)
        payload_bytes = len(
            json.dumps(
                public_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        await SkillDesignRepository(self.session).lock_context(context)
        operation = (
            await self.session.execute(
                select(SkillDesignOperationRow)
                .where(
                    SkillDesignOperationRow.id == operation_id,
                    SkillDesignOperationRow.project_id == context.project_id,
                    SkillDesignOperationRow.owner_user_id == str(context.user_id),
                    SkillDesignOperationRow.session_id == session_id,
                )
                .with_for_update(of=SkillDesignOperationRow)
            )
        ).scalar_one_or_none()
        if operation is None:
            raise AssetNotFound(context.request_id)
        if run_id is not None and operation.run_id != run_id:
            raise AssetNotFound(context.request_id)
        if source_event_id is not None:
            existing = (
                await self.session.execute(
                    select(SkillDesignActivityRow).where(
                        SkillDesignActivityRow.operation_id == operation_id,
                        SkillDesignActivityRow.source_event_id == source_event_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.kind != kind.value or existing.payload_json != public_payload or existing.run_id != run_id or existing.attempt != attempt:
                    raise TypeError("source event identity was reused")
                return existing
        used_bytes = await self.session.scalar(
            select(
                func.coalesce(
                    func.sum(func.octet_length(SkillDesignActivityRow.payload_json.cast(Text))),
                    0,
                )
            ).where(SkillDesignActivityRow.operation_id == operation_id)
        )
        byte_limit = MAX_SKILL_DESIGN_ACTIVITY_BYTES_PER_OPERATION
        if kind not in _TERMINAL_KINDS:
            byte_limit -= _TERMINAL_RESERVE_BYTES
        if int(used_bytes or 0) + payload_bytes > byte_limit:
            raise SkillDesignActivityLimitExceeded
        row = SkillDesignActivityRow(
            project_id=context.project_id,
            owner_user_id=str(context.user_id),
            session_id=session_id,
            operation_id=operation_id,
            run_id=run_id,
            attempt=attempt,
            source_event_id=source_event_id,
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
    ) -> tuple[SkillDesignActivityRow, ...]:
        self._require_context(context)
        await SkillDesignRepository(self.session).get(
            context,
            session_id,
            for_update=False,
        )
        statement = (
            select(SkillDesignActivityRow)
            .where(
                SkillDesignActivityRow.project_id == context.project_id,
                SkillDesignActivityRow.owner_user_id == str(context.user_id),
                SkillDesignActivityRow.session_id == session_id,
                SkillDesignActivityRow.seq > after_seq,
            )
            .order_by(SkillDesignActivityRow.seq)
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
            delete(SkillDesignActivityRow).where(
                SkillDesignActivityRow.project_id == context.project_id,
                SkillDesignActivityRow.owner_user_id == str(context.user_id),
                SkillDesignActivityRow.session_id == session_id,
            )
        )


def activity_view(row: SkillDesignActivityRow) -> SkillDesignActivity:
    return SkillDesignActivity(
        seq=int(row.seq),
        operation_id=uuid.UUID(str(row.operation_id)),
        run_id=row.run_id,
        kind=SkillDesignActivityKind(row.kind),
        attempt=row.attempt,
        payload=dict(row.payload_json),
        created_at=row.created_at,
    )


def reasoning_text(value: Any) -> str:
    """Extract only provider-supplied reasoning text from one stream message."""

    if not isinstance(value, dict):
        return ""
    additional = value.get("additional_kwargs")
    if isinstance(additional, dict):
        selected = additional.get("reasoning_content")
        if isinstance(selected, str) and selected:
            return selected
    content = value.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") not in {
            "thinking",
            "reasoning",
        }:
            continue
        for key in ("thinking", "reasoning", "text", "content"):
            selected = block.get(key)
            if isinstance(selected, str) and selected:
                parts.append(selected)
                break
    return "".join(parts)


__all__ = [
    "MAX_SKILL_DESIGN_ACTIVITY_BYTES_PER_OPERATION",
    "SkillDesignActivity",
    "SkillDesignActivityKind",
    "SkillDesignActivityLimitExceeded",
    "SkillDesignActivityRepository",
    "activity_view",
    "reasoning_text",
]
