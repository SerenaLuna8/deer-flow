"""Strict, bounded and secret-free model catalog validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from app.system_settings.models import (
    CreateSystemModel,
    SystemModelConnectionCheck,
    UpdateSystemModel,
)

_MAX_SETTINGS_BYTES = 32 * 1024
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PROVIDER_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}\Z")
_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high"})
_THINKING_TYPES = frozenset({"adaptive", "disabled", "enabled"})
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)\b(?:sk|pk|tp)-(?:proj-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|"
        r"client[ _-]?secret|password|passwd|authorization|secret)"
        r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]{4,}"
    ),
)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)

_OPENAI_COMPATIBLE_FIELDS = frozenset(
    {
        "base_url",
        "extra_body",
        "max_retries",
        "max_tokens",
        "reasoning_effort",
        "request_timeout",
        "stream_chunk_timeout",
        "temperature",
        "timeout",
        "when_thinking_disabled",
        "when_thinking_enabled",
    }
)
_PROVIDER_SETTING_FIELDS: Mapping[str, frozenset[str]] = {
    "anthropic": frozenset(
        {
            "base_url",
            "default_request_timeout",
            "max_retries",
            "max_tokens",
            "request_timeout",
            "temperature",
            "thinking",
            "timeout",
            "when_thinking_disabled",
            "when_thinking_enabled",
        }
    ),
    "deepseek": _OPENAI_COMPATIBLE_FIELDS,
    "openai": _OPENAI_COMPATIBLE_FIELDS | frozenset({"output_version", "use_responses_api"}),
    "patched_deepseek": _OPENAI_COMPATIBLE_FIELDS,
    "patched_openai": _OPENAI_COMPATIBLE_FIELDS | frozenset({"output_version", "use_responses_api"}),
    "vllm": _OPENAI_COMPATIBLE_FIELDS | frozenset({"cumulative_stream_usage"}),
    "vision_bridge_fake": frozenset(),
    "vision_openai_compatible_v1": frozenset({"base_url"}),
}
_ALL_SETTING_FIELDS = frozenset().union(*_PROVIDER_SETTING_FIELDS.values())


class ModelSettingsInvalid(ValueError):
    """One indistinguishable validation failure for every invalid setting."""

    def __init__(self) -> None:
        super().__init__("Model settings invalid")


@dataclass(frozen=True, slots=True)
class ProviderAdapterSpec:
    class_path: str
    credential_required: bool


PROVIDER_ADAPTERS: Mapping[str, ProviderAdapterSpec] = {
    "anthropic": ProviderAdapterSpec(
        "langchain_anthropic:ChatAnthropic",
        True,
    ),
    "deepseek": ProviderAdapterSpec(
        "langchain_deepseek:ChatDeepSeek",
        True,
    ),
    "openai": ProviderAdapterSpec(
        "langchain_openai:ChatOpenAI",
        True,
    ),
    "patched_deepseek": ProviderAdapterSpec(
        "deerflow.models.patched_deepseek:PatchedChatDeepSeek",
        True,
    ),
    "patched_openai": ProviderAdapterSpec(
        "deerflow.models.patched_openai:PatchedChatOpenAI",
        True,
    ),
    "vllm": ProviderAdapterSpec(
        "deerflow.models.vllm_provider:VllmChatModel",
        True,
    ),
    "vision_bridge_fake": ProviderAdapterSpec(
        "deerflow.vision.fake_chat_model:FakeVisionBridgeChatModel",
        False,
    ),
    "vision_openai_compatible_v1": ProviderAdapterSpec(
        "langchain_openai:ChatOpenAI",
        True,
    ),
}


def _has_secret_like_value(value: str) -> bool:
    if any(ord(character) < 32 and character not in "\n\t" for character in value):
        return True
    if any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS):
        return True
    for match in _URL_IN_TEXT.finditer(value):
        candidate = match.group(0).rstrip(".,;:")
        try:
            parsed = urlsplit(candidate)
            if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
                return True
        except ValueError:
            return True
    return False


def _validate_public_text(
    value: object,
    *,
    max_chars: int,
    required: bool,
) -> str:
    if type(value) is not str:
        raise ValueError
    normalized = value.strip()
    if (required and not normalized) or len(normalized) > max_chars or _has_secret_like_value(normalized):
        raise ValueError
    return normalized


def _validate_base_url(value: object) -> str:
    if type(value) is not str:
        raise ValueError
    normalized = value.strip()
    if not normalized or len(normalized) > 2_048 or "\\" in normalized or "?" in normalized or "#" in normalized or any(character.isspace() for character in normalized) or _has_secret_like_value(normalized):
        raise ValueError
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.hostname is None or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError
    # Accessing port makes urllib reject malformed or out-of-range ports.
    _ = parsed.port
    return normalized


def _validate_number(
    value: object,
    *,
    minimum: float,
    maximum: float,
    integer: bool = False,
) -> int | float:
    if integer:
        if type(value) is not int:
            raise ValueError
    elif type(value) not in {int, float}:
        raise ValueError
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError
    return value


def _validate_reasoning_effort(value: object) -> str:
    if type(value) is not str or value not in _REASONING_EFFORTS:
        raise ValueError
    return value


def _validate_thinking(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value or set(value) - {"type", "budget_tokens"}:
        raise ValueError
    thinking_type = value.get("type")
    if type(thinking_type) is not str or thinking_type not in _THINKING_TYPES:
        raise ValueError
    result: dict[str, object] = {"type": thinking_type}
    if "budget_tokens" in value:
        if thinking_type == "disabled":
            raise ValueError
        result["budget_tokens"] = _validate_number(
            value["budget_tokens"],
            minimum=1,
            maximum=2_000_000,
            integer=True,
        )
    return result


def _validate_extra_body(value: object) -> dict[str, object]:
    allowed = {
        "chat_template_kwargs",
        "reasoning",
        "reasoning_format",
        "thinking",
    }
    if not isinstance(value, Mapping) or not value or set(value) - allowed:
        raise ValueError
    result: dict[str, object] = {}
    if "thinking" in value:
        result["thinking"] = _validate_thinking(value["thinking"])
    if "reasoning" in value:
        reasoning = value["reasoning"]
        if not isinstance(reasoning, Mapping) or set(reasoning) != {"effort"}:
            raise ValueError
        result["reasoning"] = {
            "effort": _validate_reasoning_effort(reasoning["effort"]),
        }
    if "reasoning_format" in value:
        if value["reasoning_format"] != "deepseek-style":
            raise ValueError
        result["reasoning_format"] = "deepseek-style"
    if "chat_template_kwargs" in value:
        chat_template = value["chat_template_kwargs"]
        if not isinstance(chat_template, Mapping) or not chat_template or set(chat_template) - {"enable_thinking", "thinking"} or any(type(item) is not bool for item in chat_template.values()):
            raise ValueError
        result["chat_template_kwargs"] = dict(chat_template)
    return result


def _validate_thinking_transition(
    value: object,
    *,
    enabled: bool,
) -> dict[str, object]:
    if not isinstance(value, Mapping) or not value or set(value) - {"extra_body", "reasoning_effort", "thinking"}:
        raise ValueError
    result: dict[str, object] = {}
    if "extra_body" in value:
        result["extra_body"] = _validate_extra_body(value["extra_body"])
    if "thinking" in value:
        result["thinking"] = _validate_thinking(value["thinking"])
    if "reasoning_effort" in value:
        result["reasoning_effort"] = _validate_reasoning_effort(value["reasoning_effort"])

    thinking_profiles = [
        result.get("thinking"),
        (result.get("extra_body", {}).get("thinking") if isinstance(result.get("extra_body"), dict) else None),
    ]
    expected_types = {"adaptive", "enabled"} if enabled else {"disabled"}
    for profile in thinking_profiles:
        if isinstance(profile, dict) and profile["type"] not in expected_types:
            raise ValueError
    extra_body = result.get("extra_body")
    if isinstance(extra_body, dict):
        chat_template = extra_body.get("chat_template_kwargs")
        if isinstance(chat_template, dict) and any(item is not enabled for item in chat_template.values()):
            raise ValueError
    return result


def _validate_setting_field(key: str, value: object) -> object:
    if key == "base_url":
        return _validate_base_url(value)
    if key in {
        "default_request_timeout",
        "request_timeout",
        "stream_chunk_timeout",
        "timeout",
    }:
        return _validate_number(value, minimum=0.1, maximum=3_600)
    if key == "max_retries":
        return _validate_number(value, minimum=0, maximum=20, integer=True)
    if key == "max_tokens":
        return _validate_number(
            value,
            minimum=1,
            maximum=2_000_000,
            integer=True,
        )
    if key == "temperature":
        return _validate_number(value, minimum=-2, maximum=2)
    if key == "reasoning_effort":
        return _validate_reasoning_effort(value)
    if key == "extra_body":
        return _validate_extra_body(value)
    if key == "thinking":
        return _validate_thinking(value)
    if key == "when_thinking_enabled":
        return _validate_thinking_transition(value, enabled=True)
    if key == "when_thinking_disabled":
        return _validate_thinking_transition(value, enabled=False)
    if key in {"cumulative_stream_usage", "use_responses_api"}:
        if type(value) is not bool:
            raise ValueError
        return value
    if key == "output_version":
        if value != "responses/v1":
            raise ValueError
        return value
    raise ValueError


def validate_model_settings(
    value: object,
    *,
    provider_adapter: str | None = None,
) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        if provider_adapter is not None:
            if type(provider_adapter) is not str or provider_adapter not in _PROVIDER_SETTING_FIELDS:
                raise ValueError
            allowed_fields = _PROVIDER_SETTING_FIELDS[provider_adapter]
        else:
            allowed_fields = _ALL_SETTING_FIELDS
        if any(type(key) is not str for key in value) or set(value) - allowed_fields:
            raise ValueError
        normalized = {key: _validate_setting_field(key, item) for key, item in value.items()}
        if provider_adapter == "vision_openai_compatible_v1":
            if set(normalized) != {"base_url"}:
                raise ValueError
            base_url = normalized["base_url"]
            if not isinstance(base_url, str) or urlsplit(base_url).scheme != "https":
                raise ValueError
        payload = json.dumps(
            normalized,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) > _MAX_SETTINGS_BYTES:
            raise ValueError
        return normalized
    except (
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise ModelSettingsInvalid() from None


def provider_class_path(provider_adapter: str) -> str:
    try:
        if type(provider_adapter) is not str:
            raise ValueError
        return PROVIDER_ADAPTERS[provider_adapter].class_path
    except (KeyError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def is_provider_adapter_supported(provider_adapter: object) -> bool:
    """Return whether an adapter may be selected for current model work."""

    return type(provider_adapter) is str and provider_adapter in PROVIDER_ADAPTERS


def provider_credential_required(provider_adapter: str) -> bool:
    try:
        if type(provider_adapter) is not str:
            raise ValueError
        return PROVIDER_ADAPTERS[provider_adapter].credential_required
    except (KeyError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def _validate_credential_group(
    provider_adapter: str,
    credential_id: uuid.UUID | None,
    credential_version_id: uuid.UUID | None,
    credential_env_key: str | None,
) -> tuple[uuid.UUID | None, uuid.UUID | None, str | None]:
    spec = PROVIDER_ADAPTERS[provider_adapter]
    values = (
        credential_id,
        credential_version_id,
        credential_env_key,
    )
    present = tuple(value is not None for value in values)
    if present not in {(False, False, False), (True, True, True)}:
        raise ValueError
    if spec.credential_required and not all(present):
        raise ValueError
    if not spec.credential_required and any(present):
        raise ValueError
    if not any(present):
        return None, None, None
    if type(credential_id) is not uuid.UUID or type(credential_version_id) is not uuid.UUID or type(credential_env_key) is not str or _ENV_KEY.fullmatch(credential_env_key) is None:
        raise ValueError
    return credential_id, credential_version_id, credential_env_key


def _validate_version_fields(
    *,
    display_name: object,
    provider_adapter: object,
    provider_model: object,
    settings: object,
    supports_thinking: object,
    supports_reasoning_effort: object,
    supports_vision: object,
    credential_id: object,
    credential_version_id: object,
    credential_env_key: object,
) -> dict[str, object]:
    try:
        if (
            type(provider_adapter) is not str
            or provider_adapter not in PROVIDER_ADAPTERS
            or type(provider_model) is not str
            or type(supports_thinking) is not bool
            or type(supports_reasoning_effort) is not bool
            or type(supports_vision) is not bool
        ):
            raise ValueError
        normalized_credential = _validate_credential_group(
            provider_adapter,
            credential_id,
            credential_version_id,
            credential_env_key,
        )
        display_name = _validate_public_text(
            display_name,
            max_chars=120,
            required=True,
        )
        provider_model = _validate_public_text(
            provider_model,
            max_chars=255,
            required=True,
        )
        if _PROVIDER_MODEL.fullmatch(provider_model) is None:
            raise ValueError
        return {
            "display_name": display_name,
            "provider_adapter": provider_adapter,
            "provider_model": provider_model,
            "settings": validate_model_settings(
                settings,
                provider_adapter=provider_adapter,
            ),
            "supports_thinking": supports_thinking,
            "supports_reasoning_effort": supports_reasoning_effort,
            "supports_vision": supports_vision,
            "credential_id": normalized_credential[0],
            "credential_version_id": normalized_credential[1],
            "credential_env_key": normalized_credential[2],
        }
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def validate_create_system_model(
    command: CreateSystemModel,
) -> CreateSystemModel:
    try:
        if not isinstance(command, CreateSystemModel):
            raise ValueError
        if command.status not in {"active", "suspended"}:
            raise ValueError
        values = _validate_version_fields(
            display_name=command.display_name,
            provider_adapter=command.provider_adapter,
            provider_model=command.provider_model,
            settings=command.settings,
            supports_thinking=command.supports_thinking,
            supports_reasoning_effort=command.supports_reasoning_effort,
            supports_vision=command.supports_vision,
            credential_id=command.credential_id,
            credential_version_id=command.credential_version_id,
            credential_env_key=command.credential_env_key,
        )
        return replace(
            command,
            **values,
        )
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def validate_update_system_model(
    command: UpdateSystemModel,
) -> UpdateSystemModel:
    try:
        if not isinstance(command, UpdateSystemModel):
            raise ValueError
        return replace(
            command,
            **_validate_version_fields(
                display_name=command.display_name,
                provider_adapter=command.provider_adapter,
                provider_model=command.provider_model,
                settings=command.settings,
                supports_thinking=command.supports_thinking,
                supports_reasoning_effort=command.supports_reasoning_effort,
                supports_vision=command.supports_vision,
                credential_id=command.credential_id,
                credential_version_id=command.credential_version_id,
                credential_env_key=command.credential_env_key,
            ),
        )
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def validate_system_model_connection_test(
    command: SystemModelConnectionCheck,
) -> SystemModelConnectionCheck:
    """Apply the same provider and Credential validation before a live probe."""

    try:
        if not isinstance(command, SystemModelConnectionCheck):
            raise ValueError
        values = _validate_version_fields(
            display_name="Connection test",
            provider_adapter=command.provider_adapter,
            provider_model=command.provider_model,
            settings=command.settings,
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=command.supports_vision,
            credential_id=command.credential_id,
            credential_version_id=command.credential_version_id,
            credential_env_key=command.credential_env_key,
        )
        return replace(
            command,
            provider_adapter=values["provider_adapter"],
            provider_model=values["provider_model"],
            settings=values["settings"],
            supports_vision=values["supports_vision"],
            credential_id=values["credential_id"],
            credential_version_id=values["credential_version_id"],
            credential_env_key=values["credential_env_key"],
        )
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def canonical_model_payload_checksum(
    model_config_id: uuid.UUID,
    command: CreateSystemModel | UpdateSystemModel,
) -> str:
    try:
        if type(model_config_id) is not uuid.UUID:
            raise ValueError
        payload = {
            "schema_version": 1,
            "model_config_id": str(model_config_id),
            "provider_adapter": command.provider_adapter,
            "provider_model": command.provider_model,
            "settings": validate_model_settings(
                command.settings,
                provider_adapter=command.provider_adapter,
            ),
            "supports_thinking": command.supports_thinking,
            "supports_reasoning_effort": command.supports_reasoning_effort,
            "supports_vision": command.supports_vision,
            "credential_id": (str(command.credential_id) if command.credential_id is not None else None),
            "credential_version_id": (str(command.credential_version_id) if command.credential_version_id is not None else None),
            "credential_env_key": command.credential_env_key,
        }
        encoded = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (
        AttributeError,
        OverflowError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
    ):
        raise ModelSettingsInvalid() from None


__all__ = [
    "ModelSettingsInvalid",
    "PROVIDER_ADAPTERS",
    "ProviderAdapterSpec",
    "canonical_model_payload_checksum",
    "is_provider_adapter_supported",
    "provider_class_path",
    "provider_credential_required",
    "validate_create_system_model",
    "validate_model_settings",
    "validate_system_model_connection_test",
    "validate_update_system_model",
]
