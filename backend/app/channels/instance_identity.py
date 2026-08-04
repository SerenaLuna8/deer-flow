"""Stable identifiers for independently running channel instances."""

from __future__ import annotations


def legacy_channel_instance_id(provider: str) -> str:
    """Return the process-local identifier for an operator-configured channel.

    Project channel instances use their persisted UUID.  Historical
    ``config.yaml`` channels have no persisted instance row, so the provider
    name remains their explicit compatibility identifier.
    """

    if not isinstance(provider, str) or not provider.strip():
        raise TypeError("provider must be a non-empty string")
    return provider.strip()


def normalize_channel_instance_id(
    provider: str,
    channel_instance_id: str | None,
) -> str:
    """Return a non-empty runtime routing identifier."""

    if channel_instance_id is None:
        return legacy_channel_instance_id(provider)
    if not isinstance(channel_instance_id, str) or not channel_instance_id.strip():
        raise TypeError("channel_instance_id must be a non-empty string")
    return channel_instance_id.strip()


def persisted_channel_instance_id(
    provider: str,
    channel_instance_id: str,
) -> str | None:
    """Map the legacy process-local identifier to its nullable DB coordinate."""

    normalized = normalize_channel_instance_id(provider, channel_instance_id)
    if normalized == legacy_channel_instance_id(provider):
        return None
    return normalized
