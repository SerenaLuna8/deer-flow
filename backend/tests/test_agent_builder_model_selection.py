from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.gateway.routers.project_agent_builder import (
    AgentDesignGenerationPreferenceRequest,
    AgentDesignSessionSummaryResponse,
    AgentDesignTurnRequest,
    _generation_preference,
    _turn,
)
from app.gateway.system_model_callers import DatabaseOneshotModelCaller
from app.shared_assets.agent_design_generation import (
    AgentDesignGenerationContext,
    AgentDesignGenerationInvalid,
    AgentDesignGenerationRequest,
    AgentDesignGenerationService,
    NeedsClarificationResult,
)
from app.shared_assets.agent_design_service import (
    AgentDesignService,
    AgentDesignSessionSummary,
    AgentDesignStatus,
)
from deerflow.config.model_execution import FrozenSystemModelExecution
from deerflow.models import ModelRuntimeProfile

GENERATION_MODEL_REF = "00000000-0000-4000-8000-000000000308"
GENERATION_MODEL_VERSION_ID = "00000000-0000-4000-8000-000000000309"
GENERATION_MODEL_CHECKSUM = "a" * 64


def _frozen_model() -> FrozenSystemModelExecution:
    return FrozenSystemModelExecution(
        model_config_id=uuid.UUID(GENERATION_MODEL_REF),
        provider_payload={
            "schema_version": 1,
            "model_config_id": GENERATION_MODEL_REF,
            "provider_adapter": "deepseek",
            "provider_model": "deepseek-v4-flash",
            "max_input_tokens": 64_000,
            "settings": {},
            "supports_thinking": True,
            "supports_reasoning_effort": True,
            "supports_vision": False,
        },
        payload_checksum=GENERATION_MODEL_CHECKSUM,
        secret_generation_id=uuid.UUID(GENERATION_MODEL_VERSION_ID),
        secret_envelope_digest="b" * 64,
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
        self.model_execution: FrozenSystemModelExecution | None = None
        self.thinking_enabled: bool | None = None
        self.reasoning_effort: str | None = None

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
        model_ref: str | None = None,
        model_execution: FrozenSystemModelExecution | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
    ) -> str:
        del system_instruction, user_content
        self.model_ref = model_ref
        self.model_execution = model_execution
        self.thinking_enabled = thinking_enabled
        self.reasoning_effort = reasoning_effort
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
        model_ref=GENERATION_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="high",
    )

    assert isinstance(result, NeedsClarificationResult)
    assert caller.model_ref == GENERATION_MODEL_REF
    assert caller.thinking_enabled is True
    assert caller.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_agent_design_generation_forwards_the_frozen_model_execution() -> None:
    caller = _RecordingAgentDesignCaller()

    await AgentDesignGenerationService(model_caller=caller).generate(
        AgentDesignGenerationRequest(
            agent_name="代码审查",
            brief="审查代码并给出建议",
            phase="discovery",
        ),
        context=AgentDesignGenerationContext(),
        model_ref=GENERATION_MODEL_REF,
        model_execution=_frozen_model(),
    )

    assert caller.model_execution == _frozen_model()


@pytest.mark.asyncio
async def test_agent_design_generation_rejects_a_legacy_logical_model_name() -> None:
    caller = _RecordingAgentDesignCaller()

    with pytest.raises(AgentDesignGenerationInvalid):
        await AgentDesignGenerationService(model_caller=caller).generate(
            AgentDesignGenerationRequest(
                agent_name="代码审查",
                brief="审查代码并给出建议",
                phase="discovery",
            ),
            context=AgentDesignGenerationContext(),
            model_ref="gpt-5.6-luna",
        )

    assert caller.model_ref is None


def test_agent_builder_turn_maps_the_selected_generation_model() -> None:
    request = AgentDesignTurnRequest.model_validate(
        {
            "input": {"kind": "message", "message": "创建一个代码审查 Agent"},
            "generation_model_ref": GENERATION_MODEL_REF,
            "generation_mode": "pro",
            "thinking_enabled": True,
            "reasoning_effort": "medium",
            "expected_revision": 1,
            "idempotency_key": "builder-model-selection",
        }
    )

    command = _turn(request)

    assert command.generation_model_ref == GENERATION_MODEL_REF
    assert command.generation_mode == "pro"
    assert command.thinking_enabled is True
    assert command.reasoning_effort == "medium"


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


