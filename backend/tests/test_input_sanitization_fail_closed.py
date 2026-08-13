import logging

import pytest
from langgraph.errors import GraphBubbleUp

from deerflow.agents.middlewares.input_sanitization_middleware import (
    INPUT_SANITIZATION_FAILURE_CODE,
    INPUT_SANITIZATION_FAILURE_MESSAGE,
    InputSanitizationError,
    InputSanitizationMiddleware,
)


def _raise_internal_error(_request: object) -> object:
    raise ValueError("internal-sensitive-detail")


def test_input_sanitization_failure_is_non_recoverable_graph_control_flow() -> None:
    assert issubclass(InputSanitizationError, GraphBubbleUp)


def test_input_sanitization_internal_error_blocks_sync_model_call(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = InputSanitizationMiddleware()
    handler_called = False

    def handler(_request: object) -> object:
        nonlocal handler_called
        handler_called = True
        return object()

    monkeypatch.setattr(middleware, "_process_request", _raise_internal_error)

    with caplog.at_level(logging.ERROR), pytest.raises(InputSanitizationError) as raised:
        middleware.wrap_model_call(object(), handler)  # type: ignore[arg-type]

    assert not handler_called
    assert raised.value.code == INPUT_SANITIZATION_FAILURE_CODE
    assert str(raised.value) == INPUT_SANITIZATION_FAILURE_MESSAGE
    assert "internal-sensitive-detail" not in str(raised.value)
    assert "disposition=fail_closed" in caplog.text


@pytest.mark.asyncio
async def test_input_sanitization_internal_error_blocks_async_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    middleware = InputSanitizationMiddleware()
    handler_called = False

    async def handler(_request: object) -> object:
        nonlocal handler_called
        handler_called = True
        return object()

    monkeypatch.setattr(middleware, "_process_request", _raise_internal_error)

    with pytest.raises(InputSanitizationError) as raised:
        await middleware.awrap_model_call(object(), handler)  # type: ignore[arg-type]

    assert not handler_called
    assert raised.value.code == INPUT_SANITIZATION_FAILURE_CODE
    assert str(raised.value) == INPUT_SANITIZATION_FAILURE_MESSAGE
