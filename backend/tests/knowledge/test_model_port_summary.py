"""M11 T1 gates: RegistryKnowledgeModelPort summary-model resolution.

``resolve_summary_model`` reads the host ``knowledge_system_settings``
singleton and validates the referenced System Model against a real
PostgreSQL catalog installed from the composed Schema V1 snapshot; ``generate_summary``
stays a frozen signature that rejects unconfigured runtimes with the typed
``KNOWLEDGE_MODEL_UNAVAILABLE`` error until the pipeline task wires the
ModelRuntime dispatch.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import sqlalchemy as sa
from actweave_knowledge import KNOWLEDGE_MODEL_UNAVAILABLE, KnowledgeError
from langchain_core.messages import AIMessage, HumanMessage
from registry_helpers import registry_model_port, registry_secret_key
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.system_model_seed import seed_system_model_config

from app.knowledge.model_port import RegistryKnowledgeModelPort
from deerflow.models.runtime import ModelRuntimeProfile
from deerflow.persistence.knowledge_settings import KnowledgeSystemSettingsRow
from deerflow.persistence.system_settings import SystemModelConfigRow


async def _seed_user(session: object, label: str) -> str:
    user_id = str(uuid.uuid4())
    await session.execute(  # type: ignore[attr-defined]
        text(
            """INSERT INTO users (
                   id, email, username, system_role, created_at,
                   needs_setup, token_version
               ) VALUES (
                   :user_id, :email, :username, 'user', now(), false, 1
               )"""
        ),
        {
            "user_id": user_id,
            "email": f"{label}@example.invalid",
            "username": f"summary_{label}",
        },
    )
    return user_id


@pytest.mark.asyncio
async def test_resolve_summary_model_reads_settings_and_validates_activity(
    postgres_database_url: str,
) -> None:
    """未配置返回 None；活跃模型回传引用；失效/缺失/畸形引用抛类型化错误。"""

    engine = create_async_engine(postgres_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    port = registry_model_port()
    try:
        # 设置行尚未播种：视为未配置。
        async with factory() as session:
            assert await port.resolve_summary_model(session) is None

        model_id = uuid.uuid4()
        async with factory() as session, session.begin():
            owner = await _seed_user(session, "resolve")
            await seed_system_model_config(
                session,
                model_id=model_id,
                owner_user_id=owner,
                display_name="摘要模型",
                provider_model="test/summary-model",
            )
            session.add(KnowledgeSystemSettingsRow(id=1))

        # 行存在但 summary_model_name 为空：仍是未配置。
        async with factory() as session:
            assert await port.resolve_summary_model(session) is None

        async with factory() as session, session.begin():
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None
            row.summary_model_name = str(model_id)

        async with factory() as session:
            assert await port.resolve_summary_model(session) == str(model_id)

        # 配置了但模型被停用：类型化拒绝，不静默降级。
        async with factory() as session, session.begin():
            await session.execute(sa.update(SystemModelConfigRow).where(SystemModelConfigRow.id == model_id).values(status="suspended"))
        async with factory() as session:
            with pytest.raises(KnowledgeError) as suspended:
                await port.resolve_summary_model(session)
            assert suspended.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        # 配置指向不存在的模型。
        async with factory() as session, session.begin():
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None
            row.summary_model_name = str(uuid.uuid4())
        async with factory() as session:
            with pytest.raises(KnowledgeError) as missing:
                await port.resolve_summary_model(session)
            assert missing.value.code == KNOWLEDGE_MODEL_UNAVAILABLE

        # 畸形（非 UUID）引用同样类型化拒绝。
        async with factory() as session, session.begin():
            row = await session.get(KnowledgeSystemSettingsRow, 1)
            assert row is not None
            row.summary_model_name = "not-a-uuid"
        async with factory() as session:
            with pytest.raises(KnowledgeError) as malformed:
                await port.resolve_summary_model(session)
            assert malformed.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_generate_summary_uses_private_runtime_and_untrusted_human_message() -> None:
    runtime = AsyncMock()
    runtime.ainvoke.return_value = AIMessage(content="生成的摘要")
    port = RegistryKnowledgeModelPort(secret_key=registry_secret_key(), model_runtime=runtime)
    before = time.monotonic()
    result = await port.generate_summary(model_ref="model-reference", prompt="不可信的源内容")
    assert result == "生成的摘要"
    args, kwargs = runtime.ainvoke.call_args
    assert len(args[0]) == 1 and isinstance(args[0][0], HumanMessage)
    assert args[0][0].content == "不可信的源内容"
    assert kwargs["profile"] is ModelRuntimeProfile.PRIVATE_ONESHOT
    assert kwargs["model_name"] == "model-reference"
    assert kwargs["model_overrides"] == {"max_tokens": 1024}
    assert kwargs["provider_max_retries"] == 0
    assert before + 120 <= kwargs["deadline_monotonic"] <= time.monotonic() + 120


@pytest.mark.asyncio
async def test_generate_summary_sanitizes_provider_failure() -> None:
    runtime = AsyncMock()
    runtime.ainvoke.side_effect = RuntimeError("secret provider payload")
    port = RegistryKnowledgeModelPort(secret_key=registry_secret_key(), model_runtime=runtime)
    with pytest.raises(KnowledgeError) as caught:
        await port.generate_summary(model_ref="model-reference", prompt="source")
    assert caught.value.code == "KNOWLEDGE_TASK_FAILED"
    assert caught.value.message == "摘要生成失败"
    assert caught.value.__suppress_context__


@pytest.mark.asyncio
async def test_database_runtime_materializes_current_model_each_call(monkeypatch):
    import app.knowledge.summary_runtime as summary_runtime

    models = [SimpleNamespace(name="active-model-one"), SimpleNamespace(name="active-model-two")]
    materializer = SimpleNamespace(materialize_active=AsyncMock(side_effect=models))
    monkeypatch.setattr(summary_runtime, "SystemModelMaterializer", lambda factory: materializer)
    runtime = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="summary")))
    runtime_configs = []
    monkeypatch.setattr(summary_runtime, "ModelRuntime", lambda *, app_config: runtime_configs.append(app_config) or runtime)
    config = SimpleNamespace(with_runtime_models=lambda values: values)
    adapter = summary_runtime.DatabaseKnowledgeSummaryRuntime(app_config=config, session_factory=object())
    port = RegistryKnowledgeModelPort(secret_key=registry_secret_key(), model_runtime=adapter)
    await port.generate_summary(model_ref="configured-id", prompt="source")
    await port.generate_summary(model_ref="configured-id", prompt="source")
    assert materializer.materialize_active.await_count == 2
    assert runtime_configs == [(models[0],), (models[1],)]
    assert [call.kwargs["model_name"] for call in runtime.ainvoke.await_args_list] == [model.name for model in models]
    assert all(call.kwargs["provider_max_retries"] == 0 for call in runtime.ainvoke.await_args_list)


@pytest.mark.asyncio
async def test_database_runtime_unavailable_model_stays_safe_typed_error(monkeypatch):
    import app.knowledge.summary_runtime as summary_runtime
    from app.system_settings.execution_adapter import SystemModelMaterializationUnavailable

    materializer = SimpleNamespace(materialize_active=AsyncMock(side_effect=SystemModelMaterializationUnavailable()))
    monkeypatch.setattr(summary_runtime, "SystemModelMaterializer", lambda factory: materializer)
    adapter = summary_runtime.DatabaseKnowledgeSummaryRuntime(app_config=object(), session_factory=object())
    port = RegistryKnowledgeModelPort(secret_key=registry_secret_key(), model_runtime=adapter)
    with pytest.raises(KnowledgeError) as caught:
        await port.generate_summary(model_ref="suspended-id", prompt="source")
    assert caught.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
