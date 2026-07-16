from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QuotaConfig(BaseModel):
    """Platform defaults and deployment ceilings for project quotas."""

    model_config = ConfigDict(extra="forbid")

    default_member_limit: int = Field(default=20, ge=1)
    default_storage_bytes_limit: int = Field(default=5_368_709_120, ge=0)
    default_concurrent_run_limit: int = Field(default=3, ge=1)
    default_mcp_calls_daily_limit: int = Field(default=10_000, ge=0)
    max_member_limit: int = Field(default=10_000, ge=1)
    max_storage_bytes_limit: int = Field(default=10_995_116_277_760, ge=0)
    max_concurrent_run_limit: int = Field(default=1_024, ge=1)
    max_mcp_calls_daily_limit: int = Field(default=100_000_000, ge=0)
    warning_threshold: float = Field(default=0.8, gt=0, lt=1)

    @model_validator(mode="after")
    def validate_platform_defaults(self) -> Self:
        pairs = (
            (self.default_member_limit, self.max_member_limit, "member"),
            (self.default_storage_bytes_limit, self.max_storage_bytes_limit, "storage"),
            (self.default_concurrent_run_limit, self.max_concurrent_run_limit, "concurrent run"),
            (self.default_mcp_calls_daily_limit, self.max_mcp_calls_daily_limit, "MCP daily"),
        )
        for default, maximum, name in pairs:
            if default > maximum:
                raise ValueError(f"default {name} quota must not exceed its deployment maximum")
        return self


__all__ = ["QuotaConfig"]
