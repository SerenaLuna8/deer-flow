from pydantic import BaseModel, ConfigDict, Field


class QuotaConfig(BaseModel):
    """Platform defaults and warning policy for project quotas."""

    model_config = ConfigDict(extra="forbid")

    default_member_limit: int = Field(default=20, ge=1)
    default_storage_bytes_limit: int = Field(default=5_368_709_120, ge=0)
    default_concurrent_run_limit: int = Field(default=3, ge=1)
    default_mcp_calls_daily_limit: int = Field(default=10_000, ge=0)
    warning_threshold: float = Field(default=0.8, gt=0, lt=1)


__all__ = ["QuotaConfig"]
