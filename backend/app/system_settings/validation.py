"""Strict, bounded and secret-free model catalog validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

from app.system_settings.models import (
    CreateSystemModel,
    SystemModelConnectionCheck,
    UpdateSystemModel,
)

_MAX_SETTINGS_BYTES = 32 * 1024
_PROVIDER_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}\Z")
_SETTING_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
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


class ModelSettingsInvalid(ValueError):
    """One indistinguishable validation failure for every invalid setting."""

    def __init__(self) -> None:
        super().__init__("Model settings invalid")


ProviderSettingInputType = Literal[
    "boolean",
    "enum",
    "integer",
    "json",
    "number",
    "string",
    "url",
]


@dataclass(frozen=True, slots=True)
class ProviderSettingFieldSpec:
    """One immutable authoring field shared by validation and admin UI metadata."""

    name: str
    label: str
    input_type: ProviderSettingInputType
    advanced: bool = True
    minimum: int | float | None = None
    maximum: int | float | None = None
    step: int | float | None = None
    options: tuple[str, ...] = ()
    normalizer: Callable[[object], object] | None = None

    def __post_init__(self) -> None:
        numeric = self.input_type in {"integer", "number"}
        numeric_constraints = (self.minimum, self.maximum, self.step)
        options_valid = all(type(option) is str and bool(option) and len(option) <= 255 for option in self.options)
        if (
            type(self.name) is not str
            or not _SETTING_FIELD_NAME.fullmatch(self.name)
            or type(self.label) is not str
            or not self.label.strip()
            or len(self.label) > 120
            or self.input_type not in {"boolean", "enum", "integer", "json", "number", "string", "url"}
            or (not numeric and any(value is not None for value in numeric_constraints))
            or any(value is not None and type(value) not in {int, float} for value in numeric_constraints)
            or (self.input_type == "integer" and any(value is not None and type(value) is not int for value in numeric_constraints))
            or (self.step is not None and (not math.isfinite(self.step) or self.step <= 0))
            or (self.minimum is not None and not math.isfinite(self.minimum))
            or (self.maximum is not None and not math.isfinite(self.maximum))
            or (self.minimum is not None and self.maximum is not None and self.minimum > self.maximum)
            or (self.input_type == "enum" and (not self.options or not options_valid or len(set(self.options)) != len(self.options)))
            or (self.input_type != "enum" and bool(self.options))
            or not options_valid
            or (self.input_type == "json" and not self.advanced)
            or (self.input_type == "json" and self.normalizer is None)
            or (self.normalizer is not None and not callable(self.normalizer))
        ):
            raise ValueError("Provider setting field descriptor invalid")


@dataclass(frozen=True, slots=True)
class ProviderAdapterSpec:
    class_path: str
    api_key_required: bool
    fields: tuple[ProviderSettingFieldSpec, ...] = ()
    default_base_url: str | None = None

    def __post_init__(self) -> None:
        names = [field.name for field in self.fields]
        if len(set(names)) != len(names):
            raise ValueError("Provider setting fields must be unique")
        if self.default_base_url is not None:
            try:
                parsed = urlsplit(self.default_base_url)
                valid_default = parsed.scheme in {"http", "https"} and parsed.hostname is not None and parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment
                _ = parsed.port
            except (TypeError, ValueError):
                valid_default = False
            if not valid_default:
                raise ValueError("Provider default base URL invalid")

    @property
    def setting_fields(self) -> frozenset[str]:
        """Compatibility projection used by current authoring checks."""

        return frozenset(field.name for field in self.fields)

    def setting_field(self, name: str) -> ProviderSettingFieldSpec:
        for field in self.fields:
            if field.name == name:
                return field
        raise KeyError(name)


_BASE_URL_FIELD = ProviderSettingFieldSpec(
    "base_url",
    "Base URL",
    "url",
    advanced=False,
)
_MAX_TOKENS_FIELD = ProviderSettingFieldSpec(
    "max_tokens",
    "Max tokens",
    "integer",
    advanced=False,
    minimum=1,
    maximum=2_000_000,
    step=1,
)
_TEMPERATURE_FIELD = ProviderSettingFieldSpec(
    "temperature",
    "Temperature",
    "number",
    advanced=False,
    minimum=-2,
    maximum=2,
    step=0.01,
)
_REQUEST_TIMEOUT_FIELD = ProviderSettingFieldSpec(
    "request_timeout",
    "Request timeout (seconds)",
    "number",
    advanced=False,
    minimum=0.1,
    maximum=3_600,
    step=0.1,
)
_DEFAULT_REQUEST_TIMEOUT_FIELD = ProviderSettingFieldSpec(
    "default_request_timeout",
    "Default request timeout (seconds)",
    "number",
    minimum=0.1,
    maximum=3_600,
    step=0.1,
)
_STREAM_CHUNK_TIMEOUT_FIELD = ProviderSettingFieldSpec(
    "stream_chunk_timeout",
    "Stream chunk timeout (seconds)",
    "number",
    minimum=0.1,
    maximum=3_600,
    step=0.1,
)
_TIMEOUT_FIELD = ProviderSettingFieldSpec(
    "timeout",
    "Timeout (seconds)",
    "number",
    minimum=0.1,
    maximum=3_600,
    step=0.1,
)


_LEGACY_RUNTIME_OWNED_SETTING_FIELDS = frozenset({"max_retries"})


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


def _validate_thinking_enabled(value: object) -> dict[str, object]:
    return _validate_thinking_transition(value, enabled=True)


def _validate_thinking_disabled(value: object) -> dict[str, object]:
    return _validate_thinking_transition(value, enabled=False)


_EXTRA_BODY_FIELD = ProviderSettingFieldSpec(
    "extra_body",
    "Extra request body",
    "json",
    normalizer=_validate_extra_body,
)
_REASONING_EFFORT_FIELD = ProviderSettingFieldSpec(
    "reasoning_effort",
    "Reasoning effort",
    "enum",
    options=tuple(sorted(_REASONING_EFFORTS)),
    normalizer=_validate_reasoning_effort,
)
_THINKING_FIELD = ProviderSettingFieldSpec(
    "thinking",
    "Thinking configuration",
    "json",
    normalizer=_validate_thinking,
)
_THINKING_ENABLED_FIELD = ProviderSettingFieldSpec(
    "when_thinking_enabled",
    "Thinking-enabled overrides",
    "json",
    normalizer=_validate_thinking_enabled,
)
_THINKING_DISABLED_FIELD = ProviderSettingFieldSpec(
    "when_thinking_disabled",
    "Thinking-disabled overrides",
    "json",
    normalizer=_validate_thinking_disabled,
)
_OUTPUT_VERSION_FIELD = ProviderSettingFieldSpec(
    "output_version",
    "Output version",
    "enum",
    options=("responses/v1",),
)
_USE_RESPONSES_API_FIELD = ProviderSettingFieldSpec(
    "use_responses_api",
    "Use Responses API",
    "boolean",
)
_CUMULATIVE_STREAM_USAGE_FIELD = ProviderSettingFieldSpec(
    "cumulative_stream_usage",
    "Cumulative stream usage",
    "boolean",
)

_OPENAI_COMPATIBLE_FIELDS = (
    _BASE_URL_FIELD,
    _EXTRA_BODY_FIELD,
    _MAX_TOKENS_FIELD,
    _REASONING_EFFORT_FIELD,
    _REQUEST_TIMEOUT_FIELD,
    _STREAM_CHUNK_TIMEOUT_FIELD,
    _TEMPERATURE_FIELD,
    _TIMEOUT_FIELD,
    _THINKING_DISABLED_FIELD,
    _THINKING_ENABLED_FIELD,
)
_ANTHROPIC_FIELDS = (
    _BASE_URL_FIELD,
    _DEFAULT_REQUEST_TIMEOUT_FIELD,
    _MAX_TOKENS_FIELD,
    _REQUEST_TIMEOUT_FIELD,
    _TEMPERATURE_FIELD,
    _THINKING_FIELD,
    _TIMEOUT_FIELD,
    _THINKING_DISABLED_FIELD,
    _THINKING_ENABLED_FIELD,
)


BUILTIN_PROVIDER_ADAPTERS: Mapping[str, ProviderAdapterSpec] = MappingProxyType(
    {
        "anthropic": ProviderAdapterSpec(
            "langchain_anthropic:ChatAnthropic",
            True,
            fields=_ANTHROPIC_FIELDS,
            default_base_url="https://api.anthropic.com",
        ),
        "deepseek": ProviderAdapterSpec(
            "langchain_deepseek:ChatDeepSeek",
            True,
            fields=_OPENAI_COMPATIBLE_FIELDS,
            default_base_url="https://api.deepseek.com/v1",
        ),
        "openai": ProviderAdapterSpec(
            "langchain_openai:ChatOpenAI",
            True,
            fields=_OPENAI_COMPATIBLE_FIELDS + (_OUTPUT_VERSION_FIELD, _USE_RESPONSES_API_FIELD),
            default_base_url="https://api.openai.com/v1",
        ),
        "patched_deepseek": ProviderAdapterSpec(
            "deerflow.models.patched_deepseek:PatchedChatDeepSeek",
            True,
            fields=_OPENAI_COMPATIBLE_FIELDS,
            default_base_url="https://api.deepseek.com/v1",
        ),
        "patched_openai": ProviderAdapterSpec(
            "deerflow.models.patched_openai:PatchedChatOpenAI",
            True,
            fields=_OPENAI_COMPATIBLE_FIELDS + (_OUTPUT_VERSION_FIELD, _USE_RESPONSES_API_FIELD),
            default_base_url="https://api.openai.com/v1",
        ),
        "vllm": ProviderAdapterSpec(
            "deerflow.models.vllm_provider:VllmChatModel",
            True,
            fields=_OPENAI_COMPATIBLE_FIELDS + (_CUMULATIVE_STREAM_USAGE_FIELD,),
            default_base_url="https://api.openai.com/v1",
        ),
    }
)

# Tests may install deterministic adapters into this process-local copy. The
# immutable builtin map above is the production descriptor catalog and never
# contains test-only provider implementations.
PROVIDER_ADAPTERS: dict[str, ProviderAdapterSpec] = dict(BUILTIN_PROVIDER_ADAPTERS)
_ALL_SETTING_FIELD_SPECS = {field.name: field for descriptor in BUILTIN_PROVIDER_ADAPTERS.values() for field in descriptor.fields}


def _validate_setting_field(
    field: ProviderSettingFieldSpec,
    value: object,
) -> object:
    if field.input_type == "json":
        if field.normalizer is None:
            raise ValueError
        return field.normalizer(value)
    if field.input_type == "url":
        normalized = _validate_base_url(value)
    elif field.input_type in {"integer", "number"}:
        normalized = _validate_number(
            value,
            minimum=field.minimum if field.minimum is not None else -1e308,
            maximum=field.maximum if field.maximum is not None else 1e308,
            integer=field.input_type == "integer",
        )
    elif field.input_type == "boolean":
        if type(value) is not bool:
            raise ValueError
        normalized = value
    elif field.input_type == "enum":
        if type(value) is not str or value not in field.options:
            raise ValueError
        normalized = value
    elif field.input_type == "string":
        normalized = _validate_public_text(value, max_chars=2_048, required=True)
    else:
        raise ValueError
    return field.normalizer(normalized) if field.normalizer is not None else normalized


def _validate_model_settings(
    value: object,
    *,
    provider_adapter: str | None = None,
    allow_legacy_runtime_fields: bool,
) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        if provider_adapter is not None:
            descriptor = provider_adapter_descriptor(provider_adapter)
            field_specs = {field.name: field for field in descriptor.fields}
        else:
            field_specs = _ALL_SETTING_FIELD_SPECS
        allowed_fields = frozenset(field_specs)
        if allow_legacy_runtime_fields:
            allowed_fields = allowed_fields | _LEGACY_RUNTIME_OWNED_SETTING_FIELDS
        if any(type(key) is not str for key in value) or set(value) - allowed_fields:
            raise ValueError
        normalized = {key: (_validate_number(item, minimum=0, maximum=20, integer=True) if key in _LEGACY_RUNTIME_OWNED_SETTING_FIELDS else _validate_setting_field(field_specs[key], item)) for key, item in value.items()}
        if allow_legacy_runtime_fields:
            for key in _LEGACY_RUNTIME_OWNED_SETTING_FIELDS:
                normalized.pop(key, None)
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


def validate_model_settings(
    value: object,
    *,
    provider_adapter: str | None = None,
) -> dict[str, object]:
    """Validate settings accepted by current model authoring contracts."""

    return _validate_model_settings(
        value,
        provider_adapter=provider_adapter,
        allow_legacy_runtime_fields=False,
    )


def validate_materialized_model_settings(
    value: object,
    *,
    provider_adapter: str,
) -> dict[str, object]:
    """Read immutable historical settings while dropping Runtime-owned keys."""

    return _validate_model_settings(
        value,
        provider_adapter=provider_adapter,
        allow_legacy_runtime_fields=True,
    )


def provider_adapter_descriptor(
    provider_adapter: str,
) -> ProviderAdapterSpec:
    try:
        if type(provider_adapter) is not str:
            raise ValueError
        return PROVIDER_ADAPTERS[provider_adapter]
    except (KeyError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def provider_class_path(provider_adapter: str) -> str:
    return provider_adapter_descriptor(provider_adapter).class_path


def materialize_effective_model_settings(
    settings: Mapping[str, object],
    *,
    provider_adapter: str,
) -> dict[str, object]:
    """Pin a provider's real default endpoint instead of consulting host env."""

    result = dict(settings)
    if "base_url" not in result:
        descriptor = provider_adapter_descriptor(provider_adapter)
        if descriptor.api_key_required and descriptor.default_base_url is None:
            raise ModelSettingsInvalid()
        if descriptor.default_base_url is not None:
            result["base_url"] = descriptor.default_base_url
    return result


