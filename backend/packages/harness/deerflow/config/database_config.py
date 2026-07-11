"""PostgreSQL-only database configuration."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DatabaseConfig(BaseModel):
    """Connection settings shared by DeerFlow persistence and LangGraph."""

    model_config = ConfigDict(extra="forbid")

    url: str
    pool_size: int = Field(default=5, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_timeout_seconds: int = Field(default=30, ge=1)
    statement_timeout_seconds: int = Field(default=30, ge=1)

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
