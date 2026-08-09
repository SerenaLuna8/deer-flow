from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.gateway.routers.project_agent_builder import (
    AgentDesignSessionSummaryResponse,
    AgentDesignTurnRequest,
    _turn,
)
from app.gateway.system_model_callers import DatabaseOneshotModelCaller
from app.shared_assets.agent_design_generation import (
    AgentDesignGenerationContext,
    AgentDesignGenerationRequest,
    AgentDesignGenerationService,
    NeedsClarificationResult,
)
from app.shared_assets.agent_design_service import (
    AgentDesignSessionSummary,
    AgentDesignStatus,
)


def test_agent_builder_session_summary_exposes_revision_for_safe_deletion() -> None:
    summary = AgentDesignSessionSummary(
        id=uuid.UUID("00000000-0000-4000-8000-000000000010"),
        slug="code-reviewer",
        display_name="code-reviewer",
        status=AgentDesignStatus.INTERVIEWING,
        revision=7,
        updated_at=datetime(2026, 8, 9, tzinfo=UTC),
    )

    response = AgentDesignSessionSummaryResponse.model_validate(
        summary,
        from_attributes=True,
    )

    assert response.revision == 7


class _RecordingAgentDesignCaller:
    def __init__(self) -> None:
        self.model_ref: str | None = None

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
        model_ref: str | None = None,
    ) -> str:
        del system_instruction, user_content
        self.model_ref = model_ref
        return (
            '{"decision":"needs_clarification","questions":['
            '{"id":"scope","targets":["agents_instructions"],"prompt":"需要覆盖哪些任务？","reason":"明确职责边界","kind":"single_select","required":true,"options":["全面覆盖","关键任务","指定范围"]}]}'
        )


@pytest.mark.asyncio
async def test_agent_design_generation_uses_the_selected_conversation_model() -> None:
    caller = _RecordingAgentDesignCaller()
    service = AgentDesignGenerationService(model_caller=caller)

    result = await service.generate(
        AgentDesignGenerationRequest(
            agent_name="代码审查",
            brief="审查代码并给出建议",
            phase="discovery",
        ),
        context=AgentDesignGenerationContext(),
        model_ref="gpt-5.6-luna",
    )

    assert isinstance(result, NeedsClarificationResult)
    assert caller.model_ref == "gpt-5.6-luna"


def test_agent_builder_turn_maps_the_selected_generation_model() -> None:
    request = AgentDesignTurnRequest.model_validate(
        {
            "input": {"kind": "message", "message": "创建一个代码审查 Agent"},
            "generation_model_ref": "gpt-5.6-luna",
            "expected_revision": 1,
            "idempotency_key": "builder-model-selection",
        }
    )

    command = _turn(request)

    assert command.generation_model_ref == "gpt-5.6-luna"


def test_agent_builder_turn_rejects_an_invalid_generation_model_ref() -> None:
    with pytest.raises(ValidationError):
        AgentDesignTurnRequest.model_validate(
            {
                "input": {"kind": "message", "message": "创建 Agent"},
                "generation_model_ref": "../../provider-secret",
                "expected_revision": 1,
                "idempotency_key": "builder-invalid-model-selection",
            }
        )


@pytest.mark.asyncio
async def test_database_oneshot_caller_materializes_the_selected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materialized_refs: list[str | None] = []
    invoked_names: list[str] = []

    class _Materializer:
        async def materialize_active(self, model_ref: str | None = None):
            materialized_refs.append(model_ref)
            return SimpleNamespace(name="gpt-5.6-luna")

    class _Config:
        def with_runtime_models(self, models):
            assert tuple(model.name for model in models) == ("gpt-5.6-luna",)
            return self

    async def _run_oneshot_llm(**kwargs):
        invoked_names.append(kwargs["model_name"])
        return "ok"

    monkeypatch.setattr(
        "app.gateway.system_model_callers.run_oneshot_llm",
        _run_oneshot_llm,
    )
    caller = DatabaseOneshotModelCaller(
        app_config=_Config(),
        materializer=_Materializer(),
        run_name="agent_design_generation",
    )

    assert (
        await caller(
            system_instruction="system",
            user_content="user",
            model_ref="gpt-5.6-luna",
        )
        == "ok"
    )
    assert materialized_refs == ["gpt-5.6-luna"]
    assert invoked_names == ["gpt-5.6-luna"]
