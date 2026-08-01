"""Focused regression tests for exception-detail confidentiality."""

import logging
from types import SimpleNamespace

from deerflow.agents.middlewares.tool_error_handling_middleware import (
    ToolErrorHandlingMiddleware,
)


def test_tool_exception_secret_is_absent_from_logs_and_tool_message(
    caplog,
) -> None:
    sentinel = "TOOL-EXCEPTION-SECRET-4JQ8"
    request = SimpleNamespace(
        tool_call={
            "name": "web_search",
            "id": "call-secret",
            "args": {},
        }
    )

    def handler(_request):
        raise RuntimeError(f"Authorization: Bearer {sentinel}")

    with caplog.at_level(
        logging.ERROR,
        logger="deerflow.agents.middlewares.tool_error_handling_middleware",
    ):
        result = ToolErrorHandlingMiddleware().wrap_tool_call(request, handler)

    exposed = f"{result!r}\n{caplog.text}"
    assert sentinel not in exposed
