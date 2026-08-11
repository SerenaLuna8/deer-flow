from __future__ import annotations

import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_counts: dict[str, int] = {}
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _handle(self) -> None:
        with _lock:
            _counts[self.path] = _counts.get(self.path, 0) + 1
            count = _counts[self.path]
        length = int(self.headers.get("content-length", "0"))
        if length:
            self.rfile.read(length)

        status = 200
        content_type = "application/json"
        body = b'{"ok":true}'
        extra_headers: list[tuple[str, str]] = []
        if self.path == "/v1/not-found":
            status, body = 404, b""
        elif self.path == "/v1/server-error":
            status, body = 503, b""
        elif self.path == "/v1/invalid":
            body = b"{not-json"
        elif self.path == "/v1/redirect":
            status, body = 302, b""
            extra_headers.append(("location", "/v1/success"))
        elif self.path == "/v1/cookie":
            extra_headers.append(("set-cookie", "workflow_secret=must-not-return"))
        elif self.path == "/v1/check-cookie":
            if self.headers.get("cookie"):
                status, body = 400, b""
        elif self.path == "/v1/count":
            body = json.dumps({"count": count}, separators=(",", ":")).encode()
        elif self.path.startswith("/echo/"):
            body = json.dumps(
                {"path": self.path},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()

        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("x-target-count", str(count))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle


server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.minimum_version = ssl.TLSVersion.TLSv1_2
context.load_cert_chain("/certs/target.pem", "/certs/target.key")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
