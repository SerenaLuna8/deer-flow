"""Application-owned authority for one frozen real Vision Bridge model."""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from app.reliability.run_execution.ports import SystemModelMaterializationPort
from deerflow.config.model_config import ModelConfig
from deerflow.vision.dispatch import (
    MAX_VISION_CALLS_PER_RUN,
    MAX_VISION_NORMALIZED_BYTES_PER_RUN,
    MAX_VISION_NORMALIZED_PIXELS_PER_RUN,
    VisionDispatchDenied,
)


def _exact_model_identity(model: ModelConfig) -> tuple[object, ...]:
    return (
        model.name,
        model._system_model_config_version_id,
        model.system_provider_adapter,
        model.model,
        getattr(model, "base_url", None),
    )


class _VisionBoundaryPort(Protocol):
    async def before_vision_dispatch(self) -> None: ...

    async def after_vision_dispatch(self) -> None: ...


class PrivateRunVisionDispatchAuthority:
    """Revalidate the exact Credential/model snapshot around each HTTP call."""

    def __init__(
        self,
        *,
        boundary: _VisionBoundaryPort,
        materializer: SystemModelMaterializationPort,
        project_id: uuid.UUID,
        owner_user_id: str,
        run_id: str,
        expected_model: ModelConfig,
    ) -> None:
        self._boundary = boundary
        self._materializer = materializer
        self._project_id = project_id
        self._owner_user_id = owner_user_id
        self._run_id = run_id
        self._expected_identity = _exact_model_identity(expected_model)
        self._budget_lock = asyncio.Lock()
        self._call_count = 0
        self._normalized_bytes = 0
        self._normalized_pixels = 0

    async def _revalidate_model(self) -> None:
        try:
            current = await self._materializer.materialize_snapshot(
                project_id=self._project_id,
                owner_user_id=self._owner_user_id,
                run_id=self._run_id,
                purpose="vision",
            )
        except Exception:
            raise VisionDispatchDenied from None
        if _exact_model_identity(current) != self._expected_identity:
            raise VisionDispatchDenied

    async def before_dispatch(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> None:
        if type(normalized_bytes) is not int or normalized_bytes < 1 or type(normalized_pixels) is not int or normalized_pixels < 1:
            raise VisionDispatchDenied("VISION_CONFIGURATION_ERROR")
        async with self._budget_lock:
            if self._call_count + 1 > MAX_VISION_CALLS_PER_RUN or self._normalized_bytes + normalized_bytes > MAX_VISION_NORMALIZED_BYTES_PER_RUN or self._normalized_pixels + normalized_pixels > MAX_VISION_NORMALIZED_PIXELS_PER_RUN:
                raise VisionDispatchDenied("VISION_RATE_LIMITED")
            await self._revalidate_model()
            # This is the first point at which bytes can leave the Worker.  The
            # durable ambiguity fence therefore belongs here, not at tool entry.
            await self._boundary.before_vision_dispatch()
            self._call_count += 1
            self._normalized_bytes += normalized_bytes
            self._normalized_pixels += normalized_pixels

    async def after_dispatch(self) -> None:
        # Credential revocation is the emergency stop authority.  A response
        # produced after revocation is not returned to the lead model.
        await self._revalidate_model()
        await self._boundary.after_vision_dispatch()


__all__ = ["PrivateRunVisionDispatchAuthority"]
