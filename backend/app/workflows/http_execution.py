"""Structured Workflow HTTP dispatch through an injected egress gateway.

There is intentionally no direct-origin transport in this module.  The Worker
can submit only an endpoint-policy id plus bounded path/query/header/body
material to a separately provisioned controlled-egress gateway.  The gateway
owns DNS resolution, connection pinning, TLS/SNI and Credential injection.
"""

from __future__ import annotations

import ipaddress
import re
import ssl
from collections.abc import Mapping
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.workflows.contracts import (
    WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER,
    WorkflowHttpSettledOutcomeV1,
)
from deerflow.workflows import MAX_SAFE_JSON_INTEGER, StrictLiteralOne

_StableId = Annotated[
    StrictStr,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
_HeaderName = Annotated[
    StrictStr,
    Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9!#$%&'*+.^_`|~-]+$",
    ),
]
_HeaderValue = Annotated[StrictStr, Field(max_length=4_096)]
_PathSegment = Annotated[StrictStr, Field(max_length=1_024)]
_QueryName = Annotated[StrictStr, Field(min_length=1, max_length=256)]
_QueryValue = Annotated[StrictStr, Field(max_length=4_096)]
_BodyUtf8 = Annotated[StrictStr, Field(max_length=2_097_152)]
_Sha256Hex = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
_PositiveInt = Annotated[StrictInt, Field(ge=1, le=MAX_SAFE_JSON_INTEGER)]
_HttpMethod = Literal["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]

_CONTROLLED_HEADERS = frozenset(
    {
        "authorization",
        "authentication-info",
        "connection",
        "content-length",
        "cookie",
        "forwarded",
        "host",
        "idempotency-key",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "www-authenticate",
    }
)


def _is_controlled_header(value: str) -> bool:
    return value in _CONTROLLED_HEADERS or value.startswith("proxy-") or value.startswith("x-forwarded-")


def _require_utf8(value: str, *, maximum: int) -> str:
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise ValueError("Workflow HTTP dispatch text must use Unicode scalars") from error
    if length > maximum or "\x00" in value:
        raise ValueError("Workflow HTTP dispatch text exceeds its byte contract")
    return value


class _StrictEgressContract(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        revalidate_instances="always",
    )


class WorkflowHttpEgressHeaderV1(_StrictEgressContract):
    name: _HeaderName
    value: _HeaderValue

    @field_validator("name")
    @classmethod
    def reject_controlled_header(cls, value: str) -> str:
        if _is_controlled_header(value):
            raise ValueError("transport- and Credential-controlled headers are not accepted")
        return value

    @field_validator("value")
    @classmethod
    def validate_value_bytes(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Workflow HTTP header values cannot contain line breaks")
        return _require_utf8(value, maximum=4_096)


class WorkflowHttpEgressQueryV1(_StrictEgressContract):
    name: _QueryName
    value: _QueryValue

    @field_validator("name")
    @classmethod
    def validate_name_bytes(cls, value: str) -> str:
        value = _require_utf8(value, maximum=256)
        if re.search(
            r"(?:^|[-_])(api[-_]?key|token|secret|access[-_]?token)(?:$|[-_])",
            value,
            flags=re.IGNORECASE,
        ):
            raise ValueError("secret-looking query parameters require an endpoint Credential slot")
        return value

    @field_validator("value")
    @classmethod
    def validate_value_bytes(cls, value: str) -> str:
        return _require_utf8(value, maximum=4_096)


class WorkflowHttpEgressDispatchV1(_StrictEgressContract):
    """A request shape that is structurally unable to choose scheme/host/port."""

    schema_version: StrictLiteralOne
    endpoint_policy_id: _StableId
    method: _HttpMethod
    path_segments: Annotated[tuple[_PathSegment, ...], Field(max_length=64)]
    query: Annotated[tuple[WorkflowHttpEgressQueryV1, ...], Field(max_length=64)]
    headers: Annotated[tuple[WorkflowHttpEgressHeaderV1, ...], Field(max_length=64)]
    body_utf8: _BodyUtf8 | None
    idempotency_key: _Sha256Hex | None

    @field_validator("path_segments")
    @classmethod
    def validate_path_segment_bytes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for segment in value:
            _require_utf8(segment, maximum=1_024)
        return value

    @field_validator("body_utf8")
    @classmethod
    def validate_body_bytes(cls, value: str | None) -> str | None:
        if value is not None:
            _require_utf8(value, maximum=2_097_152)
        return value

    @model_validator(mode="after")
    def validate_request_shape(self) -> Self:
        names = [header.name for header in self.headers]
        if len(names) != len(set(names)):
            raise ValueError("Workflow HTTP dispatch headers must be unique")
        if self.method in {"GET", "HEAD"}:
            if self.body_utf8 is not None or self.idempotency_key is not None:
                raise ValueError("GET and HEAD cannot carry body or write idempotency")
        elif self.idempotency_key is None:
            raise ValueError("write requests require a server-derived idempotency key")
        return self


class WorkflowHttpEndpointResolutionV1(_StrictEgressContract):
    """One DNS answer set pinned by the egress gateway for a single dispatch."""

    schema_version: StrictLiteralOne
    endpoint_policy_id: _StableId
    origin: Annotated[StrictStr, Field(min_length=1, max_length=2_048)]
    hostname: Annotated[
        StrictStr,
        Field(
            min_length=1,
            max_length=253,
            pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$",
        ),
    ]
    port: Annotated[StrictInt, Field(ge=1, le=65_535)]
    resolved_addresses: Annotated[tuple[StrictStr, ...], Field(min_length=1, max_length=16)]

    @model_validator(mode="after")
    def validate_public_pinned_resolution(self) -> Self:
        try:
            parsed = urlsplit(self.origin)
            parsed_port = parsed.port or 443
        except ValueError as error:
            raise ValueError("Workflow HTTP endpoint origin is invalid") from error
        if not (parsed.scheme == "https" and parsed.hostname == self.hostname and parsed_port == self.port and parsed.username is None and parsed.password is None and not parsed.path and not parsed.query and not parsed.fragment):
            raise ValueError("Workflow HTTP resolution does not match its fixed origin")
        canonical: list[str] = []
        for raw in self.resolved_addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as error:
                raise ValueError("Workflow HTTP DNS answer is not an IP address") from error
            if not address.is_global or str(address) != raw:
                raise ValueError("Workflow HTTP DNS answers must be canonical public IPs")
            canonical.append(raw)
        if canonical != sorted(set(canonical)):
            raise ValueError("Workflow HTTP DNS answers must be unique and sorted")
        return self


def workflow_http_automatic_retry_allowed(
    *,
    method: str,
    attempt: int,
    max_retries: int,
    reason: str,
) -> bool:
    if type(attempt) is not int or type(max_retries) is not int:
        raise TypeError("retry counts must be integers")
    if attempt < 1 or max_retries < 0:
        raise ValueError("retry counts are invalid")
    if type(reason) is not str:
        raise TypeError("retry reason must be text")
    return (
        method in {"GET", "HEAD"}
        and reason
        in {
            "connect_failure",
            "http_408",
            "http_429",
            "http_502",
            "http_503",
            "http_504",
        }
        and attempt <= max_retries
    )


def require_endpoint_owned_idempotency_header(
    request: WorkflowHttpEgressDispatchV1,
    *,
    idempotency_header: str | None,
) -> None:
    """Reject caller headers reserved by the frozen endpoint policy."""

    if type(request) is not WorkflowHttpEgressDispatchV1:
        raise TypeError("request must be WorkflowHttpEgressDispatchV1")
    if idempotency_header is None:
        return
    if re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]{1,128}", idempotency_header) is None:
        raise ValueError("endpoint idempotency header is invalid")
    if any(header.name == idempotency_header for header in request.headers):
        raise ValueError("endpoint idempotency header is server-owned and cannot be submitted")


class WorkflowHttpControlledEgressError(RuntimeError):
    pass


class HttpxWorkflowControlledEgressClient:
    """mTLS-ready client for the injected gateway, never for an origin URL."""

    def __init__(
        self,
        *,
        gateway_url: str,
        tls_context: ssl.SSLContext,
        timeout_ms: int,
        idempotency_headers_by_endpoint: Mapping[str, str | None],
    ) -> None:
        try:
            parsed = urlsplit(gateway_url)
        except ValueError as error:
            raise ValueError("Workflow HTTP egress gateway URL is invalid") from error
        if not (parsed.scheme == "https" and parsed.hostname and parsed.username is None and parsed.password is None and not parsed.path and not parsed.query and not parsed.fragment):
            raise ValueError("Workflow HTTP egress gateway must be one HTTPS origin")
        if not isinstance(tls_context, ssl.SSLContext):
            raise TypeError("tls_context must be an SSLContext")
        if tls_context.verify_mode != ssl.CERT_REQUIRED or not tls_context.check_hostname:
            raise ValueError("Workflow HTTP egress gateway TLS verification is required")
        if type(timeout_ms) is not int or not 1 <= timeout_ms <= 300_000:
            raise ValueError("Workflow HTTP egress timeout is invalid")
        if not isinstance(idempotency_headers_by_endpoint, Mapping):
            raise TypeError("idempotency_headers_by_endpoint must be a mapping")
        endpoint_headers: dict[str, str | None] = {}
        for endpoint_id, header in idempotency_headers_by_endpoint.items():
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", endpoint_id) is None:
                raise ValueError("Workflow HTTP endpoint policy id is invalid")
            if header is not None and re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]{1,128}", header) is None:
                raise ValueError("Workflow HTTP endpoint idempotency header is invalid")
            endpoint_headers[endpoint_id] = header
        self._gateway_url = gateway_url
        self._tls_context = tls_context
        self._timeout = httpx.Timeout(timeout_ms / 1_000)
        self._idempotency_headers_by_endpoint = endpoint_headers

    def transport_invariants(self) -> dict[str, bool]:
        return {
            "gateway_https": True,
            "tls_verify": True,
            "follow_redirects": False,
            "cookie_jar": False,
            "trust_env": False,
        }

    async def dispatch(
        self,
        request: WorkflowHttpEgressDispatchV1,
    ) -> WorkflowHttpSettledOutcomeV1:
        if type(request) is not WorkflowHttpEgressDispatchV1:
            raise TypeError("request must be WorkflowHttpEgressDispatchV1")
        if request.endpoint_policy_id not in self._idempotency_headers_by_endpoint:
            raise WorkflowHttpControlledEgressError("Workflow HTTP endpoint policy is not frozen in this Run profile")
        require_endpoint_owned_idempotency_header(
            request,
            idempotency_header=self._idempotency_headers_by_endpoint[request.endpoint_policy_id],
        )
        # One client per dispatch prevents a Set-Cookie response from becoming an
        # ambient cookie on any later request. Redirects and environment proxy
        # discovery are disabled independently.
        async with httpx.AsyncClient(
            base_url=self._gateway_url,
            verify=self._tls_context,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            cookies=None,
        ) as client:
            response = await client.post(
                "/v1/workflow-http/dispatch",
                content=request.model_dump_json().encode("utf-8"),
                headers={"content-type": "application/json"},
            )
        if response.status_code != 200:
            raise WorkflowHttpControlledEgressError("Workflow HTTP controlled-egress gateway rejected the dispatch")
        if len(response.content) > 2_097_152:
            raise WorkflowHttpControlledEgressError("Workflow HTTP controlled-egress response exceeded its contract")
        try:
            return WORKFLOW_HTTP_SETTLED_OUTCOME_V1_ADAPTER.validate_json(response.content)
        except ValueError as error:
            raise WorkflowHttpControlledEgressError("Workflow HTTP controlled-egress response was invalid") from error

    async def aclose(self) -> None:
        """Compatibility hook; dispatch-scoped clients are already closed."""


__all__ = [
    "HttpxWorkflowControlledEgressClient",
    "WorkflowHttpControlledEgressError",
    "WorkflowHttpEgressDispatchV1",
    "WorkflowHttpEgressHeaderV1",
    "WorkflowHttpEgressQueryV1",
    "WorkflowHttpEndpointResolutionV1",
    "require_endpoint_owned_idempotency_header",
    "workflow_http_automatic_retry_allowed",
]