def is_provider_adapter_authorable(provider_adapter: object) -> bool:
    """Return whether admins may create/update models with this adapter."""

    return type(provider_adapter) is str and provider_adapter in PROVIDER_ADAPTERS


def is_provider_adapter_eligible_for_new_binding(
    provider_adapter: object,
) -> bool:
    """Return whether current catalog pointers may bind new model work."""

    return is_provider_adapter_authorable(provider_adapter)


def is_provider_adapter_supported(provider_adapter: object) -> bool:
    """Compatibility alias for current-work eligibility.

    New code should use the explicit authoring or binding predicate instead of
    conflating those decisions with historical materialization support.
    """

    return is_provider_adapter_eligible_for_new_binding(provider_adapter)


def provider_api_key_required(provider_adapter: str) -> bool:
    try:
        return provider_adapter_descriptor(
            provider_adapter,
        ).api_key_required
    except ModelSettingsInvalid:
        raise ModelSettingsInvalid() from None


def _validate_api_key(value: object) -> str | None:
    if value is None or value == "":
        return None
    if type(value) is not str or not value.strip() or len(value) > 16 * 1024:
        raise ValueError
    return value


def _validate_configuration_fields(
    *,
    display_name: object,
    provider_adapter: object,
    provider_model: object,
    settings: object,
    supports_thinking: object,
    supports_reasoning_effort: object,
    supports_vision: object,
    api_key: object,
) -> dict[str, object]:
    try:
        if (
            type(provider_adapter) is not str
            or not is_provider_adapter_authorable(provider_adapter)
            or type(provider_model) is not str
            or type(supports_thinking) is not bool
            or type(supports_reasoning_effort) is not bool
            or type(supports_vision) is not bool
        ):
            raise ValueError
        normalized_api_key = _validate_api_key(api_key)
        if normalized_api_key is not None and not provider_api_key_required(provider_adapter):
            raise ValueError
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
            "api_key": normalized_api_key,
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
        values = _validate_configuration_fields(
            display_name=command.display_name,
            provider_adapter=command.provider_adapter,
            provider_model=command.provider_model,
            settings=command.settings,
            supports_thinking=command.supports_thinking,
            supports_reasoning_effort=command.supports_reasoning_effort,
            supports_vision=command.supports_vision,
            api_key=command.api_key,
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
            **_validate_configuration_fields(
                display_name=command.display_name,
                provider_adapter=command.provider_adapter,
                provider_model=command.provider_model,
                settings=command.settings,
                supports_thinking=command.supports_thinking,
                supports_reasoning_effort=command.supports_reasoning_effort,
                supports_vision=command.supports_vision,
                api_key=command.api_key,
            ),
        )
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def validate_system_model_connection_test(
    command: SystemModelConnectionCheck,
) -> SystemModelConnectionCheck:
    """Apply provider validation and require this request's transient Key."""

    try:
        if not isinstance(command, SystemModelConnectionCheck):
            raise ValueError
        values = _validate_configuration_fields(
            display_name="Connection test",
            provider_adapter=command.provider_adapter,
            provider_model=command.provider_model,
            settings=command.settings,
            supports_thinking=False,
            supports_reasoning_effort=False,
            supports_vision=command.supports_vision,
            api_key=None,
        )
        transient_api_key = _validate_api_key(command.api_key)
        if provider_api_key_required(command.provider_adapter) and transient_api_key is None:
            raise ValueError
        return replace(
            command,
            provider_adapter=values["provider_adapter"],
            provider_model=values["provider_model"],
            settings=values["settings"],
            supports_vision=values["supports_vision"],
            api_key=transient_api_key or command.api_key,
        )
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


