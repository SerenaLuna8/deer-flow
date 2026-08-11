from __future__ import annotations

import json
import ssl

import pytest
from pydantic import ValidationError

from app.workflows.http_execution import (
    HttpxWorkflowControlledEgressClient,
    WorkflowHttpEgressDispatchV1,
    WorkflowHttpEndpointResolutionV1,
    require_endpoint_owned_idempotency_header,
    workflow_http_automatic_retry_allowed,
)


def _dispatch_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "endpoint_policy_id": "partner-api",
        "method": "POST",
        "path_segments": ["v1", "items/with slash"],
        "query": [
            {"name": "page", "value": "1"},
            {"name": "search", "value": "A&B"},
        ],
        "headers": [{"name": "content-type", "value": "application/json"}],
        "body_utf8": '{"ok":true}',
        "idempotency_key": "a" * 64,
    }


def test_dispatch_is_structured_and_has_no_dynamic_url_or_ambient_authority() -> None:
    request = WorkflowHttpEgressDispatchV1.model_validate_json(json.dumps(_dispatch_payload()))
    payload = request.model_dump(mode="json")
    assert payload["path_segments"] == ["v1", "items/with slash"]
    assert all(field not in payload for field in ("url", "origin", "scheme", "host", "port", "proxy"))

    for field in ("url", "origin", "proxy_url", "follow_redirects", "cookies"):
        with pytest.raises(ValidationError):
            WorkflowHttpEgressDispatchV1.model_validate_json(json.dumps({**_dispatch_payload(), field: "https://evil.example"}))


@pytest.mark.parametrize(
    "name",
    [
        "host",
        "content-length",
        "cookie",
        "authorization",
        "proxy-authorization",
        "x-forwarded-for",
        "connection",
        "idempotency-key",
    ],
)
def test_dispatch_rejects_transport_and_credential_controlled_headers(
    name: str,
) -> None:
    payload = _dispatch_payload()
    payload["headers"] = [{"name": name, "value": "forbidden"}]
    with pytest.raises(ValidationError, match="controlled"):
        WorkflowHttpEgressDispatchV1.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "name",
    ["api_key", "token", "access-token", "client_secret"],
)
def test_dispatch_rejects_secret_looking_query_credentials(name: str) -> None:
    payload = _dispatch_payload()
    payload["query"] = [{"name": name, "value": "must-use-slot"}]
    with pytest.raises(ValidationError, match="Credential slot"):
        WorkflowHttpEgressDispatchV1.model_validate_json(json.dumps(payload))


def test_production_resolution_requires_exact_sorted_public_addresses() -> None:
    resolved = WorkflowHttpEndpointResolutionV1.model_validate_json(
        json.dumps(
            {
                "schema_version": 1,
                "endpoint_policy_id": "partner-api",
                "origin": "https://api.example.com",
                "hostname": "api.example.com",
                "port": 443,
                "resolved_addresses": ["8.8.4.4", "8.8.8.8"],
            }
        )
    )
    assert resolved.resolved_addresses == ("8.8.4.4", "8.8.8.8")

    for addresses in (
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["10.0.0.1"],
        ["::1"],
        ["8.8.8.8", "8.8.4.4"],
        ["8.8.8.8", "8.8.8.8"],
    ):
        with pytest.raises(ValidationError):
            WorkflowHttpEndpointResolutionV1.model_validate_json(
                json.dumps(
                    {
                        **resolved.model_dump(mode="json"),
                        "resolved_addresses": addresses,
                    }
                )
            )


def test_runtime_gateway_client_enforces_https_tls_no_redirect_cookie_or_env() -> None:
    context = ssl.create_default_context()
    client = HttpxWorkflowControlledEgressClient(
        gateway_url="https://egress-gateway.internal:8443",
        tls_context=context,
        timeout_ms=5_000,
        idempotency_headers_by_endpoint={"partner-api": "x-provider-operation"},
    )
    try:
        assert client.transport_invariants() == {
            "gateway_https": True,
            "tls_verify": True,
            "follow_redirects": False,
            "cookie_jar": False,
            "trust_env": False,
        }
    finally:
        import asyncio

        asyncio.run(client.aclose())

    with pytest.raises(ValueError, match="HTTPS"):
        HttpxWorkflowControlledEgressClient(
            gateway_url="http://egress-gateway.internal:8080",
            tls_context=context,
            timeout_ms=5_000,
            idempotency_headers_by_endpoint={"partner-api": None},
        )


def test_frozen_endpoint_custom_idempotency_header_is_server_owned() -> None:
    payload = _dispatch_payload()
    payload["headers"] = [{"name": "x-provider-operation", "value": "caller-owned"}]
    request = WorkflowHttpEgressDispatchV1.model_validate_json(json.dumps(payload))
    with pytest.raises(ValueError, match="server-owned"):
        require_endpoint_owned_idempotency_header(
            request,
            idempotency_header="x-provider-operation",
        )


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "reason",
    [
        "connect_failure",
        "http_408",
        "http_429",
        "http_502",
        "http_503",
        "http_504",
    ],
)
def test_only_get_and_head_have_bounded_automatic_retry(
    method: str,
    reason: str,
) -> None:
    assert workflow_http_automatic_retry_allowed(
        method=method,
        attempt=1,
        max_retries=2,
        reason=reason,
    )
    assert not workflow_http_automatic_retry_allowed(
        method=method,
        attempt=3,
        max_retries=2,
        reason=reason,
    )


@pytest.mark.parametrize(
    "reason",
    ["success", "http_400", "http_401", "http_500", "response_invalid"],
)
def test_get_and_head_do_not_retry_unapproved_responses(reason: str) -> None:
    assert not workflow_http_automatic_retry_allowed(
        method="GET",
        attempt=1,
        max_retries=2,
        reason=reason,
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_write_methods_never_use_transport_automatic_retry(method: str) -> None:
    assert not workflow_http_automatic_retry_allowed(
        method=method,
        attempt=1,
        max_retries=100,
        reason="connect_failure",
    )
