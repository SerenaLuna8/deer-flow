from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.gateway.auth.config import AuthConfig, set_auth_config
from app.gateway.auth.proxy_identity import validate_proxy_identity_config
from app.gateway.routers.auth import _get_client_ip
from deerflow.config.app_config import AppConfig, reset_app_config, set_app_config
from deerflow.config.auth_config import AuthAppConfig

REPO_ROOT = Path(__file__).resolve().parents[2]


def _request(
    peer_ip: str,
    *,
    real_ip: str | None = None,
    proxy_token: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if real_ip is not None:
        headers.append((b"x-real-ip", real_ip.encode("ascii")))
    if proxy_token is not None:
        headers.append((b"x-deerflow-proxy-token", proxy_token.encode("ascii")))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/auth/login/local",
            "raw_path": b"/api/v1/auth/login/local",
            "query_string": b"",
            "headers": headers,
            "client": (peer_ip, 44000),
            "server": ("gateway", 8001),
        }
    )


@pytest.fixture(autouse=True)
def _isolated_proxy_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("DEER_FLOW_PROXY_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)
    set_auth_config(
        AuthConfig(
            jwt_secret="proxy-identity-test-jwt-secret-independent-000",
        )
    )
    set_app_config(
        AppConfig.model_validate(
            {
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
                "auth": {"trusted_proxies": []},
            }
        )
    )
    try:
        yield
    finally:
        set_auth_config(
            AuthConfig(
                jwt_secret="proxy-identity-test-jwt-secret-independent-000",
            )
        )
        reset_app_config()


def test_auth_app_config_normalizes_and_validates_trusted_proxy_networks() -> None:
    assert AuthAppConfig().trusted_proxies == (
        "127.0.0.1/32",
        "::1/128",
    )
    assert AuthAppConfig(
        trusted_proxies=["192.0.2.7", "2001:db8::/48"],
    ).trusted_proxies == ("192.0.2.7/32", "2001:db8::/48")

    with pytest.raises(ValidationError):
        AuthAppConfig(trusted_proxies=["nginx", "not-an-ip"])


def test_untrusted_peer_cannot_spoof_real_ip() -> None:
    assert (
        _get_client_ip(
            _request("198.51.100.9", real_ip="203.0.113.40"),
        )
        == "198.51.100.9"
    )


def test_trusted_peer_uses_only_one_valid_canonical_real_ip() -> None:
    set_app_config(
        AppConfig.model_validate(
            {
                "sandbox": {
                    "use": "deerflow.sandbox.local:LocalSandboxProvider",
                },
                "auth": {"trusted_proxies": ["192.0.2.0/24"]},
            }
        )
    )

    assert (
        _get_client_ip(
            _request("192.0.2.7", real_ip="2001:0db8:0:0::9"),
        )
        == "2001:db8::9"
    )
    assert (
        _get_client_ip(
            _request("192.0.2.7", real_ip="203.0.113.8, 203.0.113.9"),
        )
        == "192.0.2.7"
    )
    assert (
        _get_client_ip(
            _request("192.0.2.7", real_ip="unknown"),
        )
        == "192.0.2.7"
    )


def test_deployment_proxy_token_authenticates_dynamic_proxy_without_trusting_cidr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "proxy-token-that-is-independent-and-at-least-32-bytes"
    monkeypatch.setenv("DEER_FLOW_PROXY_AUTH_TOKEN", token)

    assert (
        _get_client_ip(
            _request(
                "10.244.7.19",
                real_ip="203.0.113.17",
                proxy_token=token,
            )
        )
        == "203.0.113.17"
    )
    assert (
        _get_client_ip(
            _request(
                "10.244.7.19",
                real_ip="203.0.113.18",
                proxy_token="wrong-token-that-is-also-long-enough-000",
            )
        )
        == "10.244.7.19"
    )


def test_two_clients_behind_the_same_authenticated_proxy_keep_distinct_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "proxy-token-that-is-independent-and-at-least-32-bytes"
    monkeypatch.setenv("DEER_FLOW_PROXY_AUTH_TOKEN", token)
    first = _get_client_ip(_request("10.244.7.19", real_ip="203.0.113.21", proxy_token=token))
    second = _get_client_ip(_request("10.244.7.19", real_ip="203.0.113.22", proxy_token=token))

    assert first == "203.0.113.21"
    assert second == "203.0.113.22"
    assert first != second


@pytest.mark.parametrize(
    "shared_domain",
    ("internal", "jwt"),
)
def test_proxy_token_must_not_reuse_another_authentication_secret(
    monkeypatch: pytest.MonkeyPatch,
    shared_domain: str,
) -> None:
    shared = "shared-auth-domain-secret-that-must-never-be-reused"
    monkeypatch.setenv("DEER_FLOW_PROXY_AUTH_TOKEN", shared)
    if shared_domain == "internal":
        monkeypatch.setenv("DEER_FLOW_INTERNAL_AUTH_TOKEN", shared)
    else:
        set_auth_config(AuthConfig(jwt_secret=shared))

    with pytest.raises(ValueError) as captured:
        validate_proxy_identity_config(AuthAppConfig())
    assert shared not in str(captured.value)
    assert str(captured.value) == ("proxy authentication token must be independent from other authentication secrets")


def test_distinct_proxy_internal_and_jwt_secrets_pass_startup_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DEER_FLOW_PROXY_AUTH_TOKEN",
        "proxy-authentication-domain-secret-000000000",
    )
    monkeypatch.setenv(
        "DEER_FLOW_INTERNAL_AUTH_TOKEN",
        "internal-authentication-domain-secret-0000000",
    )
    set_auth_config(AuthConfig(jwt_secret="jwt-signing-domain-secret-000000000000000"))

    validate_proxy_identity_config(AuthAppConfig())


