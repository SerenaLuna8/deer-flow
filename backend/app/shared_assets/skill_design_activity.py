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

_TOOL_DETAIL_FIELDS = {
    "search_available_skills": frozenset({"result_count"}),
    "read_skill_version": frozenset({"resource_name"}),
    "search_available_mcp_tools": frozenset({"result_count"}),
    "inspect_mcp_tool": frozenset({"resource_name"}),
    "list_candidate_files": frozenset({"result_count"}),
    "read_candidate_file": frozenset({"path", "size_bytes"}),
    "upsert_candidate_file": frozenset({"path", "size_bytes"}),
    "delete_candidate_file": frozenset({"path", "size_bytes"}),
    "request_skill_clarification": frozenset(),
    "finalize_skill_candidate": frozenset(),
}


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
    if kind is SkillDesignActivityKind.VALIDATION_STARTED:
        if not value:
            # Upgrade-created history used an empty payload for this stage.
            return {}
        stage = value.get("stage")
        if set(value) == {"stage"} and stage == "safety_scan":
            # Older sessions may contain this retired stage. Keep replay valid
            # without exposing it as a current validation step.
            return {}
        if set(value) != {"stage"} or stage != "package_files":
            raise TypeError("validation stage must be public and known")
        return {"stage": stage}
    if kind is SkillDesignActivityKind.REASONING:
        text = value.get("text")
        if set(value) != {"text"} or not isinstance(text, str) or not text:
            raise TypeError("reasoning payload requires text only")
        return {"text": text}
    if kind in {
        SkillDesignActivityKind.TOOL_STARTED,
        SkillDesignActivityKind.TOOL_COMPLETED,
        SkillDesignActivityKind.TOOL_FAILED,
    }:
        call_id = value.get("tool_call_id")
        tool_name = value.get("tool_name")
        allowed_details = _TOOL_DETAIL_FIELDS.get(tool_name) if isinstance(tool_name, str) else None
        if allowed_details is None or set(value) - {"tool_call_id", "tool_name"} - allowed_details or not isinstance(call_id, str) or not call_id or len(call_id) > 512:
            raise TypeError("tool activity requires safe identity fields")
        result: dict[str, object] = {
            "tool_call_id": call_id,
            "tool_name": tool_name,
        }
        result_count = value.get("result_count")
        if "result_count" in value:
            if not isinstance(result_count, int) or isinstance(result_count, bool) or result_count < 0 or result_count > 128:
                raise TypeError("tool result count must be bounded")
            result["result_count"] = result_count
        resource_name = value.get("resource_name")
        if "resource_name" in value:
            if not isinstance(resource_name, str) or not resource_name or len(resource_name) > 512 or any(ord(char) < 32 for char in resource_name):
                raise TypeError("tool resource name must be public-safe")
            result["resource_name"] = resource_name
        path = value.get("path")
        if "path" in value:
            if not isinstance(path, str) or not path or len(path) > 1_024 or path.startswith("/") or ".." in path.split("/") or any(ord(char) < 32 for char in path):
                raise TypeError("tool path must be candidate-relative")
            result["path"] = path
        size_bytes = value.get("size_bytes")
        if "size_bytes" in value:
            if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0 or size_bytes > 2 * 1024 * 1024:
                raise TypeError("tool byte count must be bounded")
            result["size_bytes"] = size_bytes
        return result
    if kind in _TERMINAL_KINDS:
        status = value.get("status")
        if set(value) - {"status", "code"} or status not in {
            "completed",
            "failed",
            "stopped",
        }:
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

    async def append_locked_settlement_terminal(
        self,
        operation: SkillDesignOperationRow,
        *,
        status: str,
        code: str | None,
    ) -> SkillDesignActivityRow:
        """Append the one safe terminal for a settlement-locked Builder turn."""

        if not isinstance(operation, SkillDesignOperationRow) or operation.operation_kind != "turn" or operation.run_id is None:
            raise TypeError("settlement terminal requires a locked Builder turn")
        payload_input: dict[str, object] = {"status": status}
        if code is not None:
            payload_input["code"] = code
        public_payload = _public_payload(
            SkillDesignActivityKind.RUN_TERMINAL,
            payload_input,
        )
        existing = (
            await self.session.execute(
                select(SkillDesignActivityRow).where(
                    SkillDesignActivityRow.operation_id == operation.id,
                    SkillDesignActivityRow.kind == SkillDesignActivityKind.RUN_TERMINAL.value,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.payload_json != public_payload:
                raise TypeError("Builder turn already has a different terminal")
            return existing
        attempt = await self.session.scalar(
            select(func.max(SkillDesignActivityRow.attempt)).where(
                SkillDesignActivityRow.operation_id == operation.id,
            )
        )
        row = SkillDesignActivityRow(
            project_id=operation.project_id,
            owner_user_id=operation.owner_user_id,
            session_id=operation.session_id,
            operation_id=operation.id,
            run_id=operation.run_id,
            attempt=int(attempt) if attempt is not None else None,
            source_event_id="run-terminal",
            kind=SkillDesignActivityKind.RUN_TERMINAL.value,
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
    kind = SkillDesignActivityKind(row.kind)
    payload = _public_payload(kind, dict(row.payload_json))
    return SkillDesignActivity(
        seq=int(row.seq),
        operation_id=uuid.UUID(str(row.operation_id)),
        run_id=row.run_id,
        kind=kind,
        attempt=row.attempt,
        payload=payload,
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
