"""Strict public schemas for project-scoped IM channel APIs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.gateway.private_work_schemas import StrictPrivateWorkRequest


class StrictChannelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectConnectionProviderResponse(StrictChannelResponse):
    provider: str
    display_name: str
    enabled: bool
    configured: bool
    connectable: bool
    unavailable_reason: str | None = None
    auth_mode: Literal["deep_link", "binding_code"]
    connection_status: str


class ProjectConnectionProvidersResponse(StrictChannelResponse):
    enabled: bool
    providers: list[ProjectConnectionProviderResponse]


class ProjectConnectionResponse(StrictChannelResponse):
    id: str
    provider: str
    status: str
    external_account_id: str | None = None
    external_account_name: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProjectConnectionsResponse(StrictChannelResponse):
    connections: list[ProjectConnectionResponse]


class ProjectConnectRequest(StrictPrivateWorkRequest):
    agent_asset_id: uuid.UUID
    agent_scope: Literal["project", "system"]
    redirect_after: str | None = None


class ProjectConnectResponse(StrictChannelResponse):
    provider: str
    mode: Literal["deep_link", "binding_code"]
    url: str | None = None
    code: str
    instruction: str
    expires_in: int


class ProjectChannelInstanceConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    public_config: dict[str, str] = Field(default_factory=dict)
    credentials: dict[str, str] = Field(default_factory=dict, repr=False)
    enabled: bool


class ProjectChannelInstanceResponse(StrictChannelResponse):
    id: uuid.UUID | None
    provider: str
    display_name: str
    status: Literal["unconfigured", "disabled", "stopped", "starting", "running", "error"]
    enabled: bool
    configured: bool
    credential_configured: bool
    public_config: dict[str, str] = Field(default_factory=dict)
    updated_at: datetime | None
    last_error: str | None = None


class ProjectChannelInstancesResponse(StrictChannelResponse):
    instances: list[ProjectChannelInstanceResponse]


PROJECT_CONNECTION_PROVIDER_META: dict[str, dict[str, str]] = {
    "telegram": {"display_name": "Telegram", "auth_mode": "deep_link"},
    "slack": {"display_name": "Slack", "auth_mode": "binding_code"},
    "discord": {"display_name": "Discord", "auth_mode": "binding_code"},
    "feishu": {"display_name": "Feishu", "auth_mode": "binding_code"},
    "dingtalk": {"display_name": "DingTalk", "auth_mode": "binding_code"},
    "wechat": {"display_name": "WeChat", "auth_mode": "binding_code"},
    "wecom": {"display_name": "WeCom", "auth_mode": "binding_code"},
}


PROJECT_CONNECTION_RUNTIME_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "telegram": ("bot_token",),
    "slack": ("bot_token", "app_token"),
    "discord": ("bot_token",),
    "feishu": ("app_id", "app_secret"),
    "dingtalk": ("client_id", "client_secret"),
    "wechat": ("bot_token",),
    "wecom": ("bot_id", "bot_secret"),
}


def project_connect_instruction(provider: str, code: str) -> str:
    if provider == "telegram":
        return f"Send /start {code} to the ActWeave Telegram bot."
    meta = PROJECT_CONNECTION_PROVIDER_META.get(provider)
    if meta is None:
        raise KeyError(provider)
    return f"Send /connect {code} to the ActWeave {meta['display_name']} bot."


__all__ = [
    "PROJECT_CONNECTION_PROVIDER_META",
    "PROJECT_CONNECTION_RUNTIME_REQUIREMENTS",
    "ProjectChannelInstanceConfigureRequest",
    "ProjectChannelInstanceResponse",
    "ProjectChannelInstancesResponse",
    "ProjectConnectRequest",
    "ProjectConnectResponse",
    "ProjectConnectionProviderResponse",
    "ProjectConnectionProvidersResponse",
    "ProjectConnectionResponse",
    "ProjectConnectionsResponse",
    "project_connect_instruction",
]
