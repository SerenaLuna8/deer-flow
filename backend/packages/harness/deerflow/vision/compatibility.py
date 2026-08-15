"""Versioned allow-list for Vision Bridge provider adapters."""

from typing import Final

VISION_BRIDGE_FAKE_ADAPTER: Final = "vision_bridge_fake"
VISION_BRIDGE_CONTRACT_V1: Final = "vision.bridge.v1"

# P1 deliberately admits only the deterministic fake adapter.  Real provider
# adapters must not be added here until their endpoint, redirect, retry,
# tracing, cancellation, response-size and structured-output profiles have
# dedicated conformance tests.
_COMPATIBLE_ADAPTER_CONTRACTS: Final = frozenset(
    {(VISION_BRIDGE_FAKE_ADAPTER, VISION_BRIDGE_CONTRACT_V1)},
)


def is_vision_bridge_adapter_compatible(
    provider_adapter: object,
    contract_version: object,
) -> bool:
    """Return whether an exact adapter/contract pair is approved."""

    return type(provider_adapter) is str and type(contract_version) is str and (provider_adapter, contract_version) in _COMPATIBLE_ADAPTER_CONTRACTS


__all__ = [
    "VISION_BRIDGE_CONTRACT_V1",
    "VISION_BRIDGE_FAKE_ADAPTER",
    "is_vision_bridge_adapter_compatible",
]
