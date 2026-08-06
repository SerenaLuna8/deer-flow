"""Authenticate reverse-proxy client identity for public rate limits."""

from __future__ import annotations

import os
import secrets
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network

from starlette.requests import Request

from app.gateway.auth.config import get_auth_config
from deerflow.config.auth_config import AuthAppConfig

PROXY_AUTH_TOKEN_ENV = "DEER_FLOW_PROXY_AUTH_TOKEN"
PROXY_AUTH_TOKEN_HEADER = "x-deerflow-proxy-token"
_MIN_PROXY_AUTH_TOKEN_LENGTH = 32


def trusted_proxy_networks(
    config: AuthAppConfig,
) -> tuple[IPv4Network | IPv6Network, ...]:
    """Return normalized networks, rejecting invalid deployment input."""

    return tuple(ip_network(value, strict=False) for value in config.trusted_proxies)


def validate_proxy_identity_config(config: AuthAppConfig) -> None:
    """Fail startup for malformed CIDRs or an unsafe shared proxy token."""

    trusted_proxy_networks(config)
    token = os.getenv(PROXY_AUTH_TOKEN_ENV)
    if token is not None and len(token) < _MIN_PROXY_AUTH_TOKEN_LENGTH:
        raise ValueError(f"{PROXY_AUTH_TOKEN_ENV} must contain at least {_MIN_PROXY_AUTH_TOKEN_LENGTH} characters")
    if token is None:
        return
    jwt_secret = get_auth_config().jwt_secret
    separated_secrets = (
        os.getenv("DEER_FLOW_INTERNAL_AUTH_TOKEN"),
        jwt_secret,
        os.getenv("AUTH_JWT_SECRET"),
    )
    if any(secret is not None and secrets.compare_digest(token, secret) for secret in separated_secrets):
        raise ValueError("proxy authentication token must be independent from other authentication secrets")


def _has_valid_proxy_token(request: Request) -> bool:
    configured = os.getenv(PROXY_AUTH_TOKEN_ENV, "")
    if len(configured) < _MIN_PROXY_AUTH_TOKEN_LENGTH:
        return False
    presented = request.headers.get(PROXY_AUTH_TOKEN_HEADER, "")
    return bool(presented) and secrets.compare_digest(presented, configured)


def resolve_rate_limit_client_ip(
    request: Request,
    config: AuthAppConfig,
) -> str:
    """Resolve one canonical address from an authenticated reverse proxy."""

    peer_host = request.client.host if request.client else None
    try:
        peer_ip = ip_address(peer_host) if peer_host else None
    except ValueError:
        peer_ip = None

    trusted_peer = peer_ip is not None and any(peer_ip in network for network in trusted_proxy_networks(config))
    if trusted_peer or _has_valid_proxy_token(request):
        real_ip = request.headers.get("x-real-ip", "").strip()
        try:
            return ip_address(real_ip).compressed
        except ValueError:
            pass

    return peer_ip.compressed if peer_ip is not None else "unknown"


__all__ = [
    "PROXY_AUTH_TOKEN_ENV",
    "PROXY_AUTH_TOKEN_HEADER",
    "resolve_rate_limit_client_ip",
    "trusted_proxy_networks",
    "validate_proxy_identity_config",
]
