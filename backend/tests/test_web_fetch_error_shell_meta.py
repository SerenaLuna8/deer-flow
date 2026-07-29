"""HTTP error-page classification for real ``web_fetch`` result shapes."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import ToolMessage

from deerflow.agents.middlewares.tool_result_meta import TOOL_META_KEY, normalize_tool_message


def _normalize(content: str, *, name: str = "web_fetch") -> dict[str, object]:
    message = ToolMessage(
        content=content,
        tool_call_id="tool-call-1",
        name=name,
        status="success",
        additional_kwargs={},
    )
    return normalize_tool_message(message).additional_kwargs[TOOL_META_KEY]


@pytest.mark.parametrize(
    ("title", "error_type", "recoverable", "next_action"),
    [
        ("# 404 Not Found", "not_found", True, "rewrite_query"),
        ("# 403 Forbidden", "permission", True, "try_alternative"),
        ("# 503 Service Unavailable", "transient", False, "try_alternative"),
    ],
)
def test_web_fetch_http_error_title_is_classified(
    title: str,
    error_type: str,
    recoverable: bool,
    next_action: str,
) -> None:
    meta = _normalize(f"{title}\n\nserver boilerplate")

    assert meta == {
        "status": "error",
        "error_type": error_type,
        "recoverable_by_model": recoverable,
        "recommended_next_action": next_action,
        "source": "content_analysis",
    }


def test_legitimate_404_article_title_is_not_misclassified() -> None:
    meta = _normalize("# 404 Ways to Cook Rice\n\nA complete guide to cooking rice.")

    assert meta["status"] == "success"
    assert meta["error_type"] is None


def test_error_title_rule_is_scoped_to_web_fetch() -> None:
    meta = _normalize("# 404 Not Found", name="read_file")

    assert meta["status"] == "success"


@pytest.mark.parametrize(
    ("html", "expected_type"),
    [
        (
            "<html><head><title>404 Not Found</title></head><body><article><h1>404 Not Found</h1><p>nginx</p></article></body></html>",
            "not_found",
        ),
        (
            "<html><head><title>403 Forbidden</title></head><body><article><h1>403 Forbidden</h1><p>Apache</p></article></body></html>",
            "permission",
        ),
        (
            "<html><head><title>503 Service Unavailable</title></head><body><article><h1>503 Service Unavailable</h1><p>nginx</p></article></body></html>",
            "transient",
        ),
        (
            "<html><head><title>404 Ways to Cook Rice</title></head><body><article><h1>404 Ways to Cook Rice</h1><p>A practical guide.</p></article></body></html>",
            None,
        ),
    ],
)
def test_real_browserless_web_fetch_output_receives_expected_meta(
    html: str,
    expected_type: str | None,
) -> None:
    """Drive the real producer; only its network client and URL check are faked."""
    from deerflow.community.browserless import tools as browserless_tools

    client = MagicMock()
    client.fetch_html = AsyncMock(return_value=html)
    with (
        patch.object(browserless_tools, "_get_browserless_client", return_value=client),
        patch.object(browserless_tools, "_get_tool_config", return_value=None),
        patch.object(browserless_tools, "validate_public_http_url", return_value=None),
    ):
        rendered = asyncio.run(browserless_tools.web_fetch_tool.ainvoke("https://example.org/page"))

    meta = _normalize(rendered)
    assert meta["error_type"] == expected_type
    assert meta["status"] == ("success" if expected_type is None else "error")