def canonical_model_payload_checksum(
    model_config_id: uuid.UUID,
    command: CreateSystemModel | UpdateSystemModel,
) -> str:
    try:
        if not isinstance(model_config_id, uuid.UUID):
            raise ValueError
        payload = canonical_model_payload(model_config_id, command)
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


def canonical_model_payload(
    model_config_id: uuid.UUID,
    command: CreateSystemModel | UpdateSystemModel,
) -> dict[str, object]:
    try:
        if not isinstance(model_config_id, uuid.UUID):
            raise ValueError
        return {
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
        }
    except (AttributeError, TypeError, ValueError):
        raise ModelSettingsInvalid() from None


__all__ = [
    "BUILTIN_PROVIDER_ADAPTERS",
    "ModelSettingsInvalid",
    "PROVIDER_ADAPTERS",
    "ProviderAdapterSpec",
    "ProviderSettingFieldSpec",
    "canonical_model_payload",
    "canonical_model_payload_checksum",
    "is_provider_adapter_authorable",
    "is_provider_adapter_eligible_for_new_binding",
    "is_provider_adapter_supported",
    "provider_adapter_descriptor",
    "provider_class_path",
    "materialize_effective_model_settings",
    "provider_api_key_required",
    "validate_materialized_model_settings",
    "validate_create_system_model",
    "validate_model_settings",
    "validate_system_model_connection_test",
    "validate_update_system_model",
]
