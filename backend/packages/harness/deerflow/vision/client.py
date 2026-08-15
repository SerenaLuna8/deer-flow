"""Narrow Vision Evidence client interface and approved implementations."""

from __future__ import annotations

import asyncio
import io
import time
from collections.abc import Callable
from threading import Event
from typing import Protocol

from PIL import Image

from deerflow.config.model_config import ModelConfig
from deerflow.vision.compatibility import (
    VISION_BRIDGE_FAKE_ADAPTER,
    VISION_OPENAI_COMPATIBLE_V1_ADAPTER,
    is_vision_bridge_adapter_compatible,
)
from deerflow.vision.contracts import (
    VisionEvidence,
    VisionEvidenceItem,
    VisionInvocationResult,
    VisionUsageReceipt,
)
from deerflow.vision.prompt import VisionMode, render_vision_prompt_v1


class VisionClientError(RuntimeError):
    """One stable, content-free Vision client failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class VisionEvidenceClient(Protocol):
    requires_external_dispatch: bool

    async def analyze(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        mode: VisionMode,
        deadline_monotonic: float,
        abort_signal: Event,
    ) -> VisionInvocationResult: ...


class FakeVisionEvidenceClient:
    """Deterministic, non-networking P1 adapter for closed-loop tests."""

    requires_external_dispatch = False

    async def analyze(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        mode: VisionMode,
        deadline_monotonic: float,
        abort_signal: Event,
    ) -> VisionInvocationResult:
        render_vision_prompt_v1(mode)
        if abort_signal.is_set() or time.monotonic() >= deadline_monotonic:
            raise VisionClientError("VISION_DEADLINE_EXCEEDED")
        await asyncio.sleep(0)
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                width, height = image.size
        except (OSError, ValueError):
            raise VisionClientError("UNSUPPORTED_MEDIA") from None
        return VisionInvocationResult(
            evidence=VisionEvidence(
                summary=(f"The P1 fake Vision Bridge accepted one normalized {width} by {height} image."),
                evidence=[
                    VisionEvidenceItem(
                        kind="visual",
                        text=(f"A single normalized image reached the fixed {mode} analysis contract as {mime_type}."),
                        location="entire image",
                    )
                ],
                uncertainty=["The P1 fake adapter validates the pipeline but does not perform semantic recognition."],
                partial=True,
            ),
            usage_receipt=VisionUsageReceipt(
                call_count=1,
                request_dispatched=False,
            ),
        )


VisionClientFactory = Callable[[ModelConfig, str], VisionEvidenceClient]


def build_vision_evidence_client(
    model_config: ModelConfig,
    contract_version: str,
) -> VisionEvidenceClient:
    """Build only an explicitly compatible exact adapter/contract pair."""

    adapter = model_config.system_provider_adapter
    if not model_config.supports_vision or not is_vision_bridge_adapter_compatible(
        adapter,
        contract_version,
    ):
        raise VisionClientError("VISION_CONFIGURATION_ERROR")
    if adapter == VISION_BRIDGE_FAKE_ADAPTER:
        return FakeVisionEvidenceClient()
    if adapter == VISION_OPENAI_COMPATIBLE_V1_ADAPTER:
        from deerflow.config import is_tracing_enabled
        from deerflow.vision.openai_compatible import (
            OpenAICompatibleVisionError,
            OpenAICompatibleVisionEvidenceClient,
        )

        # Root graph callbacks can otherwise capture the image path, fixed
        # prompt, OCR and evidence.  Until selective tool redaction is proven,
        # fail closed rather than silently exporting that payload to a second
        # observability provider.
        if is_tracing_enabled():
            raise VisionClientError("DATA_POLICY_BLOCKED")
        try:
            return OpenAICompatibleVisionEvidenceClient(model_config)
        except OpenAICompatibleVisionError as error:
            raise VisionClientError(error.code) from None
    raise VisionClientError("VISION_CONFIGURATION_ERROR")


__all__ = [
    "FakeVisionEvidenceClient",
    "VisionClientError",
    "VisionClientFactory",
    "VisionEvidenceClient",
    "build_vision_evidence_client",
]
