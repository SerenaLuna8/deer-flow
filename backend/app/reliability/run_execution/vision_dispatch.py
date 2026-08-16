"""Application-owned authority for one frozen real Vision Bridge model."""

from __future__ import annotations

import asyncio
import uuid
from typing import Protocol

from app.reliability.run_execution.ports import SystemModelMaterializationPort
from deerflow.config.model_config import ModelConfig
from deerflow.vision.contracts import VisionUsageReceipt
from deerflow.vision.dispatch import (
    VisionDispatchAttempt,
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
    async def before_vision_dispatch(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> None: ...

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
        self._attempt_lock = asyncio.Lock()
        self._open_attempts: set[VisionDispatchAttempt] = set()

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

    async def before_attempt(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> VisionDispatchAttempt:
        if type(normalized_bytes) is not int or normalized_bytes < 1 or type(normalized_pixels) is not int or normalized_pixels < 1:
            raise VisionDispatchDenied("VISION_CONFIGURATION_ERROR")
        await self._revalidate_model()
        # This is the first point at which bytes can leave the Worker. The
        # durable Run aggregate and ambiguity fence commit here, not at tool
        # entry and not in process-local counters.
        await self._boundary.before_vision_dispatch(
            normalized_bytes=normalized_bytes,
            normalized_pixels=normalized_pixels,
        )
        async with self._attempt_lock:
            attempt = VisionDispatchAttempt()
            self._open_attempts.add(attempt)
            return attempt

    async def after_attempt(
        self,
        *,
        attempt: VisionDispatchAttempt,
        usage_receipt: VisionUsageReceipt,
        error_code: str | None,
    ) -> None:
        if (
            not isinstance(attempt, VisionDispatchAttempt)
            or not isinstance(usage_receipt, VisionUsageReceipt)
            or usage_receipt.call_count not in {0, 1}
            or usage_receipt.request_dispatched is not (usage_receipt.call_count == 1)
            or (error_code is not None and (type(error_code) is not str or not error_code))
        ):
            raise VisionDispatchDenied("VISION_CONFIGURATION_ERROR")
        async with self._attempt_lock:
            if attempt not in self._open_attempts:
                raise VisionDispatchDenied("VISION_CONFIGURATION_ERROR")
            self._open_attempts.remove(attempt)
        # Credential revocation is the emergency stop authority.  A response
        # produced after revocation is not returned to the lead model.
        await self._revalidate_model()
        await self._boundary.after_vision_dispatch()


__all__ = ["PrivateRunVisionDispatchAuthority"]
