"""Secret-safe application contracts for the system model catalog."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from deerflow.persistence.shared_assets import (
    CredentialEnvelopeRow,
    CredentialRow,
    CredentialVersionRow,
)
from deerflow.persistence.system_settings import (
    RunModelConfigSnapshotRow,
    SystemModelConfigRow,
    SystemModelConfigVersionRow,
)


@dataclass(frozen=True, slots=True)
class CreateSystemModel:
    logical_name: str
    display_name: str
    description: str
    status: str
    provider_adapter: str
    provider_model: str
    settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_env_key: str | None
    sort_order: int = 0


@dataclass(frozen=True, slots=True)
class UpdateSystemModel:
    display_name: str
    description: str
    provider_adapter: str
    provider_model: str
    settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_env_key: str | None
    sort_order: int


@dataclass(frozen=True, slots=True)
class SystemModelVersionView:
    id: uuid.UUID
    model_config_id: uuid.UUID
    version_number: int
    provider_adapter: str
    provider_model: str
    settings: Mapping[str, object]
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_env_key: str | None
    payload_checksum: str
    supersedes_version_id: uuid.UUID | None
    created_by_user_id: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SystemModelView:
    id: uuid.UUID
    logical_name: str
    display_name: str
    description: str
    status: str
    current_version_id: uuid.UUID
    revision: int
    sort_order: int
    current_version: SystemModelVersionView
    created_by_user_id: str
    updated_by_user_id: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PublicSystemModelView:
    logical_name: str
    display_name: str
    description: str
    supports_thinking: bool
    supports_reasoning_effort: bool
    supports_vision: bool
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
    logical_name: str
    model_config_id: uuid.UUID
    model_config_version_id: uuid.UUID
    payload_checksum: str
    credential_id: uuid.UUID | None
    credential_version_id: uuid.UUID | None
    credential_env_key: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class LockedSystemModelMaterial:
    """Exact material with envelope bytes hidden from repr and serialization."""

    model: SystemModelConfigRow
    version: SystemModelConfigVersionRow
    credential: CredentialRow | None = field(default=None, repr=False)
    credential_version: CredentialVersionRow | None = field(
        default=None,
        repr=False,
    )
    envelope: CredentialEnvelopeRow | None = field(default=None, repr=False)
    snapshot: RunModelConfigSnapshotRow | None = field(default=None, repr=False)


__all__ = [
    "CreateSystemModel",
    "LockedSystemModelMaterial",
    "PublicSystemModelView",
    "RunModelConfigSnapshotView",
    "SystemModelCatalogStateView",
    "SystemModelCatalogView",
    "SystemModelVersionView",
    "SystemModelView",
    "UpdateSystemModel",
]
