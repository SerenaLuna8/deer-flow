"""Pure projections and serialization for Project Skill Builder designs."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import datetime

from app.projects.context import ProjectContext
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.skill_design_contracts import (
    SkillDesignClarificationOption,
    SkillDesignClarificationRequest,
    SkillDesignSecretRequirement,
    SkillDesignServiceErrorCode,
    SkillDesignSessionSummary,
    SkillDesignStatus,
    SkillDesignValidation,
)
from app.shared_assets.skill_design_generation import (
    MAX_SKILL_DESIGN_BRIEF_CHARS,
    ClarificationQuestion,
)
from app.shared_assets.skill_package_integrity import SkillArchivePreview
from deerflow.persistence.shared_assets import SkillDesignSessionRow

_CHECKSUM_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _conversation_brief(
    context: ProjectContext,
    messages: object,
) -> str:
    """Build a bounded, role-preserving transcript, newest turns first."""

    if not isinstance(messages, list) or not messages:
        raise AssetValidationFailed(context.request_id)
    remaining = MAX_SKILL_DESIGN_BRIEF_CHARS
    newest_first: list[str] = []
    for raw in reversed(messages):
        if not isinstance(raw, Mapping):
            raise AssetValidationFailed(context.request_id)
        role = raw.get("role")
        content = raw.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise AssetValidationFailed(context.request_id)
        content = content.strip()
        if not content:
            raise AssetValidationFailed(context.request_id)
        prefix = f"{role}: "
        separator_size = 1 if newest_first else 0
        available = remaining - separator_size
        if available <= len(prefix):
            break
        if len(prefix) + len(content) <= available:
            segment = f"{prefix}{content}"
        else:
            content_budget = available - len(prefix)
            if newest_first and content_budget > 1:
                segment = f"{prefix}…{content[-(content_budget - 1) :]}"
            else:
                segment = f"{prefix}{content[:content_budget]}"
        newest_first.append(segment)
        remaining -= len(segment) + separator_size
        if remaining <= len("assistant: "):
            break
    if not newest_first:
        raise AssetValidationFailed(context.request_id)
    return "\n".join(reversed(newest_first))


def _validation_from_preview(
    preview: SkillArchivePreview,
    *,
    validated_at: datetime,
) -> SkillDesignValidation:
    return SkillDesignValidation(
        draft_checksum=preview.checksum,
        validated_at=validated_at,
        description=preview.description,
        frontmatter=dict(preview.frontmatter),
        compatibility=preview.compatibility,
        secret_requirements=tuple(
            SkillDesignSecretRequirement(
                name=item.name,
                target_env=item.target_env,
                optional=item.optional,
            )
            for item in preview.secret_requirements
        ),
    )


def _validation_matches_preview(
    validation: SkillDesignValidation,
    preview: SkillArchivePreview,
) -> bool:
    expected = _validation_from_preview(
        preview,
        validated_at=validation.validated_at,
    )
    return validation == expected


def _validation_json(
    validation: SkillDesignValidation,
) -> dict[str, object]:
    return {
        "draft_checksum": validation.draft_checksum,
        "validated_at": validation.validated_at.isoformat(),
        "description": validation.description,
        "frontmatter": dict(validation.frontmatter),
        "compatibility": validation.compatibility,
        "secret_requirements": [
            {
                "name": item.name,
                "target_env": item.target_env,
                "optional": item.optional,
            }
            for item in validation.secret_requirements
        ],
    }


def _validation_from_json(
    context: ProjectContext,
    value: object,
) -> SkillDesignValidation:
    if not isinstance(value, dict):
        raise AssetValidationFailed(context.request_id)
    try:
        checksum = value["draft_checksum"]
        validated_at = datetime.fromisoformat(value["validated_at"])
        description = value["description"]
        frontmatter = value["frontmatter"]
        compatibility = value["compatibility"]
        requirements = value["secret_requirements"]
    except (KeyError, TypeError, ValueError):
        raise AssetValidationFailed(context.request_id) from None
    if (
        not isinstance(checksum, str)
        or _CHECKSUM_PATTERN.fullmatch(checksum) is None
        or not isinstance(description, str)
        or not isinstance(frontmatter, dict)
        or compatibility is not None
        and not isinstance(compatibility, str)
        or not isinstance(requirements, list)
    ):
        raise AssetValidationFailed(context.request_id)
    parsed_requirements: list[SkillDesignSecretRequirement] = []
    for item in requirements:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("target_env"), str) or type(item.get("optional")) is not bool:
            raise AssetValidationFailed(context.request_id)
        parsed_requirements.append(
            SkillDesignSecretRequirement(
                name=item["name"],
                target_env=item["target_env"],
                optional=item["optional"],
            )
        )
    return SkillDesignValidation(
        draft_checksum=checksum,
        validated_at=validated_at,
        description=description,
        frontmatter=frontmatter,
        compatibility=compatibility,
        secret_requirements=tuple(parsed_requirements),
    )


def _message_json(
    role: str,
    content: str,
    *,
    now: datetime,
    operation_id: uuid.UUID | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "id": uuid.uuid4().hex,
        "role": role,
        "content": content,
        "created_at": now.isoformat(),
    }
    if operation_id is not None:
        message["operation_id"] = str(operation_id)
    return message


def _progress_json(
    status: SkillDesignStatus,
) -> list[dict[str, object]]:
    if status is SkillDesignStatus.GENERATING:
        values = ("completed", "running", "pending")
    elif status is SkillDesignStatus.DRAFT_READY:
        values = ("completed", "completed", "pending")
    elif status in {
        SkillDesignStatus.VALIDATED,
        SkillDesignStatus.COMMITTING,
        SkillDesignStatus.COMPLETED,
    }:
        values = ("completed", "completed", "completed")
    elif status is SkillDesignStatus.FAILED:
        values = ("completed", "failed", "pending")
    elif status is SkillDesignStatus.AWAITING_CLARIFICATION:
        values = ("running", "pending", "pending")
    else:
        values = ("pending", "pending", "pending")
    return [
        {
            "id": "interview",
            "label": "确认需求",
            "status": values[0],
        },
        {
            "id": "package",
            "label": "生成候选文件",
            "status": values[1],
        },
        {
            "id": "validate",
            "label": "检查 Skill",
            "status": values[2],
        },
    ]


def _clarification_request(
    question: ClarificationQuestion,
) -> SkillDesignClarificationRequest:
    request_id = uuid.uuid4().hex
    options = tuple(
        SkillDesignClarificationOption(
            id=f"{question.id}-{index}",
            label=value,
            value=value,
        )
        for index, value in enumerate(question.options, start=1)
    )
    return SkillDesignClarificationRequest(
        version=1,
        kind="human_input_request",
        source="skill-builder",
        request_id=request_id,
        clarification_type="skill_design",
        title="补充 Skill 信息",
        question=question.prompt,
        context=question.reason,
        input_mode=("single_choice" if question.kind == "single_select" else "free_text"),
        options=options,
    )


def _clarification_json(
    request: SkillDesignClarificationRequest,
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
        "options": [{"id": item.id, "label": item.label, "value": item.value} for item in request.options],
    }


def _clarification_from_json(
    context: ProjectContext,
    value: object,
) -> SkillDesignClarificationRequest:
    if not isinstance(value, dict):
        raise AssetValidationFailed(context.request_id)
    try:
        options = tuple(
            SkillDesignClarificationOption(
                id=item["id"],
                label=item["label"],
                value=item["value"],
            )
            for item in value.get("options", [])
            if isinstance(item, dict)
        )
        request = SkillDesignClarificationRequest(
            version=value["version"],
            kind=value["kind"],
            source=value["source"],
            request_id=value["request_id"],
            clarification_type=value["clarification_type"],
            title=value["title"],
            question=value["question"],
            context=value["context"],
            input_mode=value["input_mode"],
            options=options,
        )
    except (KeyError, TypeError, ValueError):
        raise AssetValidationFailed(context.request_id) from None
    return request


def _session_summary(
    row: SkillDesignSessionRow,
) -> SkillDesignSessionSummary:
    return SkillDesignSessionSummary(
        id=row.id,
        slug=row.slug,
        display_name=row.display_name,
        status=SkillDesignStatus(row.status),
        revision=row.revision,
        updated_at=row.updated_at,
        session_kind=row.session_kind,
    )


def _idempotency_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request_checksum(value: object) -> str:
    canonical = json.dumps(
        _jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


def _stable_generation_error_message(code: str) -> str:
    if code == "MODEL_OUTPUT_LIMIT":
        return "本轮达到模型输出上限。已保存成功写入的候选草稿；请发送“基于现有草稿继续完成”让 Builder 续作。"
    if code == SkillDesignServiceErrorCode.INVALID_MODEL_OUTPUT.value:
        return "生成结果不是有效的 Skill 文件包，请调整描述后重试。"
    if code == SkillDesignServiceErrorCode.GENERATION_INTERRUPTED.value:
        return "上一次生成已中断，请重新发送你的要求。"
    return "Skill 生成暂时不可用，请稍后重试。"
