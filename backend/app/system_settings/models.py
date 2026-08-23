"""Secret-safe application contracts for the stable System Model catalog."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from deerflow.config.model_execution import FrozenSystemModelExecution
from deerflow.persistence.system_settings import (
    SystemModelConfigRow,
    SystemModelSecretGenerationRow,
)


@dataclass(frozen=True, slots=True)
class CreateSystemModel:
    display_name: str
    status: str
    provider_adapter: str
    provider_model: str
    max_input_tokens: int
    settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class UpdateSystemModel:
    display_name: str
    provider_adapter: str
    provider_model: str
    max_input_tokens: int
    settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class SystemModelConnectionCheck:
    """Validated transient inputs for one administrator connection check."""

    provider_adapter: str
    provider_model: str
    max_input_tokens: int
    settings: Mapping[str, object]
    supports_vision: bool
    api_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SystemModelView:
    id: uuid.UUID
    display_name: str
    status: str
    provider_adapter: str
    provider_model: str
    max_input_tokens: int
    settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    payload_checksum: str
    api_key_configured: bool
    secret_readiness: str
    secret_revision: int
    revision: int
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PublicSystemModelView:
    model_ref: str
    display_name: str
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    supports_vision_bridge: bool
    is_default: bool


@dataclass(frozen=True, slots=True)
class SystemModelCatalogStateView:
    revision: int
    default_model_config_id: uuid.UUID | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SystemModelCatalogView:
    catalog_revision: int
    default_model_config_id: uuid.UUID | None
    items: tuple[SystemModelView, ...]


@dataclass(frozen=True, slots=True)
class RunModelConfigSnapshotView:
    project_id: uuid.UUID
    owner_user_id: str
    thread_id: str
    run_id: str
    purpose: str
    model_ref: str
    provider_adapter: str
    provider_model: str
    max_input_tokens: int
    provider_settings: Mapping[str, object]
    model_config_id: uuid.UUID
    payload_checksum: str
    secret_generation_id: uuid.UUID | None
    secret_envelope_digest: str | None
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LockedSystemModelMaterial:
    """Exact execution payload with protected bytes hidden from serialization."""

    model: SystemModelConfigRow
    secret_generation: SystemModelSecretGenerationRow | None = field(
        default=None,
        repr=False,
    )
    execution: FrozenSystemModelExecution | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ConnectionTestSystemModelMaterial:
    """Transient administrator-provided Key; never persisted or returned."""

    command: SystemModelConnectionCheck = field(repr=False)


__all__ = [
    "ConnectionTestSystemModelMaterial",
    "CreateSystemModel",
    "FrozenSystemModelExecution",
    "LockedSystemModelMaterial",
    "PublicSystemModelView",
    "RunModelConfigSnapshotView",
    "SystemModelCatalogStateView",
    "SystemModelCatalogView",
    "SystemModelView",
    "SystemModelConnectionCheck",
    "UpdateSystemModel",
]
