"""Gateway-only no-tool model callers backed by the database catalog."""

from __future__ import annotations

from dataclasses import dataclass

from app.system_settings import SystemModelMaterializer
from deerflow.config.app_config import AppConfig
from deerflow.utils.oneshot_llm import run_oneshot_llm


@dataclass(frozen=True, slots=True)
class DatabaseOneshotModelCaller:
    """Materialize the current default model immediately before one call."""

    app_config: AppConfig
    materializer: SystemModelMaterializer
    run_name: str
    model_ref: str | None = None

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
    ) -> str:
        model = await self.materializer.materialize_active(self.model_ref)
        runtime_config = self.app_config.with_runtime_models((model,))
        return await run_oneshot_llm(
            system_instruction=system_instruction,
            user_content=user_content,
            run_name=self.run_name,
            app_config=runtime_config,
            model_name=model.name,
            thread_id=None,
            attach_tracing=False,
        )


__all__ = ["DatabaseOneshotModelCaller"]
