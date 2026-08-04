from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.gateway.channel_schemas import (
    ProjectChannelInstanceConfigureRequest,
    ProjectChannelInstanceResponse,
)
from app.project_channels.errors import ChannelInstanceValidationFailed
from app.project_channels.providers import validate_channel_configuration


def test_channel_configure_request_is_strict_and_keeps_secrets_write_only() -> None:
    request = ProjectChannelInstanceConfigureRequest.model_validate(
        {
            "display_name": "Team bot",
            "public_config": {"app_id": "cli_example"},
            "credentials": {"app_secret": "never-return-me"},
            "enabled": True,
        }
    )
    assert request.credentials == {"app_secret": "never-return-me"}
    assert "never-return-me" not in repr(request)
    with pytest.raises(ValidationError):
        ProjectChannelInstanceConfigureRequest.model_validate(
            {
                "public_config": {"app_id": "cli_example"},
                "credentials": {"app_secret": "never-return-me"},
                "enabled": True,
                "owner_user_id": str(uuid.uuid4()),
            }
        )

    response = ProjectChannelInstanceResponse(
        id=uuid.uuid4(),
        provider="feishu",
        display_name="Team bot",
        status="running",
        enabled=True,
        configured=True,
        credential_configured=True,
        public_config={"app_id": "cli_example"},
        updated_at=datetime(2026, 8, 3, tzinfo=UTC),
        last_error=None,
    )
    dumped = response.model_dump_json()
    assert "never-return-me" not in dumped
    assert "credentials" not in dumped
    assert "credential_version" not in dumped


def test_feishu_configuration_requires_app_id_and_secret_for_first_setup() -> None:
    with pytest.raises(ChannelInstanceValidationFailed) as exc_info:
        validate_channel_configuration(
            "feishu",
            public_config={},
            credentials={},
            has_existing_credential=False,
            request_id="req-channel",
        )
    assert exc_info.value.code == "CHANNEL_INSTANCE_INVALID"
    assert exc_info.value.message == "Feishu App ID and App Secret are required."
    assert exc_info.value.fields == ("public_config.app_id", "credentials.app_secret")


def test_feishu_secret_can_be_omitted_when_preserving_existing_credential() -> None:
    normalized = validate_channel_configuration(
        "feishu",
        public_config={"app_id": " cli_example "},
        credentials={},
        has_existing_credential=True,
        request_id="req-channel",
    )
    assert normalized.public_config == {"app_id": "cli_example"}
    assert normalized.credential_payload is None


@pytest.mark.parametrize(
    "domain",
    [
        "https://open.feishu.cn",
        "https://open.larksuite.com",
    ],
)
def test_feishu_accepts_only_official_domain_as_public_configuration(
    domain: str,
) -> None:
    normalized = validate_channel_configuration(
        "feishu",
        public_config={
            "app_id": "cli_example",
            "domain": domain,
        },
        credentials={"app_secret": "secret"},
        has_existing_credential=False,
        request_id="req-channel",
    )
    assert normalized.public_config["domain"] == domain


@pytest.mark.parametrize(
    "domain",
    [
        "http://open.feishu.cn",
        "https://open.feishu.cn/",
        "https://open.feishu.cn/open-apis",
        "https://open.feishu.cn?next=https://127.0.0.1",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://open.feishu.cn.evil.example",
    ],
)
def test_feishu_rejects_non_official_domain_without_echoing_input(
    domain: str,
) -> None:
    with pytest.raises(ChannelInstanceValidationFailed) as exc_info:
        validate_channel_configuration(
            "feishu",
            public_config={"app_id": "cli_example", "domain": domain},
            credentials={"app_secret": "secret"},
            has_existing_credential=False,
            request_id="req-channel-domain",
        )

    assert exc_info.value.fields == ("public_config.domain",)
    assert exc_info.value.message == ("Feishu Domain must use an official HTTPS endpoint.")
    assert domain not in str(exc_info.value)
    assert domain not in repr(exc_info.value)


def test_unconfigured_provider_response_has_no_fake_identity_or_timestamp() -> None:
    response = ProjectChannelInstanceResponse(
        id=None,
        provider="feishu",
        display_name="Feishu",
        status="unconfigured",
        enabled=False,
        configured=False,
        credential_configured=False,
        public_config={},
        updated_at=None,
        last_error=None,
    )
    assert response.model_dump()["id"] is None
    assert response.model_dump()["updated_at"] is None


def test_unknown_or_extra_provider_fields_fail_without_rendering_values() -> None:
    secret = "never-log-provider-secret"
    with pytest.raises(ChannelInstanceValidationFailed) as exc_info:
        validate_channel_configuration(
            "feishu",
            public_config={"app_id": "cli_example", "unknown": "value"},
            credentials={"app_secret": secret},
            has_existing_credential=False,
            request_id="req-channel",
        )
    assert exc_info.value.message == "Feishu configuration contains unsupported fields."
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)

    with pytest.raises(ChannelInstanceValidationFailed) as unknown:
        validate_channel_configuration(
            "unsupported",
            public_config={},
            credentials={"token": secret},
            has_existing_credential=False,
            request_id="req-channel",
        )
    assert unknown.value.message == "This channel provider is not supported."
    assert secret not in str(unknown.value)


@pytest.mark.parametrize(
    ("provider", "public_config", "credentials"),
    [
        ("slack", {}, {"bot_token": "x", "app_token": "y"}),
        ("telegram", {}, {"bot_token": "x"}),
        ("discord", {}, {"bot_token": "x"}),
        ("dingtalk", {"client_id": "id"}, {"client_secret": "x"}),
        ("wecom", {"bot_id": "id"}, {"bot_secret": "x"}),
        ("wechat", {}, {"bot_token": "x"}),
    ],
)
def test_other_supported_channels_share_the_same_instance_contract(
    provider: str,
    public_config: dict[str, str],
    credentials: dict[str, str],
) -> None:
    normalized = validate_channel_configuration(
        provider,
        public_config=public_config,
        credentials=credentials,
        has_existing_credential=False,
        request_id="req-channel",
    )
    assert normalized.public_config == public_config
    assert normalized.credential_payload is not None
    assert set(normalized.credential_payload["env"])
