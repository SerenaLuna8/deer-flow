"""Runtime-only configuration for the text-model Vision Bridge."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS = 60


class VisionBridgeConfig(BaseModel):
    """One frozen Vision Bridge selection materialized for a Run.

    ``model_name`` is intentionally the only enablement signal.  This object is
    populated from PostgreSQL Runtime Policy and is rejected in ``config.yaml``.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    model_name: str | None = None
    timeout_seconds: int = Field(
        default=DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS,
        ge=5,
        le=120,
    )
    contract_version: Literal["vision.bridge.v1"] = "vision.bridge.v1"


__all__ = ["DEFAULT_VISION_BRIDGE_TIMEOUT_SECONDS", "VisionBridgeConfig"]
