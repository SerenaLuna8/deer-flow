"""Gateway-only no-tool model callers backed by the database catalog."""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from app.system_settings import SystemModelMaterializer
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.config.model_execution import FrozenSystemModelExecution
from deerflow.models import ModelRuntime, ModelRuntimeProfile
from deerflow.models.runtime import AsyncAbortEvent
from deerflow.utils.oneshot_llm import run_oneshot_llm

# Platform-generated 64x64 blue-square PNG. It contains no user/project data
# and exists only to prove that an administrator-supplied endpoint supports the
# selected System Model's standard multimodal message path, rather than merely
# accepting text chat.
_VISION_CONNECTION_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAlklEQVR4nO3awQmEUAAD0XGwBGuwMsuyMmuwh28NIosM67sHArlmGmNQJnESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3Hz3cC6nfzSsS//tYDESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESdz0/YVeJnESJ3ESJ3ESJ3ESJ3ESJ3ESJ3G+XeCpCyOACXsXBLW8AAAAAElFTkSuQmCC"
)


@dataclass(frozen=True, slots=True)
class DatabaseOneshotModelCaller:
    """Materialize an admitted execution snapshot, or the current configuration."""

    app_config: AppConfig
    materializer: SystemModelMaterializer
    run_name: str
    model_ref: str | None = None

    async def __call__(
        self,
        *,
        system_instruction: str,
        user_content: str,
        model_ref: str | None = None,
        model_execution: FrozenSystemModelExecution | None = None,
        thinking_enabled: bool = False,
        reasoning_effort: str | None = None,
        abort_event: AsyncAbortEvent | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        effective_ref = model_ref if model_ref is not None else self.model_ref
        if model_execution is not None:
            model = await self.materializer.materialize_frozen(
                model_execution,
            )
        else:
            model = await self.materializer.materialize_active(effective_ref)
        runtime_config = self.app_config.with_runtime_models((model,))
        return await run_oneshot_llm(
            system_instruction=system_instruction,
            user_content=user_content,
            run_name=self.run_name,
            app_config=runtime_config,
            model_name=model.name,
            thinking_enabled=thinking_enabled,
            reasoning_effort=reasoning_effort,
            abort_event=abort_event,
            on_reasoning_delta=on_reasoning_delta,
            profile=ModelRuntimeProfile.PRIVATE_ONESHOT,
        )


@dataclass(frozen=True, slots=True)
class ModelConnectionTester:
    """Run one bounded, untraced model call without creating a Run or a model."""

    app_config: AppConfig
    timeout_seconds: float = 20.0

    async def test(self, model: ModelConfig) -> bool:
        try:
            runtime_config = self.app_config.with_runtime_models((model,))
            runtime = ModelRuntime(app_config=runtime_config)
            if getattr(model, "supports_vision", False) is True:
                user_content: str | list[dict[str, str]] = [
                    {
                        "type": "text",
                        "text": ("Inspect this platform-generated image and reply with OK."),
                    },
                    {
                        "type": "image",
                        "base64": base64.b64encode(_VISION_CONNECTION_PROBE_PNG).decode("ascii"),
                        "mime_type": "image/png",
                    },
                ]
            else:
                user_content = "OK"
            await runtime.ainvoke(
                [
                    SystemMessage(content="You are a connectivity probe. Reply with OK."),
                    HumanMessage(content=user_content),
                ],
                profile=ModelRuntimeProfile.ADMIN_PROBE,
                model_name=model.name,
                config={"run_name": "admin_model_connection_test"},
                deadline_monotonic=time.monotonic() + self.timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - provider failures are intentionally opaque
            return False
        return True


__all__ = ["DatabaseOneshotModelCaller", "ModelConnectionTester"]
