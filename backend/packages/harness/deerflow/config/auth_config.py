"""OIDC / SSO authentication configuration models."""

from __future__ import annotations

from ipaddress import ip_network
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OIDCProviderConfig(BaseModel):
    """Configuration for a single OIDC identity provider (Keycloak, Google, Azure AD, etc.)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(description="Human-readable name shown on the login button")
    issuer: str = Field(description="OIDC issuer URL (e.g. https://keycloak.example.com/realms/deerflow)")
    client_id: str = Field(description="OAuth2 client ID assigned by the provider")
    client_secret: str | None = Field(default=None, description="OAuth2 client secret ($ENV_VAR references supported)")
    redirect_uri: str | None = Field(default=None, description="Callback URL the provider will redirect to after auth")
    scopes: list[str] = Field(
        default_factory=lambda: ["openid", "email", "profile"],
        description="OIDC scopes to request (must include openid)",
    )
    token_endpoint_auth_method: Literal["client_secret_post", "client_secret_basic", "none"] = Field(
        default="client_secret_post",
        description="How the client authenticates at the token endpoint",
    )

    # ── User provisioning ─────────────────────────────────────────────
    auto_create_users: bool = Field(
        default=True,
        description="Automatically create an ActWeave user on first SSO login",
    )
    require_verified_email: bool = Field(
        default=True,
        description="Reject authentication if the provider does not report the email as verified",
    )
    allowed_email_domains: list[str] = Field(
        default_factory=list,
        description="If non-empty, only allow users whose email domain is in this list (e.g. ['example.com'])",
    )
    admin_emails: list[str] = Field(
        default_factory=list,
        description="Users with these email addresses are automatically granted the admin role on first login",
    )

    # ── PKCE / nonce ──────────────────────────────────────────────────
    pkce_enabled: bool = Field(default=True, description="Enable PKCE (S256) for the authorization code flow")
    nonce_enabled: bool = Field(default=True, description="Include and validate the nonce claim in ID tokens")

    # ── Endpoint overrides (for providers with non-standard discovery) ─
    authorization_endpoint: str | None = Field(default=None)
    token_endpoint: str | None = Field(default=None)
    userinfo_endpoint: str | None = Field(default=None)
    jwks_uri: str | None = Field(default=None)


class OIDCAuthConfig(BaseModel):
    """Top-level OIDC authentication configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable OIDC SSO authentication")
    frontend_base_url: str | None = Field(
        default=None,
        description="Base URL of the frontend (used for callback redirects when behind a reverse proxy)",
    )
    providers: dict[str, OIDCProviderConfig] = Field(
        default_factory=dict,
        description="Map of provider IDs to their configuration (e.g. keycloak, google, azure)",
    )


class LocalAuthConfig(BaseModel):
    """Configuration for the built-in email/password provider."""

    model_config = ConfigDict(extra="forbid")

    allow_registration: bool = Field(
        default=True,
        description=("Allow visitors to create local accounts through POST /api/v1/auth/register. First-admin initialization and controlled OIDC provisioning are independent."),
    )
    allow_insecure_persistent_cookie: bool = Field(
        default=False,
        description=("Allow remember-me cookies on non-loopback plain HTTP origins. Keep false unless transport security is enforced elsewhere."),
    )


class AuthAppConfig(BaseModel):
    """Authentication configuration section for the ActWeave app config."""

    model_config = ConfigDict(extra="forbid")

    trusted_proxies: tuple[str, ...] = Field(
        default=("127.0.0.1/32", "::1/128"),
        description=("Exact proxy IPs/CIDRs allowed to supply X-Real-IP. Loopback is trusted by default for the repository-owned local nginx."),
    )
    local: LocalAuthConfig = Field(
        default_factory=LocalAuthConfig,
        description="Built-in email/password authentication settings",
    )
    oidc: OIDCAuthConfig = Field(default_factory=OIDCAuthConfig, description="OIDC SSO authentication settings")

    @field_validator("trusted_proxies")
    @classmethod
    def _normalize_trusted_proxies(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized: list[str] = []
        for value in values:
            network = ip_network(value.strip(), strict=False)
            rendered = str(network)
            if rendered not in normalized:
                normalized.append(rendered)
        return tuple(normalized)
