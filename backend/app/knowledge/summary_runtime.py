"""Per-call System Model materialization for private Knowledge summaries."""

from __future__ import annotations

from collections.abc import Mapping

from actweave_knowledge import KNOWLEDGE_MODEL_UNAVAILABLE, KnowledgeError
from langchain_core.messages import BaseMessage, HumanMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.system_settings import SystemModelMaterializer
from app.system_settings.execution_adapter import SystemModelMaterializationUnavailable
from deerflow.config.app_config import AppConfig
from deerflow.models.runtime import ModelRuntime, ModelRuntimeProfile


class DatabaseKnowledgeSummaryRuntime:
    """Resolve only the active model needed now, never cache catalog secrets."""

    def __init__(self, *, app_config: AppConfig, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._app_config = app_config
        self._materializer = SystemModelMaterializer(session_factory)

    async def ainvoke(
        self,
        messages: list[HumanMessage],
        *,
        profile: ModelRuntimeProfile,
        model_name: str,
        model_overrides: Mapping[str, object],
        provider_max_retries: int,
        deadline_monotonic: float,
    ) -> BaseMessage:
        try:
            model = await self._materializer.materialize_active(model_name)
        except SystemModelMaterializationUnavailable:
            raise KnowledgeError(KNOWLEDGE_MODEL_UNAVAILABLE, "摘要模型不存在或已停用") from None
        # Materializer has closed its transaction before any provider request.
        runtime = ModelRuntime(app_config=self._app_config.with_runtime_models((model,)))
        return await runtime.ainvoke(
            messages,
            profile=profile,
            model_name=model.name,
            model_overrides=model_overrides,
            provider_max_retries=provider_max_retries,
            deadline_monotonic=deadline_monotonic,
        )
