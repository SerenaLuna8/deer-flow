"""Project-scoped IM channel instance configuration."""

from app.project_channels.errors import (
    ChannelInstanceConflict,
    ChannelInstanceForbidden,
    ChannelInstanceNotFound,
    ChannelInstanceStorageUnavailable,
    ChannelInstanceValidationFailed,
)
from app.project_channels.providers import (
    CHANNEL_PROVIDER_SPECS,
    NormalizedChannelConfiguration,
    validate_channel_configuration,
)

__all__ = [
    "CHANNEL_PROVIDER_SPECS",
    "ChannelInstanceConflict",
    "ChannelInstanceForbidden",
    "ChannelInstanceNotFound",
    "ChannelInstanceStorageUnavailable",
    "ChannelInstanceValidationFailed",
    "NormalizedChannelConfiguration",
    "validate_channel_configuration",
]
