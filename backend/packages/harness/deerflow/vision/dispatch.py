"""Opaque runtime protocol for an exact governed Vision API dispatch."""

from __future__ import annotations

from typing import Final, Protocol

MAX_VISION_CALLS_PER_RUN: Final = 8
MAX_VISION_NORMALIZED_BYTES_PER_RUN: Final = 40 * 1024 * 1024
MAX_VISION_NORMALIZED_PIXELS_PER_RUN: Final = 80_000_000


class VisionDispatchDenied(RuntimeError):
    """Current Run authority or exact model material is no longer valid."""

    def __init__(self, code: str = "VISION_AUTH_FAILED") -> None:
        self.code = code
        super().__init__(code)


class VisionDispatchAuthority(Protocol):
    async def before_dispatch(
        self,
        *,
        normalized_bytes: int,
        normalized_pixels: int,
    ) -> None: ...

    async def after_dispatch(self) -> None: ...


__all__ = [
    "MAX_VISION_CALLS_PER_RUN",
    "MAX_VISION_NORMALIZED_BYTES_PER_RUN",
    "MAX_VISION_NORMALIZED_PIXELS_PER_RUN",
    "VisionDispatchAuthority",
    "VisionDispatchDenied",
]
