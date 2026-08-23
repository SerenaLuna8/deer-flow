"""Cycle-free checkpoint key protocol for provider-request metering."""

from typing import Final

PROVIDER_REQUEST_PROFILE_STATE_KEY: Final[str] = "provider_request_profile"
PROVIDER_REQUEST_MEASUREMENT_STATE_KEY: Final[str] = "provider_request_measurement"

__all__ = [
    "PROVIDER_REQUEST_MEASUREMENT_STATE_KEY",
    "PROVIDER_REQUEST_PROFILE_STATE_KEY",
]
