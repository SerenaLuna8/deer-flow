from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ConfigureProjectChannelInstance:
    display_name: str | None
    public_config: Mapping[str, str]
    credentials: Mapping[str, str] = field(repr=False)
    enabled: bool


@dataclass(frozen=True)
class ProjectChannelInstanceView:
    id: uuid.UUID | None
    provider: str
    display_name: str
    status: str
    enabled: bool
    configured: bool
    credential_configured: bool
    public_config: Mapping[str, str]
    updated_at: datetime | None
    last_error: str | None


__all__ = [
    "ConfigureProjectChannelInstance",
    "ProjectChannelInstanceView",
]
