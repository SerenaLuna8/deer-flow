"""Gateway-only no-tool model callers backed by the database catalog."""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from threading import Event

from app.system_settings import SystemModelMaterializer
from deerflow.config.app_config import AppConfig
from deerflow.config.model_config import ModelConfig
from deerflow.utils.oneshot_llm import run_oneshot_llm
from deerflow.vision.client import build_vision_evidence_client
from deerflow.vision.compatibility import (
    VISION_BRIDGE_CONTRACT_V1,
    resolve_materialized_vision_bridge_protocol,
)

# Platform-generated 64x64 blue-square PNG. It contains no user/project data
# and exists only to prove that an administrator-supplied endpoint supports the
# exact image and strict structured-output profile, rather than merely accepting
# text chat.
_VISION_CONNECTION_PROBE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAIAAAAlC+aJAAAAlklEQVR4nO3awQmEUAAD0XGwBGuwMsuyMmuwh28NIosM67sHArlmGmNQJnESJ3ESJ3ESJ3ESJ3ESJ3ESJ3ESJ3Hz3cC6nfzSsS//tYDESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESZzESdz0/YVeJnESJ3ESJ3ESJ3ESJ3ESJ3ESJ3G+XeCpCyOACXsXBLW8AAAAAElFTkSuQmCC"
)


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
        model_ref: str | None = None,
    ) -> str:
        model = await self.materializer.materialize_active(
            model_ref if model_ref is not None else self.model_ref,
        )
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
            if getattr(model, "supports_vision", False) is True:
                protocol = resolve_materialized_vision_bridge_protocol(
                    model,
                    VISION_BRIDGE_CONTRACT_V1,
                )
                if protocol is None:
                    return False
                client = build_vision_evidence_client(
                    model,
                    VISION_BRIDGE_CONTRACT_V1,
                    transient_gate_key="admin-vision-connection-test",
                )
                deadline = time.monotonic() + self.timeout_seconds
                await asyncio.wait_for(
                    client.analyze(
                        image_bytes=_VISION_CONNECTION_PROBE_PNG,
                        mime_type="image/png",
                        mode="auto",
                        deadline_monotonic=deadline,
                        abort_signal=Event(),
                    ),
                    timeout=self.timeout_seconds,
                )
                return True
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
