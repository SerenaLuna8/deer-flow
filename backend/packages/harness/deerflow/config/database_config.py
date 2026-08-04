"""PostgreSQL-only database configuration."""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

CheckpointChannelMode = Literal["full", "delta"]
DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY = 10


class CheckpointDeltaConfig(BaseModel):
    """Restart-required tuning for the DeltaChannel representation."""

    model_config = ConfigDict(extra="forbid")

    snapshot_frequency: int = Field(
        default=DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
        ge=1,
        description=("Store a complete messages seed every N delta writes. The value is compiled into the graph and must match in every Gateway and Worker."),
    )


class DatabaseConfig(BaseModel):
    """Connection settings shared by ActWeave persistence and LangGraph."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(repr=False)
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_timeout_seconds: int = Field(default=30, ge=1)
    statement_timeout_seconds: int = Field(default=30, ge=1)
    checkpoint_channel_mode: CheckpointChannelMode = Field(
        default="full",
        description=("Checkpoint representation. 'full' stores whole message values; 'delta' stores incremental message writes. Restart every Gateway and Worker together when changing this value."),
    )
    checkpoint_delta: CheckpointDeltaConfig = Field(
        default_factory=CheckpointDeltaConfig,
        description="Delta checkpoint tuning; ignored while checkpoint_channel_mode is full.",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_snapshot_frequency(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "checkpoint_delta_snapshot_frequency" not in data:
            return data
        migrated = dict(data)
        legacy_value = migrated.pop("checkpoint_delta_snapshot_frequency")
        nested = migrated.get("checkpoint_delta")
        if isinstance(nested, dict):
            if "snapshot_frequency" in nested:
                logger.warning("Both database.checkpoint_delta_snapshot_frequency and database.checkpoint_delta.snapshot_frequency are set; the nested value wins.")
                return migrated
            migrated["checkpoint_delta"] = {
                **nested,
                "snapshot_frequency": legacy_value,
            }
        elif nested is None:
            migrated["checkpoint_delta"] = {
                "snapshot_frequency": legacy_value,
            }
        else:
            logger.warning("Ignoring deprecated database.checkpoint_delta_snapshot_frequency because database.checkpoint_delta is already set.")
            return migrated
        logger.warning("database.checkpoint_delta_snapshot_frequency is deprecated; use database.checkpoint_delta.snapshot_frequency.")
        return migrated

    @model_validator(mode="before")
    @classmethod
    def _default_url_from_environment(cls, data: Any) -> Any:
        if isinstance(data, dict) and "url" not in data:
            database_url = os.getenv("DATABASE_URL")
            if database_url is not None:
                return {**data, "url": database_url}
        return data

    @field_validator("url")
    @classmethod
    def _validate_postgres_url(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("database.url must be a PostgreSQL URL using postgresql:// or postgresql+asyncpg://")
        return value

    @property
    def sqlalchemy_url(self) -> str:
        """Return the PostgreSQL URL expected by SQLAlchemy's async engine."""
        if self.url.startswith("postgresql+asyncpg://"):
            return self.url
        return self.url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def checkpointer_url(self) -> str:
        """Return the driver-neutral PostgreSQL URL expected by the checkpointer."""
        if self.url.startswith("postgresql+asyncpg://"):
            return self.url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return self.url
