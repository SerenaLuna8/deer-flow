from __future__ import annotations

import json

import pytest

from app.shared_assets.skill_design_generation import (
    CandidateResult,
    NeedsClarificationResult,
    SkillDesignGenerationInvalid,
    SkillDesignGenerationRequest,
    SkillDesignGenerationService,
)


class _CapturingCaller:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
    ) -> str:
        self.calls.append((system_instruction, user_content))
        return self.response


class _SequentialCaller:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
    ) -> str:
        self.calls.append((system_instruction, user_content))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_skill_design_generation_uses_pinned_skill_creator_and_returns_strict_candidate() -> None:
    caller = _CapturingCaller(
        json.dumps(
            {
                "decision": "candidate",
                "files": [
                    {
                        "path": "SKILL.md",
                        "media_type": "text/markdown",
                        "content": "---\nname: release-notes\ndescription: Create release notes.\n---\n\n# Workflow\n\nDraft concise notes.",
                    },
                    {
                        "path": "references/style.md",
                        "media_type": "text/markdown",
                        "content": "# Style\n\nUse short headings.",
                    },
                ],
                "summary": "Created the Skill package.",
            }
        )
    )
    service = SkillDesignGenerationService(model_caller=caller)
    pinned_skill_creator = "# Skill Creator\n\nKeep SKILL.md concise and use progressive disclosure."

    result = await service.generate(
        SkillDesignGenerationRequest(
            skill_slug="release-notes",
            skill_name="Release Notes",
            brief="Create release notes from merged pull requests.",
        ),
        skill_creator_content=pinned_skill_creator,
    )

    assert isinstance(result, CandidateResult)
    assert [item.path for item in result.files] == [
        "SKILL.md",
        "references/style.md",
    ]
    assert caller.calls
    system_instruction, user_content = caller.calls[0]
    assert pinned_skill_creator in system_instruction
    assert "Do not call tools" in system_instruction
    assert "release-notes" in user_content


@pytest.mark.asyncio
async def test_skill_design_generation_accepts_only_one_strict_json_object() -> None:
    caller = _CapturingCaller(
        """```json
{"decision":"candidate","files":[],"summary":"invalid"}
```"""
    )
    service = SkillDesignGenerationService(model_caller=caller)

    with pytest.raises(SkillDesignGenerationInvalid) as captured:
        await service.generate(
            SkillDesignGenerationRequest(
                skill_slug="release-notes",
                skill_name="Release Notes",
                brief="Create release notes.",
            ),
            skill_creator_content="# Skill Creator",
        )

    assert captured.value.code == "SKILL_DESIGN_INVALID_MODEL_OUTPUT"
    assert len(caller.calls) == 2


@pytest.mark.asyncio
async def test_skill_design_generation_repairs_one_invalid_model_response() -> None:
    caller = _SequentialCaller(
        [
            "I created the requested Skill.",
            json.dumps(
                {
                    "decision": "candidate",
                    "files": [
                        {
                            "path": "SKILL.md",
                            "media_type": "text/markdown",
                            "content": "---\nname: release-notes\ndescription: Create release notes.\n---\n\n# Workflow\n\nDraft concise notes.",
                        }
                    ],
                    "summary": "Created the Skill package.",
                }
            ),
        ]
    )

    result = await SkillDesignGenerationService(model_caller=caller).generate(
        SkillDesignGenerationRequest(
            skill_slug="release-notes",
            skill_name="Release Notes",
            brief="Create release notes.",
        ),
        skill_creator_content="# Skill Creator",
    )

    assert isinstance(result, CandidateResult)
    assert len(caller.calls) == 2
    assert caller.calls[0][1] == caller.calls[1][1]
    assert "previous response did not satisfy" in caller.calls[1][0]


@pytest.mark.asyncio
async def test_skill_design_generation_maps_lone_surrogate_to_stable_invalid_output() -> None:
    caller = _CapturingCaller("\ud800")
    service = SkillDesignGenerationService(model_caller=caller)

    with pytest.raises(SkillDesignGenerationInvalid) as captured:
        await service.generate(
            SkillDesignGenerationRequest(
                skill_slug="release-notes",
                skill_name="Release Notes",
                brief="Create release notes.",
            ),
            skill_creator_content="# Skill Creator",
        )

    assert captured.value.code == "SKILL_DESIGN_INVALID_MODEL_OUTPUT"


@pytest.mark.asyncio
async def test_skill_design_generation_returns_bounded_clarification() -> None:
    caller = _CapturingCaller(
        json.dumps(
            {
                "decision": "needs_clarification",
                "questions": [
                    {
                        "id": "source",
                        "prompt": "Where do merged changes come from?",
                        "reason": "The source determines the repeatable workflow.",
                        "kind": "single_select",
                        "required": True,
                        "options": ["GitHub pull requests", "Local changelog"],
                    }
                ],
            }
        )
    )
    service = SkillDesignGenerationService(model_caller=caller)

    result = await service.generate(
        SkillDesignGenerationRequest(
            skill_slug="release-notes",
            skill_name="Release Notes",
            brief="Create release notes.",
        ),
        skill_creator_content="# Skill Creator",
    )

    assert isinstance(result, NeedsClarificationResult)
    assert result.questions[0].id == "source"


@pytest.mark.asyncio
async def test_skill_design_generation_rejects_multiple_clarification_questions() -> None:
    caller = _CapturingCaller(
        json.dumps(
            {
                "decision": "needs_clarification",
                "questions": [
                    {
                        "id": "source",
                        "prompt": "What is the source?",
                        "reason": "Select the integration.",
                        "kind": "free_text",
                        "required": True,
                        "options": [],
                    },
                    {
                        "id": "format",
                        "prompt": "What is the format?",
                        "reason": "Select the output.",
                        "kind": "free_text",
                        "required": True,
                        "options": [],
                    },
                ],
            }
        )
    )

    with pytest.raises(SkillDesignGenerationInvalid):
        await SkillDesignGenerationService(model_caller=caller).generate(
            SkillDesignGenerationRequest(
                skill_slug="release-notes",
                skill_name="Release Notes",
                brief="Create release notes.",
            ),
            skill_creator_content="# Skill Creator",
        )


@pytest.mark.asyncio
async def test_skill_design_generation_rejects_unsafe_or_duplicate_paths() -> None:
    caller = _CapturingCaller(
        json.dumps(
            {
                "decision": "candidate",
                "files": [
                    {
                        "path": "SKILL.md",
                        "media_type": "text/markdown",
                        "content": "---\nname: release-notes\ndescription: Create release notes.\n---\n",
                    },
                    {
                        "path": "../SKILL.md",
                        "media_type": "text/markdown",
                        "content": "escape",
                    },
                ],
                "summary": "Created files.",
            }
        )
    )
    service = SkillDesignGenerationService(model_caller=caller)

    with pytest.raises(SkillDesignGenerationInvalid):
        await service.generate(
            SkillDesignGenerationRequest(
                skill_slug="release-notes",
                skill_name="Release Notes",
                brief="Create release notes.",
            ),
            skill_creator_content="# Skill Creator",
        )
