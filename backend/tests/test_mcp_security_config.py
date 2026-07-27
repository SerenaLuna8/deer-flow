from __future__ import annotations

import pytest
from pydantic import ValidationError

from deerflow.config.app_config import AppConfig


def _config(**overrides: object) -> AppConfig:
    return AppConfig.model_validate(
        {
            "sandbox": {"use": "test"},
            **overrides,
        }
    )


def test_mcp_security_defaults_fail_closed_for_project_remote_servers() -> None:
    config = _config()

    assert config.mcp_security.project_remote_allowed_endpoints == ()
    assert config.mcp_security.require_egress_proxy is True
    assert config.mcp_security.egress_proxy_url is None
    assert config.mcp_security.discovery_timeout_seconds == 15
    assert config.mcp_security.tool_call_timeout_seconds == 60


def test_mcp_security_accepts_exact_https_endpoints_and_bounded_timeouts() -> None:
    config = _config(
        mcp_security={
            "project_remote_allowed_endpoints": [
                "https://mcp.example.test/api",
                "https://mcp.example.test:8443/sse",
            ],
            "require_egress_proxy": False,
            "discovery_timeout_seconds": 5,
            "tool_call_timeout_seconds": 120,
        }
    )

    assert config.mcp_security.project_remote_allowed_endpoints == (
        "https://mcp.example.test/api",
        "https://mcp.example.test:8443/sse",
    )
    assert config.mcp_security.require_egress_proxy is False
    assert config.mcp_security.discovery_timeout_seconds == 5
    assert config.mcp_security.tool_call_timeout_seconds == 120


@pytest.mark.parametrize(
    "endpoint",
    (
        "http://mcp.example.test/api",
        "https://*.example.test/api",
        "https://user:password@mcp.example.test/api",
        "https://127.0.0.1/api",
        "https://localhost/api",
        "https://mcp.example.test/api#fragment",
        "https://internal/api",
        "https://exa mple.test/api",
        "https://1234/api",
        "https://0x7f.0.0.1/api",
    ),
)
def test_mcp_security_rejects_unsafe_operator_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        _config(
            mcp_security={
                "project_remote_allowed_endpoints": [endpoint],
                "require_egress_proxy": False,
            }
        )


def test_mcp_security_requires_proxy_url_when_proxy_is_mandatory_and_remote_is_enabled() -> None:
    with pytest.raises(ValidationError):
        _config(
            mcp_security={
                "project_remote_allowed_endpoints": ["https://mcp.example.test/api"],
                "require_egress_proxy": True,
            }
        )
