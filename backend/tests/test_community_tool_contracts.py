from __future__ import annotations

import ipaddress
import json
from types import SimpleNamespace

import pytest

from deerflow.community import ddg_search, image_search
from deerflow.community.brave import tools as brave_tools
from deerflow.community.errors import CommunityToolError
from deerflow.community.groundroute import tools as groundroute_tools
from deerflow.community.serper import tools as serper_tools
from deerflow.community.url_safety import (
    sanitize_public_http_reference_url,
    validate_public_http_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://2130706433/private",
        "http://0x7f000001/private",
        "http://0177.0.0.1/private",
        "http://[::ffff:127.0.0.1]/private",
        "http://[2002:7f00:1::]/private",
        "http://[64:ff9b::7f00:1]/private",
        "http://localhost./private",
        "http://metadata.google.internal/private",
    ],
)
def test_reference_url_filter_rejects_private_and_obfuscated_hosts(url: str) -> None:
    assert sanitize_public_http_reference_url(url) == ""


def test_reference_url_filter_accepts_public_references_without_dns_resolution() -> None:
    assert sanitize_public_http_reference_url("https://images.example.com/a.png") == "https://images.example.com/a.png"


def test_reference_url_filter_treats_malformed_ipv6_as_invalid() -> None:
    assert sanitize_public_http_reference_url("http://[::1/image.png") == ""


def test_fetch_url_validation_checks_resolved_addresses() -> None:
    error = validate_public_http_url(
        "https://public-name.example/resource",
        resolver=lambda _hostname: [ipaddress.ip_address("10.0.0.7")],
    )

    assert error is not None
    assert "private" in error.lower()


@pytest.mark.parametrize(
    ("payload_json", "expected_code", "expected_retryable"),
    [
        (brave_tools._brave_http_error("q", service_name="Brave", status_code=401), "provider_authentication_failed", False),
        (brave_tools._brave_http_error("q", service_name="Brave", status_code=429), "provider_rate_limited", True),
        (serper_tools._serper_http_error("q", 503), "provider_unavailable", True),
        (serper_tools._serper_http_error("q", 400), "provider_request_failed", False),
    ],
)
def test_http_provider_errors_use_stable_structured_contract(
    payload_json: str,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    payload = json.loads(payload_json)

    assert payload["error_code"] == expected_code
    assert payload["retryable"] is expected_retryable
    assert payload["provider"] in {"brave", "serper"}


def test_groundroute_fetch_rejects_private_url_before_provider_call(monkeypatch) -> None:
    monkeypatch.setattr(groundroute_tools, "_get_api_key", lambda _name: "key")
    provider_called = False

    def post_search(_api_key: str, _body: dict):
        nonlocal provider_called
        provider_called = True
        return {}

    monkeypatch.setattr(groundroute_tools, "_post_search", post_search)

    payload = json.loads(groundroute_tools.web_fetch_tool.func("http://2130706433/private"))

    assert payload["error_code"] == "url_not_public"
    assert provider_called is False


def _no_tool_config():
    return SimpleNamespace(get_tool_config=lambda _name: None)


def test_duckduckgo_image_result_uses_full_image_and_thumbnail_fields(monkeypatch) -> None:
    monkeypatch.setattr(image_search.tools, "get_app_config", _no_tool_config)
    monkeypatch.setattr(
        image_search.tools,
        "_search_images",
        lambda **_kwargs: [
            {
                "title": "reference",
                "image": "https://cdn.example.com/full.jpg",
                "thumbnail": "https://cdn.example.com/thumb.jpg",
            }
        ],
    )

    payload = json.loads(image_search.tools.image_search_tool.func("reference"))

    assert payload["results"] == [
        {
            "title": "reference",
            "image_url": "https://cdn.example.com/full.jpg",
            "thumbnail_url": "https://cdn.example.com/thumb.jpg",
        }
    ]


def test_duckduckgo_image_result_drops_private_reference_urls(monkeypatch) -> None:
    monkeypatch.setattr(image_search.tools, "get_app_config", _no_tool_config)
    monkeypatch.setattr(
        image_search.tools,
        "_search_images",
        lambda **_kwargs: [
            {
                "title": "unsafe",
                "image": "http://[::ffff:127.0.0.1]/full.jpg",
                "thumbnail": "http://2130706433/thumb.jpg",
            }
        ],
    )

    payload = json.loads(image_search.tools.image_search_tool.func("reference"))

    assert payload["error_code"] == "no_safe_results"
    assert payload["retryable"] is False


@pytest.mark.parametrize(
    ("module", "tool_name", "search_name"),
    [
        (ddg_search.tools, "web_search_tool", "_search_text"),
        (image_search.tools, "image_search_tool", "_search_images"),
    ],
)
def test_duckduckgo_provider_failure_is_distinct_from_no_results(
    monkeypatch,
    module,
    tool_name: str,
    search_name: str,
) -> None:
    monkeypatch.setattr(module, "get_app_config", _no_tool_config)

    def fail(**_kwargs):
        raise CommunityToolError(
            provider="duckduckgo",
            code="provider_unavailable",
            message="DuckDuckGo search is temporarily unavailable",
            retryable=True,
        )

    monkeypatch.setattr(module, search_name, fail)
    payload = json.loads(getattr(module, tool_name).func("query"))

    assert payload["error_code"] == "provider_unavailable"
    assert payload["provider"] == "duckduckgo"
    assert payload["retryable"] is True


@pytest.mark.parametrize(
    ("module", "tool_name", "search_name"),
    [
        (ddg_search.tools, "web_search_tool", "_search_text"),
        (image_search.tools, "image_search_tool", "_search_images"),
    ],
)
def test_duckduckgo_empty_result_has_non_retryable_no_results_code(
    monkeypatch,
    module,
    tool_name: str,
    search_name: str,
) -> None:
    monkeypatch.setattr(module, "get_app_config", _no_tool_config)
    monkeypatch.setattr(module, search_name, lambda **_kwargs: [])

    payload = json.loads(getattr(module, tool_name).func("query"))

    assert payload["error_code"] == "no_results"
    assert payload["provider"] == "duckduckgo"
    assert payload["retryable"] is False
