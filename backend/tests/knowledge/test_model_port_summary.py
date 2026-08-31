"""M11 T1 gates: RegistryKnowledgeModelPort summary-model resolution.

``resolve_summary_model`` reads the host ``knowledge_system_settings``
singleton and validates the referenced System Model against a real
PostgreSQL catalog installed from ``full_schema.sql``; ``generate_summary``
stays a frozen signature that rejects unconfigured runtimes with the typed
``KNOWLEDGE_MODEL_UNAVAILABLE`` error until the pipeline task wires the
ModelRuntime dispatch.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from actweave_knowledge import KNOWLEDGE_MODEL_UNAVAILABLE, KnowledgeError
from registry_helpers import registry_model_port
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from support.system_model_seed import seed_system_model_config

from deerflow.persistence.bootstrap import _install_full_schema
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
        await _install_full_schema(engine)

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
async def test_generate_summary_requires_configured_runtime() -> None:
    """T1 冻结签名：未注入 ModelRuntime 的端口以类型化错误拒绝生成调用。"""

    port = registry_model_port()
    with pytest.raises(KnowledgeError) as rejected:
        await port.generate_summary(model_ref=str(uuid.uuid4()), prompt="总结这段内容")
    assert rejected.value.code == KNOWLEDGE_MODEL_UNAVAILABLE
