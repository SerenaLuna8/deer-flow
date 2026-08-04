from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig

_DEFAULT_PROJECT_REMOTE_ALLOWED_NETWORKS = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "::1/128",
    "fc00::/7",
)


def _config(**overrides: object) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            **overrides,
        }
    )


def test_mcp_security_defaults_to_bounded_local_and_private_networks() -> None:
    config = _config()

    assert config.mcp_security.project_remote_allowed_networks == _DEFAULT_PROJECT_REMOTE_ALLOWED_NETWORKS
    assert not hasattr(config.mcp_security, "project_remote_allowed_endpoints")
    assert config.mcp_security.require_egress_proxy is False
    assert config.mcp_security.egress_proxy_url is None
    assert config.mcp_security.discovery_timeout_seconds == 15
    assert config.mcp_security.tool_call_timeout_seconds == 60


def test_mcp_security_accepts_an_explicit_empty_deny_all_network_policy() -> None:
    config = _config(
        mcp_security={
            "project_remote_allowed_networks": [],
        }
    )

    assert config.mcp_security.project_remote_allowed_networks == ()


def test_mcp_security_accepts_canonical_ip_networks_and_bounded_timeouts() -> None:
    config = _config(
        mcp_security={
            "project_remote_allowed_networks": [
                "127.0.0.0/8",
                "10.0.0.0/8",
                "2001:0db8::/32",
            ],
            "discovery_timeout_seconds": 5,
            "tool_call_timeout_seconds": 120,
        }
    )

    assert config.mcp_security.project_remote_allowed_networks == (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "2001:db8::/32",
    )
    assert config.mcp_security.require_egress_proxy is False
    assert config.mcp_security.discovery_timeout_seconds == 5
    assert config.mcp_security.tool_call_timeout_seconds == 120


@pytest.mark.parametrize(
    "network",
    (
        "",
        "127.0.0.1",
        "10.1.2.3/8",
        "10.0.0.0/33",
        "2001:db8::1/64",
        "2001:db8::/129",
        "not-a-network/24",
    ),
)
def test_mcp_security_rejects_invalid_operator_networks(network: str) -> None:
    with pytest.raises(ValidationError):
        _config(
            mcp_security={
                "project_remote_allowed_networks": [network],
            }
        )


def test_mcp_security_rejects_duplicate_networks_and_retired_endpoint_field() -> None:
    with pytest.raises(ValidationError):
        _config(
            mcp_security={
                "project_remote_allowed_networks": [
                    "2001:db8::/32",
                    "2001:0db8::/32",
                ],
            }
        )
    with pytest.raises(ValidationError, match="project_remote_allowed_endpoints"):
        _config(mcp_security={"project_remote_allowed_endpoints": []})


def test_mcp_security_requires_proxy_url_when_proxy_is_mandatory_independently_of_networks() -> None:
    with pytest.raises(ValidationError):
        _config(
            mcp_security={
                "require_egress_proxy": True,
                "project_remote_allowed_networks": [],
            }
        )
