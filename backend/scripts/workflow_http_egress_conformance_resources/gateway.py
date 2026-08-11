from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlencode

TARGET_HOST = "target.workflow.test"
TARGET_PORT = 8443
PINNED_TARGET_IP = os.environ["PINNED_TARGET_IP"]
ALLOWED_KEYS = {
    "schema_version",
    "endpoint_policy_id",
    "method",
    "path_segments",
    "query",
    "headers",
    "body_utf8",
    "idempotency_key",
}
FORBIDDEN_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "cookie",
    "forwarded",
    "host",
    "proxy-authorization",
    "set-cookie",
    "transfer-encoding",
    "location",
}


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, context: ssl.SSLContext):
        super().__init__(host=host, port=TARGET_PORT, context=context, timeout=4)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, TARGET_PORT), timeout=4)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def _resolution_is_pinned() -> bool:
    addresses = sorted(
        {
            result[4][0]
            for result in socket.getaddrinfo(
                TARGET_HOST,
                TARGET_PORT,
                type=socket.SOCK_STREAM,
            )
        }
    )
    if addresses != [PINNED_TARGET_IP]:
        return False
    return ipaddress.ip_address(PINNED_TARGET_IP).is_private


def _count(value: bytes) -> dict[str, object]:
    return {"value": len(value), "relation": "exact"}


def _typed_outcome(
    status: int,
    headers: list[tuple[str, str]],
    body: bytes,
    duration_ms: int,
) -> dict[str, object]:
    content_type = next((value for name, value in headers if name == "content-type"), "")
    if "application/json" in content_type and body:
        try:
            parsed_body = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {
                "kind": "response_invalid",
                "status_code": status,
                "duration_ms": duration_ms,
                "wire_byte_count": _count(body),
                "decoded_byte_count": _count(body),
                "error": {
                    "code": "WORKFLOW_HTTP_RESPONSE_INVALID",
                    "safe_message": "Response did not match the declared schema.",
                },
            }
        persisted_body: dict[str, object] = {"kind": "json", "value": parsed_body}
        retained = json.dumps(
            parsed_body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    elif body:
        persisted_body = {"kind": "text", "text": body.decode("utf-8")}
        retained = body
    else:
        persisted_body = {"kind": "empty"}
        retained = b""
    safe_headers = [{"name": name, "value": value} for name, value in headers if name not in FORBIDDEN_HEADERS and not name.startswith("proxy-")]
    response = {
        "status_code": status,
        "headers": safe_headers,
        "body": persisted_body,
        "duration_ms": duration_ms,
        "wire_byte_count": _count(body),
        "decoded_byte_count": _count(body),
        "retained_body_byte_count": len(retained),
    }
    return {
        "kind": "success" if 200 <= status < 300 else "http_error",
        "response": response,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _json(self, status: int, value: dict[str, object]) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/v1/workflow-http/dispatch":
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            if not 1 <= length <= 2_097_152:
                raise ValueError
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or set(request) != ALLOWED_KEYS:
                raise ValueError
            if request["schema_version"] != 1:
                raise ValueError
            if request["endpoint_policy_id"] != "target-v1":
                self._json(403, {"error": "endpoint_forbidden"})
                return
            if request["method"] not in {
                "GET",
                "HEAD",
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            }:
                raise ValueError
            if not _resolution_is_pinned():
                self._json(409, {"error": "dns_pin_changed"})
                return
            segments = request["path_segments"]
            query = request["query"]
            headers = request["headers"]
            if not isinstance(segments, list) or len(segments) > 64:
                raise ValueError
            if not isinstance(query, list) or len(query) > 64:
                raise ValueError
            if not isinstance(headers, list) or len(headers) > 64:
                raise ValueError
            path = "/" + "/".join(quote(str(segment), safe="") for segment in segments)
            pairs = []
            for pair in query:
                if set(pair) != {"name", "value"}:
                    raise ValueError
                pairs.append((pair["name"], pair["value"]))
            if pairs:
                path += "?" + urlencode(pairs)
            outbound: dict[str, str] = {}
            for header in headers:
                if set(header) != {"name", "value"}:
                    raise ValueError
                name = str(header["name"]).lower()
                if name in FORBIDDEN_HEADERS or name.startswith("proxy-"):
                    raise ValueError
                outbound[name] = str(header["value"])
            if request["idempotency_key"] is not None:
                outbound["idempotency-key"] = request["idempotency_key"]
            body = None if request["body_utf8"] is None else str(request["body_utf8"]).encode()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._json(400, {"error": "request_invalid"})
            return

        context = ssl.create_default_context(cafile="/certs/ca.pem")
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        connection = PinnedHTTPSConnection(TARGET_HOST, PINNED_TARGET_IP, context)
        started = time.monotonic()
        try:
            connection.request(request["method"], path, body=body, headers=outbound)
            response = connection.getresponse()
            response_body = response.read(2_097_153)
            if len(response_body) > 2_097_152:
                self._json(502, {"error": "response_limit"})
                return
            response_headers = [(name.lower(), value) for name, value in response.getheaders()]
        except (OSError, ssl.SSLError, http.client.HTTPException):
            self._json(502, {"error": "transport_error"})
            return
        finally:
            connection.close()
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        self._json(
            200,
            _typed_outcome(
                response.status,
                response_headers,
                response_body,
                duration_ms,
            ),
        )


server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain("/certs/gateway.pem", "/certs/gateway.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
