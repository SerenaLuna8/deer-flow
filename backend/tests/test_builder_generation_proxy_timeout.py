from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
API_LOCATION = r"location\s+/api/\s*\{"


@pytest.mark.parametrize(
    "relative_path",
    (
        "docker/nginx/nginx.conf",
        "docker/nginx/nginx.local.conf",
        "deploy/helm/deer-flow/templates/configmap-nginx.yaml",
    ),
)
def test_api_proxy_timeout_is_longer_than_builder_generation_bound(
    relative_path: str,
) -> None:
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    match = re.search(API_LOCATION + r"(?P<body>.*?)\n\s*\}", source, re.DOTALL)

    assert match is not None
    body = match.group("body")
    assert "proxy_connect_timeout 600s;" in body
    assert "proxy_send_timeout 600s;" in body
    assert "proxy_read_timeout 600s;" in body
