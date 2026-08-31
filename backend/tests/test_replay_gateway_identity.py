from types import SimpleNamespace

import pytest
from _replay_fixture import install_replay_model_adapter, replay_gateway_user

from app.gateway.auth_disabled import (
    AUTH_SOURCE_AUTH_DISABLED,
    AUTH_SOURCE_INTERNAL,
    AUTH_SOURCE_SESSION,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [AUTH_SOURCE_SESSION, AUTH_SOURCE_INTERNAL])
async def test_replay_gateway_preserves_authenticated_request_identity(
    source: str,
) -> None:
    user = SimpleNamespace(id="session-user")
    request = SimpleNamespace(
        state=SimpleNamespace(auth_source=source, user=user),
    )

    assert await replay_gateway_user(request) is user


@pytest.mark.asyncio
async def test_replay_gateway_maps_only_auth_disabled_identity_to_admin() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(
            auth_source=AUTH_SOURCE_AUTH_DISABLED,
            user=SimpleNamespace(id="default"),
        ),
    )

    user = await replay_gateway_user(request)

    assert str(user.id) == "5fb66f7d-5655-54df-a7da-66066c114f17"
    assert user.system_role == "system_admin"


def test_replay_model_uses_a_supported_provider_engineering_profile(monkeypatch) -> None:
    import os
    from dataclasses import replace

    from app.system_settings import validation

    monkeypatch.setattr(validation, "PROVIDER_ADAPTERS", dict(validation.PROVIDER_ADAPTERS))
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    native = validation.BUILTIN_PROVIDER_ADAPTERS["openai"]
    install_replay_model_adapter()
    replay = validation.PROVIDER_ADAPTERS["openai"]
    assert replay == replace(native, class_path="replay_provider:ReplayChatModel")
    assert replay.api_key_required is True
    assert validation.PROVIDER_ADAPTERS["knowledge_replay"] == native
