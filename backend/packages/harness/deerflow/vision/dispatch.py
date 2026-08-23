"""Opaque runtime protocol for an exact governed Vision API dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Protocol

from deerflow.vision.contracts import VisionUsageReceipt

MAX_VISION_CALLS_PER_RUN: Final = 8
MAX_VISION_NORMALIZED_BYTES_PER_RUN: Final = 40 * 1024 * 1024
MAX_VISION_NORMALIZED_PIXELS_PER_RUN: Final = 80_000_000
# Runtime Policy v2 through v4 retain these historical values only in their
# version-owned codecs. Current Agent tool admission uses the shared Run limit;
# this provider-dispatch cap remains a separate technical safety boundary.
VISION_TOOL_FREQUENCY_WARN: Final = 6
VISION_TOOL_FREQUENCY_HARD_STOP: Final = MAX_VISION_CALLS_PER_RUN + 1


class VisionDispatchDenied(RuntimeError):
    """Current Run authority or exact model material is no longer valid."""

    def __init__(self, code: str = "VISION_AUTH_FAILED") -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VisionDispatchAttempt:
    """Opaque server-owned handle for one governed provider attempt."""

    _token: object = field(default_factory=object, repr=False)


class VisionDispatchAuthority(Protocol):
    async def before_attempt(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> VisionDispatchAttempt: ...

    async def after_attempt(
        self,
        *,
        attempt: VisionDispatchAttempt,
        usage_receipt: VisionUsageReceipt,
        error_code: str | None,
    ) -> None: ...


__all__ = [
    "MAX_VISION_CALLS_PER_RUN",
    "MAX_VISION_NORMALIZED_BYTES_PER_RUN",
    "MAX_VISION_NORMALIZED_PIXELS_PER_RUN",
    "VISION_TOOL_FREQUENCY_HARD_STOP",
    "VISION_TOOL_FREQUENCY_WARN",
    "VisionDispatchAttempt",
    "VisionDispatchAuthority",
    "VisionDispatchDenied",
]
