"""Resolve a governed Vision Bridge model to its configured wire protocol."""

from collections.abc import Mapping
from typing import Final
from urllib.parse import urlsplit

VISION_BRIDGE_FAKE_ADAPTER: Final = "vision_bridge_fake"
VISION_OPENAI_COMPATIBLE_V1_ADAPTER: Final = "vision_openai_compatible_v1"
VISION_BRIDGE_CONTRACT_V1: Final = "vision.bridge.v1"
VISION_BRIDGE_PROTOCOL_FAKE: Final = "fake"
VISION_BRIDGE_PROTOCOL_OPENAI_CHAT_COMPLETIONS: Final = "openai_chat_completions"
VISION_BRIDGE_PROTOCOL_OPENAI_RESPONSES: Final = "openai_responses"

_OPENAI_CHAT_COMPLETIONS_ADAPTERS: Final = frozenset(
    {
        "deepseek",
        "mindie",
        "openai",
        "patched_deepseek",
        "patched_mimo",
        "patched_minimax",
        "patched_openai",
        "patched_stepfun",
        "vllm",
        VISION_OPENAI_COMPATIBLE_V1_ADAPTER,
    }
)
_OPENAI_RESPONSES_ADAPTERS: Final = frozenset({"openai", "patched_openai"})


def _has_exact_https_base_url(settings: object) -> bool:
    if not isinstance(settings, Mapping):
        return False
    value = settings.get("base_url")
    if type(value) is not str:
        return False
    normalized = value.strip()
    try:
        parsed = urlsplit(normalized)
        return (
            parsed.scheme == "https"
            and bool(parsed.netloc)
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and "\\" not in normalized
            and not any(character.isspace() for character in normalized)
            and (parsed.port is None or 1 <= parsed.port <= 65_535)
        )
    except (TypeError, ValueError):
        return False


def resolve_vision_bridge_protocol(
    provider_adapter: object,
    settings: object,
    contract_version: object,
) -> str | None:
    """Return the controlled protocol selected by one exact model version.

    The model catalog decides the wire protocol.  Bridge-only timeout, retry,
    prompt, schema and body policy remain fixed in the protocol executor and
    never inherit generic provider settings.
    """

    if type(provider_adapter) is not str or contract_version != VISION_BRIDGE_CONTRACT_V1:
        return None
    if provider_adapter == VISION_BRIDGE_FAKE_ADAPTER:
        if isinstance(settings, Mapping) and not settings:
            return VISION_BRIDGE_PROTOCOL_FAKE
        return None
    if provider_adapter not in _OPENAI_CHAT_COMPLETIONS_ADAPTERS or not _has_exact_https_base_url(settings):
        return None
    assert isinstance(settings, Mapping)
    use_responses_api = settings.get("use_responses_api")
    if use_responses_api is True:
        if provider_adapter in _OPENAI_RESPONSES_ADAPTERS:
            return VISION_BRIDGE_PROTOCOL_OPENAI_RESPONSES
        return None
    if use_responses_api is not None and use_responses_api is not False:
        return None
    return VISION_BRIDGE_PROTOCOL_OPENAI_CHAT_COMPLETIONS


def resolve_materialized_vision_bridge_protocol(
    model_config: object,
    contract_version: object,
) -> str | None:
    """Resolve protocol from one exact runtime ``ModelConfig``-like object."""

    adapter = getattr(model_config, "system_provider_adapter", None)
    settings: Mapping[str, object]
    if adapter == VISION_BRIDGE_FAKE_ADAPTER:
        settings = {}
    else:
        settings = {
            "base_url": getattr(model_config, "base_url", None),
            "use_responses_api": getattr(
                model_config,
                "use_responses_api",
                None,
            ),
        }
    return resolve_vision_bridge_protocol(
        adapter,
        settings,
        contract_version,
    )


def vision_bridge_protocol_requires_external_dispatch(
    protocol: object,
) -> bool:
    """Return whether bytes leave the Worker for this resolved protocol."""

    return protocol in {
        VISION_BRIDGE_PROTOCOL_OPENAI_CHAT_COMPLETIONS,
        VISION_BRIDGE_PROTOCOL_OPENAI_RESPONSES,
    }


def is_vision_bridge_adapter_compatible(
    provider_adapter: object,
    contract_version: object,
) -> bool:
    """Compatibility shim for profiles whose protocol needs no model settings.

    Generic System Models must use :func:`resolve_vision_bridge_protocol`
    because their configured protocol is part of the compatibility decision.
    """

    if provider_adapter == VISION_BRIDGE_FAKE_ADAPTER:
        return contract_version == VISION_BRIDGE_CONTRACT_V1
    if provider_adapter == VISION_OPENAI_COMPATIBLE_V1_ADAPTER:
        return contract_version == VISION_BRIDGE_CONTRACT_V1
    return False


__all__ = [
    "VISION_BRIDGE_CONTRACT_V1",
    "VISION_BRIDGE_FAKE_ADAPTER",
    "VISION_BRIDGE_PROTOCOL_FAKE",
    "VISION_BRIDGE_PROTOCOL_OPENAI_CHAT_COMPLETIONS",
    "VISION_BRIDGE_PROTOCOL_OPENAI_RESPONSES",
    "VISION_OPENAI_COMPATIBLE_V1_ADAPTER",
    "is_vision_bridge_adapter_compatible",
    "resolve_materialized_vision_bridge_protocol",
    "resolve_vision_bridge_protocol",
    "vision_bridge_protocol_requires_external_dispatch",
]
