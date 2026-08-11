from __future__ import annotations

import json
import os
import socket
import ssl
import urllib.request

TARGET_IP = os.environ["TARGET_IP"]


def _must_not_connect(host: str, port: int) -> None:
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        return
    raise RuntimeError(f"unexpected direct path to {host}:{port}")


_must_not_connect(TARGET_IP, 8443)
_must_not_connect("1.1.1.1", 443)

payload = json.dumps(
    {
        "schema_version": 1,
        "endpoint_policy_id": "target-v1",
        "method": "GET",
        "path_segments": ["v1", "success"],
        "query": [],
        "headers": [],
        "body_utf8": None,
        "idempotency_key": None,
    },
    separators=(",", ":"),
).encode()
context = ssl.create_default_context(cafile="/certs/ca.pem")
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=context),
)
request = urllib.request.Request(
    "https://egress.gateway.test:8443/v1/workflow-http/dispatch",
    data=payload,
    headers={"content-type": "application/json"},
    method="POST",
)
with opener.open(request, timeout=5) as response:
    outcome = json.loads(response.read())
if outcome.get("kind") != "success":
    raise RuntimeError("controlled egress dispatch failed")
print("worker_direct_path=blocked controlled_egress=passed", flush=True)
