"""Per-turn model, reasoning-effort, and attachment contract for Skill Builder."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi import HTTPException

from app.gateway.routers.project_skill_builder import (
    SkillDesignAttachmentRequest,
    SkillDesignExecutionPreferenceRequest,
    SkillDesignMessageTurnRequest,
    SkillDesignTurnRequest,
    _execution_preference,
    _turn,
    require_admissible_execution_options,
)
from app.private_work.skill_builder_run_admission import (
    SkillBuilderRunAdmissionService,
)
from app.projects.capabilities import Capability
from app.projects.context import ProjectContext
from app.projects.models import ProjectRole
from app.shared_assets.errors import AssetValidationFailed
from app.shared_assets.skill_design_generation import (
    MAX_SKILL_DESIGN_ATTACHMENT_BYTES,
    SkillDesignAttachment,
    SkillDesignGeneratedFile,
    SkillDesignGenerationRequest,
    SkillDesignGenerationService,
)
from app.shared_assets.skill_design_service import (
    SetSkillDesignExecutionPreference,
    SkillDesignMessageTurn,
    SkillDesignService,
    SkillDesignTurnAttachment,
    SubmitSkillDesignTurn,
)
from app.system_settings import PublicSystemModelView

_MODEL_REF = "00000000-0000-4000-8000-000000000101"
_OTHER_MODEL_REF = "00000000-0000-4000-8000-000000000102"
_PLAIN_MODEL_REF = "00000000-0000-4000-8000-000000000103"
_THINKING_MODEL_REF = "00000000-0000-4000-8000-000000000104"


def _context() -> ProjectContext:
    return ProjectContext(
        user_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
        project_id=uuid.UUID("00000000-0000-4000-8000-000000000002"),
        membership_id=uuid.UUID("00000000-0000-4000-8000-000000000003"),
        role=ProjectRole.ADMIN,
        capabilities=frozenset({Capability.SHARED_ASSETS_READ, Capability.SHARED_ASSETS_EDIT}),
        membership_version=1,
        request_id="req-1",
    )


def _command(turn: SkillDesignMessageTurn) -> SubmitSkillDesignTurn:
    return SubmitSkillDesignTurn(
        input=turn,
        expected_revision=1,
        idempotency_key="key-1",
    )


def _model(
    model_ref: str,
    *,
    supports_thinking: bool = True,
    is_default: bool = False,
) -> PublicSystemModelView:
    return PublicSystemModelView(
        model_ref=model_ref,
        display_name=model_ref,
        supports_thinking=supports_thinking,
        supports_reasoning_effort=supports_thinking,
        supports_vision=False,
        supports_vision_bridge=False,
        is_default=is_default,
    )


class TestValidateTurn:
    def test_normalizes_model_effort_and_attachments(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="请根据附件生成 Skill",
            model_name=_MODEL_REF,
            reasoning_effort="high",
            attachments=(SkillDesignTurnAttachment(name="api.md", content="# API\n"),),
        )

        validated = SkillDesignService._validate_turn(_context(), _command(turn))

        assert isinstance(validated.input, SkillDesignMessageTurn)
        assert validated.input.model_name == _MODEL_REF
        assert validated.input.reasoning_effort == "high"
        assert validated.input.attachments == (SkillDesignTurnAttachment(name="api.md", content="# API\n"),)

    def test_rejects_unknown_reasoning_effort(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            reasoning_effort="ultra",
        )

        with pytest.raises(AssetValidationFailed):
            SkillDesignService._validate_turn(_context(), _command(turn))

    def test_rejects_invalid_model_name(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            model_name="Bad/Name",
        )

        with pytest.raises(AssetValidationFailed):
            SkillDesignService._validate_turn(_context(), _command(turn))

    def test_rejects_oversized_attachment(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            attachments=(
                SkillDesignTurnAttachment(
                    name="big.txt",
                    content="a" * (MAX_SKILL_DESIGN_ATTACHMENT_BYTES + 1),
                ),
            ),
        )

        with pytest.raises(AssetValidationFailed):
            SkillDesignService._validate_turn(_context(), _command(turn))

    def test_rejects_secret_like_attachment(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            attachments=(
                SkillDesignTurnAttachment(
                    name="env.txt",
                    content="api_key = sk-abcdefghijklmnop1234",
                ),
            ),
        )

        with pytest.raises(AssetValidationFailed):
            SkillDesignService._validate_turn(_context(), _command(turn))

    def test_rejects_duplicate_attachment_names(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            attachments=(
                SkillDesignTurnAttachment(name="a.md", content="x"),
                SkillDesignTurnAttachment(name="a.md", content="y"),
            ),
        )

        with pytest.raises(AssetValidationFailed):
            SkillDesignService._validate_turn(_context(), _command(turn))

    def test_accepts_non_ascii_attachment_names(self) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            attachments=(SkillDesignTurnAttachment(name="接口说明.md", content="# 说明"),),
        )

        validated = SkillDesignService._validate_turn(_context(), _command(turn))

        assert isinstance(validated.input, SkillDesignMessageTurn)
        assert validated.input.attachments[0].name == "接口说明.md"

    @pytest.mark.parametrize("name", ["../etc", "a/b.md", ".env", "a\\b", "a:b"])
    def test_rejects_path_like_attachment_names(self, name: str) -> None:
        turn = SkillDesignMessageTurn(
            kind="message",
            message="hi",
            attachments=(SkillDesignTurnAttachment(name=name, content="x"),),
        )

        with pytest.raises(AssetValidationFailed):
            SkillDesignService._validate_turn(_context(), _command(turn))


class TestGenerationExecutionOptions:
    @pytest.mark.asyncio
    async def test_forwards_model_effort_and_attachments(self) -> None:
        calls: list[dict[str, object]] = []

        async def caller(
            *,
            system_instruction: str,
            user_content: str,
            model_name: str | None = None,
            reasoning_effort: str | None = None,
        ) -> str:
            calls.append(
                {
                    "user_content": user_content,
                    "model_name": model_name,
                    "reasoning_effort": reasoning_effort,
                }
            )
            return json.dumps(
                {
                    "decision": "candidate",
                    "files": [
                        {
                            "path": "SKILL.md",
                            "media_type": "text/markdown",
                            "content": "---\nname: demo-skill\n---\n",
                        }
                    ],
                    "summary": "候选文件已生成",
                }
            )

        service = SkillDesignGenerationService(caller)
        request = SkillDesignGenerationRequest(
            skill_slug="demo-skill",
            skill_name="demo-skill",
            brief="user: 根据附件生成",
            attachments=(SkillDesignAttachment(name="api.md", content="# 附件内容\n"),),
        )

        result = await service.generate(
            request,
            skill_creator_content="# skill-creator",
            model_name=_MODEL_REF,
            reasoning_effort="high",
        )

        assert result.status == "candidate"
        assert calls
        assert calls[0]["model_name"] == _MODEL_REF
        assert calls[0]["reasoning_effort"] == "high"
        payload = json.loads(str(calls[0]["user_content"]).split("--- BEGIN UNTRUSTED SKILL DESIGN INPUT ---\n")[1].split("\n--- END UNTRUSTED SKILL DESIGN INPUT ---")[0])
        assert payload["attachments"] == [{"name": "api.md", "content": "# 附件内容\n"}]


class TestDurableRunInput:
    def test_first_turn_imports_bounded_transcript_without_eager_draft_content(
        self,
    ) -> None:
        request = SkillDesignGenerationRequest(
            skill_slug="demo-skill",
            skill_name="Demo Skill",
            brief="user: first\nassistant: question\nuser: answer",
            current_files=(
                SkillDesignGeneratedFile(
                    path="SKILL.md",
                    media_type="text/markdown",
                    content="---\nname: demo-skill\n---\n",
                ),
            ),
        )

        payload = SkillBuilderRunAdmissionService._run_input_payload(
            request,
            turn_message="answer",
            first_turn=True,
            draft_checksum="a" * 64,
            request_id="req-1",
        )

        assert payload["conversation"] == {
            "mode": "initial",
            "brief": request.brief,
        }
        assert payload["authoring"] == {"kind": "create"}
        assert "content" not in payload["draft"]["files"][0]
        assert payload["draft"]["checksum"] == "a" * 64
        assert payload["prior_dependency_references"] == []

    def test_continuation_sends_only_current_turn_not_checkpoint_history(
        self,
    ) -> None:
        request = SkillDesignGenerationRequest(
            skill_slug="demo-skill",
            skill_name="Demo Skill",
            brief="user: old request\nassistant: old answer\nuser: new delta",
        )

        payload = SkillBuilderRunAdmissionService._run_input_payload(
            request,
            turn_message="new delta",
            first_turn=False,
            draft_checksum=None,
            prior_dependency_references=(
                "skill:system:data-analysis:v2",
                "mcp:project:docs:v3:search_docs",
            ),
            request_id="req-1",
        )

        assert payload["conversation"] == {
            "mode": "continuation",
            "turn": "new delta",
        }
        assert "old request" not in json.dumps(payload, ensure_ascii=False)
        assert payload["prior_dependency_references"] == [
            "mcp:project:docs:v3:search_docs",
            "skill:system:data-analysis:v2",
        ]

    @pytest.mark.asyncio
    async def test_rejects_invalid_execution_options(self) -> None:
        async def caller(**_: object) -> str:
            raise AssertionError("model must not be called")

        service = SkillDesignGenerationService(caller)
        request = SkillDesignGenerationRequest(
            skill_slug="demo-skill",
            skill_name="demo-skill",
            brief="user: hi",
        )

        from app.shared_assets.skill_design_generation import (
            SkillDesignGenerationInvalid,
        )

        with pytest.raises(SkillDesignGenerationInvalid):
            await service.generate(
                request,
                skill_creator_content="# skill-creator",
                reasoning_effort="extreme",
            )

        with pytest.raises(SkillDesignGenerationInvalid):
            await service.generate(
                request,
                skill_creator_content="# skill-creator",
                model_name="Bad Name",
            )


class TestRouterAdmission:
    def test_maps_the_session_execution_preference(self) -> None:
        body = SkillDesignExecutionPreferenceRequest.model_validate(
            {
                "model_name": _MODEL_REF,
                "mode": "pro",
                "thinking_enabled": True,
                "reasoning_effort": "medium",
            }
        )

        command = _execution_preference(body)

        assert command == SetSkillDesignExecutionPreference(
            model_name=_MODEL_REF,
            mode="pro",
            thinking_enabled=True,
            reasoning_effort="medium",
        )

    @pytest.mark.parametrize(
        ("mode", "thinking_enabled", "reasoning_effort"),
        [
            ("flash", True, "none"),
            ("thinking", False, "low"),
            ("pro", True, "high"),
        ],
    )
    def test_rejects_inconsistent_session_execution_preference(
        self,
        mode: str,
        thinking_enabled: bool,
        reasoning_effort: str,
    ) -> None:
        with pytest.raises(ValueError):
            SkillDesignExecutionPreferenceRequest.model_validate(
                {
                    "model_name": _MODEL_REF,
                    "mode": mode,
                    "thinking_enabled": thinking_enabled,
                    "reasoning_effort": reasoning_effort,
                }
            )

    def test_turn_conversion_carries_execution_options(self) -> None:
        body = SkillDesignTurnRequest(
            input=SkillDesignMessageTurnRequest(
                kind="message",
                message="hi",
                model_name=_MODEL_REF,
                reasoning_effort="medium",
                attachments=[
                    SkillDesignAttachmentRequest(name="api.md", content="# A"),
                ],
            ),
            expected_revision=1,
            idempotency_key="key-1",
        )

        command = _turn(body)

        assert isinstance(command.input, SkillDesignMessageTurn)
        assert command.input.model_name == _MODEL_REF
        assert command.input.reasoning_effort == "medium"
        assert command.input.attachments == (SkillDesignTurnAttachment(name="api.md", content="# A"),)

    def test_allows_absent_options_without_catalog(self) -> None:
        require_admissible_execution_options(
            [],
            model_name=None,
            reasoning_effort=None,
            request_id="req-1",
        )
        require_admissible_execution_options(
            [],
            model_name=None,
            reasoning_effort="none",
            request_id="req-1",
        )

    def test_rejects_model_missing_from_catalog(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            require_admissible_execution_options(
                [_model(_MODEL_REF, is_default=True)],
                model_name=_OTHER_MODEL_REF,
                reasoning_effort=None,
                request_id="req-1",
            )

        assert excinfo.value.status_code == 422
        assert excinfo.value.detail["code"] == "SKILL_BUILDER_MODEL_UNAVAILABLE"

    def test_rejects_thinking_on_unsupported_model(self) -> None:
        with pytest.raises(HTTPException) as excinfo:
            require_admissible_execution_options(
                [_model(_PLAIN_MODEL_REF, supports_thinking=False)],
                model_name=_PLAIN_MODEL_REF,
                reasoning_effort="high",
                request_id="req-1",
            )

        assert excinfo.value.detail["code"] == "SKILL_BUILDER_EFFORT_UNSUPPORTED"

    def test_checks_default_model_for_effort_only_requests(self) -> None:
        with pytest.raises(HTTPException):
            require_admissible_execution_options(
                [_model(_PLAIN_MODEL_REF, supports_thinking=False, is_default=True)],
                model_name=None,
                reasoning_effort="low",
                request_id="req-1",
            )

        require_admissible_execution_options(
            [_model(_THINKING_MODEL_REF, is_default=True)],
            model_name=None,
            reasoning_effort="low",
            request_id="req-1",
        )
