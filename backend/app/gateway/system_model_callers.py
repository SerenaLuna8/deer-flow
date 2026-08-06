"""Gateway-only no-tool model callers backed by the database catalog."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.system_settings import SystemModelMaterializer
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
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


@dataclass(frozen=True, slots=True)
class ModelConnectionTester:
    """Run one bounded, untraced model call without creating a Run or a model."""

    app_config: AppConfig
    timeout_seconds: float = 20.0

    async def test(self, model: ModelConfig) -> bool:
        try:
            runtime_config = self.app_config.with_runtime_models((model,))
            await asyncio.wait_for(
                run_oneshot_llm(
                    system_instruction="You are a connectivity probe. Reply with OK.",
                    user_content="OK",
                    run_name="admin_model_connection_test",
                    app_config=runtime_config,
                    model_name=model.name,
                    thread_id=None,
                    attach_tracing=False,
                ),
                timeout=self.timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - provider failures are intentionally opaque
            return False
        return True


__all__ = ["DatabaseOneshotModelCaller", "ModelConnectionTester"]