def test_repository_owned_container_and_helm_nginx_attest_real_ip() -> None:
    compose = (REPO_ROOT / "docker/docker-compose.yaml").read_text()
    dev_compose = (REPO_ROOT / "docker/docker-compose-dev.yaml").read_text()
    docker_launcher = (REPO_ROOT / "scripts/docker.sh").read_text()
    nginx = (REPO_ROOT / "docker/nginx/nginx.conf").read_text()
    helm_secret = (REPO_ROOT / "deploy/helm/deer-flow/templates/secret-app.yaml").read_text()
    helm_gateway = (REPO_ROOT / "deploy/helm/deer-flow/templates/gateway-deployment.yaml").read_text()
    helm_nginx = (REPO_ROOT / "deploy/helm/deer-flow/templates/nginx-deployment.yaml").read_text()
    helm_config = (REPO_ROOT / "deploy/helm/deer-flow/templates/configmap-nginx.yaml").read_text()

    assert compose.count("DEER_FLOW_PROXY_AUTH_TOKEN") >= 3
    assert "envsubst '$$DEER_FLOW_PROXY_AUTH_TOKEN'" in compose
    assert dev_compose.count("DEER_FLOW_PROXY_AUTH_TOKEN") >= 2
    assert "ensure_proxy_auth_token" in docker_launcher
    assert "DEER_FLOW_PROXY_AUTH_TOKEN" in docker_launcher
    assert 'X-DeerFlow-Proxy-Token "${DEER_FLOW_PROXY_AUTH_TOKEN}"' in nginx
    assert "DEER_FLOW_PROXY_AUTH_TOKEN" in helm_secret
    assert "DEER_FLOW_PROXY_AUTH_TOKEN" in helm_gateway
    assert "DEER_FLOW_PROXY_AUTH_TOKEN" in helm_nginx
    assert 'X-DeerFlow-Proxy-Token "${DEER_FLOW_PROXY_AUTH_TOKEN}"' in helm_config
