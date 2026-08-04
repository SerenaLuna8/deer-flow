from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.project_channels.errors import ChannelInstanceValidationFailed

_PUBLIC_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}\Z")
_MAX_SECRET_LENGTH = 4096
_FEISHU_OFFICIAL_DOMAINS = frozenset(
    {
        "https://open.feishu.cn",
        "https://open.larksuite.com",
    }
)


def is_allowed_channel_public_value(
    provider: str,
    field: str,
    value: object,
) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if provider == "feishu" and field == "domain":
        return normalized in _FEISHU_OFFICIAL_DOMAINS
    return _PUBLIC_VALUE.fullmatch(normalized) is not None


@dataclass(frozen=True)
class ChannelProviderSpec:
    provider: str
    display_name: str
    public_fields: tuple[str, ...]
    required_public_fields: tuple[str, ...]
    credential_fields: tuple[str, ...]
    credential_env: Mapping[str, str]


@dataclass(frozen=True)
class NormalizedChannelConfiguration:
    provider: str
    public_config: dict[str, str]
    credential_payload: dict[str, dict[str, str]] | None


def _spec(
    provider: str,
    display_name: str,
    public_fields: tuple[str, ...],
    required_public_fields: tuple[str, ...],
    credential_fields: tuple[str, ...],
    credential_env: dict[str, str],
) -> ChannelProviderSpec:
    return ChannelProviderSpec(
        provider=provider,
        display_name=display_name,
        public_fields=public_fields,
        required_public_fields=required_public_fields,
        credential_fields=credential_fields,
        credential_env=MappingProxyType(dict(credential_env)),
    )


CHANNEL_PROVIDER_SPECS: Mapping[str, ChannelProviderSpec] = MappingProxyType(
    {
        "feishu": _spec(
            "feishu",
            "Feishu",
            ("app_id", "domain"),
            ("app_id",),
            ("app_secret",),
            {"app_secret": "FEISHU_APP_SECRET"},
        ),
        "slack": _spec(
            "slack",
            "Slack",
            (),
            (),
            ("bot_token", "app_token"),
            {"bot_token": "SLACK_BOT_TOKEN", "app_token": "SLACK_APP_TOKEN"},
        ),
        "telegram": _spec(
            "telegram",
            "Telegram",
            ("bot_username",),
            (),
            ("bot_token",),
            {"bot_token": "TELEGRAM_BOT_TOKEN"},
        ),
        "discord": _spec(
            "discord",
            "Discord",
            (),
            (),
            ("bot_token",),
            {"bot_token": "DISCORD_BOT_TOKEN"},
        ),
        "dingtalk": _spec(
            "dingtalk",
            "DingTalk",
            ("client_id",),
            ("client_id",),
            ("client_secret",),
            {"client_secret": "DINGTALK_CLIENT_SECRET"},
        ),
        "wecom": _spec(
            "wecom",
            "WeCom",
            ("bot_id",),
            ("bot_id",),
            ("bot_secret",),
            {"bot_secret": "WECOM_BOT_SECRET"},
        ),
        "wechat": _spec(
            "wechat",
            "WeChat",
            (),
            (),
            ("bot_token",),
            {"bot_token": "WECHAT_BOT_TOKEN"},
        ),
    }
)


def _unsupported_fields(
    values: Mapping[str, object],
    allowed: tuple[str, ...],
) -> bool:
    return any(not isinstance(key, str) or key not in allowed for key in values)


def validate_channel_configuration(
    provider: str,
    *,
    public_config: object,
    credentials: object,
    has_existing_credential: bool,
    request_id: str,
) -> NormalizedChannelConfiguration:
    spec = CHANNEL_PROVIDER_SPECS.get(provider)
    if spec is None:
        raise ChannelInstanceValidationFailed(
            request_id,
            "This channel provider is not supported.",
            fields=("provider",),
        )
    if not isinstance(public_config, Mapping) or not isinstance(credentials, Mapping):
        raise ChannelInstanceValidationFailed(
            request_id,
            f"{spec.display_name} configuration is invalid.",
        )
    if _unsupported_fields(public_config, spec.public_fields) or _unsupported_fields(
        credentials,
        spec.credential_fields,
    ):
        raise ChannelInstanceValidationFailed(
            request_id,
            f"{spec.display_name} configuration contains unsupported fields.",
        )

    normalized_public: dict[str, str] = {}
    missing_fields: list[str] = []
    for field in spec.public_fields:
        value = public_config.get(field)
        if value is None and field not in spec.required_public_fields:
            continue
        if provider == "feishu" and field == "domain":
            if not is_allowed_channel_public_value(provider, field, value):
                raise ChannelInstanceValidationFailed(
                    request_id,
                    "Feishu Domain must use an official HTTPS endpoint.",
                    fields=("public_config.domain",),
                )
            assert isinstance(value, str)
            normalized_public[field] = value.strip()
            continue
        if not is_allowed_channel_public_value(provider, field, value):
            missing_fields.append(f"public_config.{field}")
            continue
        assert isinstance(value, str)
        normalized_public[field] = value.strip()

    normalized_credentials: dict[str, str] = {}
    if credentials:
        for field in spec.credential_fields:
            value = credentials.get(field)
            if not isinstance(value, str) or not value or len(value) > _MAX_SECRET_LENGTH or "\x00" in value:
                missing_fields.append(f"credentials.{field}")
                continue
            normalized_credentials[field] = value
    elif not has_existing_credential:
        missing_fields.extend(f"credentials.{field}" for field in spec.credential_fields)

    if missing_fields:
        labels = " and ".join(
            {
                "app_id": "App ID",
                "app_secret": "App Secret",
                "client_id": "Client ID",
                "client_secret": "Client Secret",
                "bot_id": "Bot ID",
                "bot_secret": "Bot Secret",
                "bot_token": "Bot Token",
                "app_token": "App Token",
            }.get(field.rsplit(".", 1)[-1], field.rsplit(".", 1)[-1])
            for field in missing_fields
        )
        raise ChannelInstanceValidationFailed(
            request_id,
            f"{spec.display_name} {labels} are required.",
            fields=tuple(missing_fields),
        )

    payload = None
    if normalized_credentials:
        payload = {"env": {spec.credential_env[field]: normalized_credentials[field] for field in spec.credential_fields}}
    return NormalizedChannelConfiguration(
        provider=provider,
        public_config=normalized_public,
        credential_payload=payload,
    )


__all__ = [
    "CHANNEL_PROVIDER_SPECS",
    "ChannelProviderSpec",
    "NormalizedChannelConfiguration",
    "is_allowed_channel_public_value",
    "validate_channel_configuration",
]
