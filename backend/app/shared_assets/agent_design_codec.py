"""Pure projections and serialization for Project Agent Builder designs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ValidationError

from app.projects.context import ProjectContext
from app.shared_assets.agent_design_contracts import (
    AgentDesignBlueprint,
    AgentDesignClarificationOption,
    AgentDesignClarificationRequest,
    AgentDesignMessage,
    AgentDesignProgressItem,
    AgentDesignProgressStatus,
    AgentDesignSessionSummary,
    AgentDesignSessionView,
    AgentDesignStatus,
)
from app.shared_assets.agent_design_generation import (
    AgentDesignConflict,
    AgentDesignInterviewAnswer,
    ClarificationQuestion,
)
from app.shared_assets.errors import (
    AgentDesignGenerationProfileStale,
    AssetValidationFailed,
)
from app.shared_assets.models import (
    AgentModelSettings,
    AgentPayload,
    AssetScope,
    SkillAssetRef,
)
from app.system_settings.model_refs import (
    DEFAULT_MODEL_REF as DEFAULT_AGENT_MODEL_REF,
)
from app.system_settings.model_refs import exact_model_ref
from deerflow.persistence.shared_assets import AgentDesignSessionRow

_CLARIFICATION_SET_KIND = "agent_design_clarification_set"


def blueprint_checksum(blueprint: AgentDesignBlueprint) -> str:
    if not isinstance(blueprint, AgentDesignBlueprint):
        raise TypeError("blueprint must be AgentDesignBlueprint")
    canonical = json.dumps(
        _blueprint_json(blueprint),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _agent_payload(blueprint: AgentDesignBlueprint) -> AgentPayload:
    return AgentPayload(
        description=blueprint.description,
        model_ref=blueprint.model_ref,
        tool_groups=blueprint.tool_groups,
        skill_refs=blueprint.skill_refs,
        mcp_version_ids=blueprint.mcp_version_ids,
        agents_instructions=blueprint.agents_instructions,
        soul=blueprint.soul,
        identity=blueprint.identity,
        user_context=blueprint.user_context,
        payload_schema_version=4,
        model_settings=blueprint.model_settings,
    )


def _encode_session_cursor(
    created_at: datetime,
    session_id: uuid.UUID,
) -> str:
    payload = json.dumps(
        {
            "created_at": created_at.isoformat(),
            "id": str(session_id),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _decode_session_cursor(
    context: ProjectContext,
    cursor: str | None,
) -> tuple[datetime | None, uuid.UUID | None]:
    if cursor is None:
        return None, None
    if not isinstance(cursor, str) or not 1 <= len(cursor) <= 256:
        raise AssetValidationFailed(context.request_id)
    try:
        padding = b"=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            cursor.encode("ascii") + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        session_id = uuid.UUID(payload["id"])
        if created_at.tzinfo is None:
            raise ValueError
    except (
        UnicodeEncodeError,
        binascii.Error,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise AssetValidationFailed(context.request_id) from None
    return created_at, session_id


def _request_checksum(value: object) -> str:
    canonical = json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        document: dict[str, object] = {}
        for field in fields(value):
            item = getattr(value, field.name)
            # Empty model settings did not exist in the v2 Builder request
            # contract. Omitting them preserves idempotent retries across
            # an in-place checkout upgrade.
            if isinstance(item, AgentModelSettings) and item.is_empty:
                continue
            document[field.name] = _jsonable(item)
        return document
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (uuid.UUID, datetime, StrEnum)):
        return str(value)
    return value


def _message_json(
    role: str,
    content: str,
    *,
    now: datetime,
    clarification_request_id: str | None = None,
    clarification_question: str | None = None,
    authoritative_brief: bool = False,
    operation_id: uuid.UUID | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": content,
        "created_at": now.isoformat(),
    }
    if clarification_request_id is not None:
        message["clarification_request_id"] = clarification_request_id
    if clarification_question is not None:
        message["clarification_question"] = clarification_question
    if authoritative_brief:
        message["authoritative_brief"] = True
    if operation_id is not None:
        message["operation_id"] = str(operation_id)
    return message


def _progress_json(
    status: AgentDesignProgressStatus,
) -> list[dict[str, object]]:
    return [
        {
            "id": field,
            "label": label,
            "status": status.value,
        }
        for field, label in (
            ("agents_instructions", "AGENTS.md"),
            ("soul", "SOUL.md"),
            ("identity", "IDENTITY.md"),
            ("user_context", "USER.md"),
        )
    ]


def _blueprint_json(
    blueprint: AgentDesignBlueprint,
    *,
    assumptions: tuple[str, ...] = (),
    conflicts: tuple[AgentDesignConflict, ...] = (),
) -> dict[str, object]:
    document: dict[str, object] = {
        "description": blueprint.description,
        "model_ref": blueprint.model_ref,
        "tool_groups": list(blueprint.tool_groups),
        "skill_refs": [{"scope": value.scope.value, "asset_id": str(value.asset_id)} for value in blueprint.skill_refs],
        "mcp_version_ids": [str(value) for value in blueprint.mcp_version_ids],
        "agents_instructions": blueprint.agents_instructions,
        "soul": blueprint.soul,
        "identity": blueprint.identity,
        "user_context": blueprint.user_context,
    }
    if not blueprint.model_settings.is_empty:
        document["model_settings"] = blueprint.model_settings.model_dump(exclude_none=True)
    if assumptions:
        document["assumptions"] = list(assumptions)
    if conflicts:
        document["conflicts"] = [conflict.model_dump(mode="json") for conflict in conflicts]
    return document


def _candidate_metadata_from_json(
    raw: Mapping[str, object] | None,
) -> tuple[tuple[str, ...], tuple[AgentDesignConflict, ...]]:
    if raw is None:
        return (), ()
    assumptions_raw = raw.get("assumptions", [])
    conflicts_raw = raw.get("conflicts", [])
    if not isinstance(assumptions_raw, list) or not isinstance(
        conflicts_raw,
        list,
    ):
        raise AssetValidationFailed("unknown")
    assumptions: list[str] = []
    try:
        for item in assumptions_raw:
            if not isinstance(item, str):
                raise ValueError
            value = item.strip()
            if not value or len(value) > 500:
                raise ValueError
            assumptions.append(value)
        conflicts = tuple(
            AgentDesignConflict.model_validate_json(
                json.dumps(item, ensure_ascii=False),
                strict=True,
            )
            for item in conflicts_raw
        )
    except (TypeError, ValueError, ValidationError):
        raise AssetValidationFailed("unknown") from None
    if len(assumptions) > 12 or len(conflicts) > 12:
        raise AssetValidationFailed("unknown")
    return tuple(assumptions), conflicts


def _has_blocking_conflicts(
    cls,
    raw: Mapping[str, object] | None,
) -> bool:
    _, conflicts = cls._candidate_metadata_from_json(raw)
    return any(conflict.severity == "error" for conflict in conflicts)


def _remaining_conflicts_after_blueprint_update(
    current: AgentDesignBlueprint | None,
    updated: AgentDesignBlueprint,
    conflicts: tuple[AgentDesignConflict, ...],
) -> tuple[AgentDesignConflict, ...]:
    """Preserve findings until a newly generated candidate replaces them."""

    del current, updated
    return conflicts


def _blueprint_from_json(
    raw: Mapping[str, object] | None,
) -> AgentDesignBlueprint:
    if raw is None:
        raise AssetValidationFailed("unknown")
    try:
        blueprint = AgentDesignBlueprint(
            description=str(raw["description"]),
            model_ref=str(raw["model_ref"]),
            tool_groups=tuple(str(item) for item in raw["tool_groups"]),
            skill_refs=tuple(
                SkillAssetRef(
                    scope=AssetScope(str(item["scope"])),
                    asset_id=uuid.UUID(str(item["asset_id"])),
                )
                for item in raw["skill_refs"]
            ),
            mcp_version_ids=tuple(uuid.UUID(str(item)) for item in raw["mcp_version_ids"]),
            agents_instructions=str(raw["agents_instructions"]),
            soul=str(raw["soul"]),
            identity=str(raw["identity"]),
            user_context=str(raw["user_context"]),
            model_settings=AgentModelSettings.model_validate(raw.get("model_settings", {})),
        )
        if blueprint.model_ref != DEFAULT_AGENT_MODEL_REF and exact_model_ref(blueprint.model_ref) is None:
            raise ValueError
        return blueprint
    except (KeyError, TypeError, ValueError):
        raise AssetValidationFailed("unknown") from None


def _clarification_request(
    question: ClarificationQuestion,
    *,
    index: int = 1,
    total: int = 1,
) -> AgentDesignClarificationRequest:
    if question.kind == "free_text":
        input_mode = "free_text"
    elif question.kind == "single_select":
        input_mode = "choice_with_other"
    else:
        input_mode = "choice_with_other"
    options = tuple(
        AgentDesignClarificationOption(
            id=f"{question.id}-{index + 1}",
            label=option,
            value=option,
        )
        for index, option in enumerate(question.options)
    )
    return AgentDesignClarificationRequest(
        version=1,
        kind="human_input_request",
        source="agent_builder",
        request_id=question.id,
        clarification_type="agent_design",
        title=f"问题 {index}/{total}",
        question=question.prompt,
        context=question.reason,
        input_mode=input_mode,
        options=options,
    )


def _clarification_json(
    request: AgentDesignClarificationRequest,
) -> dict[str, object]:
    return {
        "version": request.version,
        "kind": request.kind,
        "source": request.source,
        "request_id": request.request_id,
        "clarification_type": request.clarification_type,
        "title": request.title,
        "question": request.question,
        "context": request.context,
        "input_mode": request.input_mode,
        "options": [
            {
                "id": option.id,
                "label": option.label,
                "value": option.value,
            }
            for option in request.options
        ],
    }


def _clarification_set_json(
    cls,
    requests: tuple[AgentDesignClarificationRequest, ...],
) -> dict[str, object]:
    if len(requests) != 3:
        raise AssetValidationFailed("unknown")
    return {
        "version": 1,
        "kind": _CLARIFICATION_SET_KIND,
        "questions": [cls._clarification_json(request) for request in requests],
    }


def _clarifications_from_json(
    cls,
    raw: Mapping[str, object],
) -> tuple[AgentDesignClarificationRequest, ...]:
    if raw.get("kind") == _CLARIFICATION_SET_KIND:
        questions = raw.get("questions")
        if not isinstance(questions, list) or len(questions) != 3:
            raise AssetValidationFailed("unknown")
        values = tuple(cls._clarification_from_json(question) for question in questions if isinstance(question, Mapping))
        if len(values) != 3:
            raise AssetValidationFailed("unknown")
        return values
    return (cls._clarification_from_json(raw),)


def _clarification_from_json(
    raw: Mapping[str, object],
) -> AgentDesignClarificationRequest:
    try:
        return AgentDesignClarificationRequest(
            version=int(raw["version"]),
            kind=str(raw["kind"]),
            source=str(raw["source"]),
            request_id=str(raw["request_id"]),
            clarification_type=str(raw["clarification_type"]),
            title=str(raw["title"]),
            question=str(raw["question"]),
            context=str(raw["context"]),
            input_mode=str(raw["input_mode"]),
            options=tuple(
                AgentDesignClarificationOption(
                    id=str(option["id"]),
                    label=str(option["label"]),
                    value=str(option["value"]),
                )
                for option in raw.get("options", ())
                if isinstance(option, Mapping)
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise AssetValidationFailed("unknown") from None


def _clarification_answers(
    row: AgentDesignSessionRow,
) -> dict[str, str]:
    answers: dict[str, str] = {}
    for message in row.messages_json:
        request_id = message.get("clarification_request_id")
        content = message.get("content")
        if isinstance(request_id, str) and isinstance(content, str):
            answers[request_id] = content
    return answers


def _clarification_history(
    row: AgentDesignSessionRow,
) -> tuple[AgentDesignInterviewAnswer, ...]:
    history: list[AgentDesignInterviewAnswer] = []
    for message in row.messages_json:
        request_id = message.get("clarification_request_id")
        content = message.get("content")
        question = message.get("clarification_question")
        if isinstance(request_id, str) and isinstance(content, str):
            history.append(
                AgentDesignInterviewAnswer(
                    id=request_id,
                    question=(question if isinstance(question, str) else request_id),
                    answer=content,
                )
            )
    return tuple(history)


def _session_view(
    row: AgentDesignSessionRow,
) -> AgentDesignSessionView:
    blueprint = _blueprint_from_json(row.blueprint_json) if row.blueprint_json is not None else None
    assumptions, conflicts = _candidate_metadata_from_json(row.blueprint_json)
    messages = tuple(
        AgentDesignMessage(
            id=str(item["id"]),
            role=str(item["role"]),
            content=str(item["content"]),
            created_at=datetime.fromisoformat(str(item["created_at"])),
            operation_id=(uuid.UUID(str(item["operation_id"])) if item.get("operation_id") is not None else None),
        )
        for item in row.messages_json
    )
    progress = tuple(
        AgentDesignProgressItem(
            id=str(item["id"]),
            label=str(item["label"]),
            status=AgentDesignProgressStatus(str(item["status"])),
        )
        for item in row.progress_json
    )
    active_raw = row.active_clarification_json
    active_clarifications: tuple[AgentDesignClarificationRequest, ...] = ()
    if active_raw is not None:
        answered = _clarification_answers(row)
        active_clarifications = tuple(request for request in _clarifications_from_json(_CodecOwner, active_raw) if request.request_id not in answered)
    active = active_clarifications[0] if active_clarifications else None
    return AgentDesignSessionView(
        id=row.id,
        project_id=row.project_id,
        owner_user_id=row.owner_user_id,
        thread_id=row.thread_id,
        slug=row.slug,
        display_name=row.display_name,
        status=AgentDesignStatus(row.status),
        revision=row.revision,
        blueprint=blueprint,
        blueprint_checksum=row.blueprint_checksum,
        assumptions=assumptions,
        conflicts=conflicts,
        messages=messages,
        active_clarification=active,
        active_clarifications=active_clarifications,
        progress=progress,
        error_code=row.error_code,
        error_message=row.error_message,
        created_agent_id=row.created_agent_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        generation_preference=(
            {
                "model_ref": row.generation_model_ref,
                "mode": row.generation_mode,
            }
            if row.generation_model_ref is not None and row.generation_mode is not None
            else None
        ),
    )


class _CodecOwner:
    _clarification_from_json = staticmethod(_clarification_from_json)


def _session_summary(
    row: AgentDesignSessionRow,
) -> AgentDesignSessionSummary:
    return AgentDesignSessionSummary(
        id=row.id,
        slug=row.slug,
        display_name=row.display_name,
        status=AgentDesignStatus(row.status),
        revision=row.revision,
        updated_at=row.updated_at,
    )


def _stable_generation_error_message(code: str) -> str:
    if code == AgentDesignGenerationProfileStale.code:
        return "所选模型或思考强度已不可用，请刷新模型列表后重试。"
    if code == "AGENT_DESIGN_GENERATION_TIMEOUT":
        return "Agent 设定生成超时，请稍后重试。"
    if code in {
        "AGENT_DESIGN_INVALID_MODEL_OUTPUT",
        "AGENT_DESIGN_UNSUPPORTED_CAPABILITY",
        "AGENT_DESIGN_UNDECLARED_CAPABILITY",
    }:
        return "模型返回的 Agent 设定无效，请调整描述后重试。"
    return "Agent 设定生成暂时不可用，请稍后重试。"