def test_agent_builder_maps_the_session_generation_preference() -> None:
    request = AgentDesignGenerationPreferenceRequest.model_validate(
        {
            "generation_model_ref": GENERATION_MODEL_REF,
            "generation_mode": "ultra",
            "thinking_enabled": True,
            "reasoning_effort": "high",
        }
    )

    command = _generation_preference(request)

    assert command.generation_model_ref == GENERATION_MODEL_REF
    assert command.generation_mode == "ultra"
    assert command.thinking_enabled is True
    assert command.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_database_oneshot_caller_materializes_the_selected_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    materialized_refs: list[str | None] = []
    invocation: dict[str, object] = {}

    class _Materializer:
        async def materialize_active(self, model_ref: str | None = None):
            materialized_refs.append(model_ref)
            return SimpleNamespace(name=GENERATION_MODEL_REF)

    class _Config:
        def with_runtime_models(self, models):
            assert tuple(model.name for model in models) == (GENERATION_MODEL_REF,)
            return self

    async def _run_oneshot_llm(**kwargs):
        invocation.update(kwargs)
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
            model_ref=GENERATION_MODEL_REF,
        )
        == "ok"
    )
    assert materialized_refs == [GENERATION_MODEL_REF]
    assert invocation["model_name"] == GENERATION_MODEL_REF
    assert invocation["profile"] is ModelRuntimeProfile.PRIVATE_ONESHOT
    assert invocation["thinking_enabled"] is False
    assert invocation["reasoning_effort"] is None
    assert "attach_tracing" not in invocation


@pytest.mark.asyncio
async def test_database_oneshot_caller_forwards_builder_reasoning_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation: dict[str, object] = {}

    class _Materializer:
        async def materialize_active(self, model_ref: str | None = None):
            del model_ref
            return SimpleNamespace(name=GENERATION_MODEL_REF)

    class _Config:
        def with_runtime_models(self, models):
            del models
            return self

    async def _run_oneshot_llm(**kwargs):
        invocation.update(kwargs)
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

    await caller(
        system_instruction="system",
        user_content="user",
        model_ref=GENERATION_MODEL_REF,
        thinking_enabled=True,
        reasoning_effort="medium",
    )

    assert invocation["thinking_enabled"] is True
    assert invocation["reasoning_effort"] == "medium"


@pytest.mark.asyncio
async def test_database_oneshot_caller_materializes_the_frozen_model_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exact_materialization: list[FrozenSystemModelExecution] = []

    class _Materializer:
        async def materialize_frozen(self, execution):
            exact_materialization.append(execution)
            return SimpleNamespace(name=GENERATION_MODEL_REF)

        async def materialize_active(self, _model_ref: str | None = None):
            raise AssertionError("a frozen Builder turn must not resolve Current Version")

    class _Config:
        def with_runtime_models(self, models):
            del models
            return self

    async def _run_oneshot_llm(**_kwargs):
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
            model_ref=GENERATION_MODEL_REF,
            model_execution=_frozen_model(),
        )
        == "ok"
    )
    assert exact_materialization == [_frozen_model()]


@pytest.mark.asyncio
async def test_builder_settlement_does_not_recheck_the_current_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Repository:
        def __init__(self, _session: object) -> None:
            pass

        async def resolve_active_model(self, *_args: object, **_kwargs: object):
            raise AssertionError("settlement must not read the mutable model row")

    monkeypatch.setattr(
        "app.shared_assets.agent_design_service.SystemModelRepository",
        _Repository,
    )
    operation = SimpleNamespace(
        requested_generation_profile_json={
            "model_ref": GENERATION_MODEL_REF,
            "mode": "pro",
            "thinking_enabled": True,
            "reasoning_effort": "medium",
        },
        effective_generation_profile_json={
            "model_ref": GENERATION_MODEL_REF,
            "mode": "pro",
            "thinking_enabled": True,
            "reasoning_effort": "medium",
            "model_execution": {
                "model_config_id": GENERATION_MODEL_REF,
                "provider_payload": dict(_frozen_model().provider_payload),
                "payload_checksum": GENERATION_MODEL_CHECKSUM,
                "secret_generation_id": GENERATION_MODEL_VERSION_ID,
                "secret_envelope_digest": "b" * 64,
            },
        },
    )

    assert await AgentDesignService._generation_profile_is_valid(  # noqa: SLF001
        SimpleNamespace(),  # type: ignore[arg-type]
        operation,  # type: ignore[arg-type]
    )
