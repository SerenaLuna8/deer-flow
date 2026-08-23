from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langgraph.errors import GraphBubbleUp

from deerflow.community.browserless import tools as browserless_tools
from deerflow.community.browserless.browserless_client import _browserless_http_error
from deerflow.community.crawl4ai import tools as crawl4ai_tools
from deerflow.community.crawl4ai.crawl4ai_client import _crawl4ai_http_error
from deerflow.community.errors import CommunityToolError
from deerflow.community.exa import tools as exa_tools
from deerflow.community.fastcrw import tools as fastcrw_tools
from deerflow.community.firecrawl import tools as firecrawl_tools
from deerflow.community.infoquest import tools as infoquest_tools
from deerflow.community.infoquest.infoquest_client import (
    InfoQuestClient,
    _infoquest_http_error,
)
from deerflow.community.jina_ai import tools as jina_tools
from deerflow.community.jina_ai.jina_client import _jina_http_error
from deerflow.community.searxng import tools as searxng_tools
from deerflow.community.searxng.searxng_client import _searxng_http_error
from deerflow.community.tavily import tools as tavily_tools
from deerflow.runtime.context_keys import RuntimeContextKeys

_PRIVATE_URL = "http://2130706433/internal"
_SECRET_EXCEPTION = "secret-provider-detail"


@pytest.fixture(autouse=True)
def _without_tool_config(monkeypatch) -> None:
    config = SimpleNamespace(get_tool_config=lambda _name: None)
    for module in (
        browserless_tools,
        crawl4ai_tools,
        exa_tools,
        fastcrw_tools,
        firecrawl_tools,
        infoquest_tools,
        jina_tools,
        searxng_tools,
        tavily_tools,
    ):
        monkeypatch.setattr(module, "get_app_config", lambda: config)


def _sync_tool_call(tool, *args):
    assert tool.func is not None
    return tool.func(*args)


async def _async_tool_call(tool, *args):
    callable_ = tool.coroutine or tool.func
    assert callable_ is not None
    result = callable_(*args)
    if hasattr(result, "__await__"):
        return await result
    return result


def _error_payload(raw: str, provider: str, code: str) -> dict:
    payload = json.loads(raw)
    assert payload["provider"] == provider
    assert payload["error_code"] == code
    assert _SECRET_EXCEPTION not in raw
    return payload


@pytest.mark.parametrize(
    ("module", "getter_name", "tool_name", "provider"),
    [
        (exa_tools, "_get_exa_client", "web_fetch_tool", "exa"),
        (firecrawl_tools, "_get_firecrawl_client", "web_fetch_tool", "firecrawl"),
        (tavily_tools, "_get_tavily_client", "web_fetch_tool", "tavily"),
        (infoquest_tools, "_get_infoquest_client", "web_fetch_tool", "infoquest"),
    ],
)
def test_remote_saas_fetch_rejects_private_reference_before_provider_call(
    monkeypatch,
    module,
    getter_name: str,
    tool_name: str,
    provider: str,
) -> None:
    provider_called = False

    def forbidden_client(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(module, getter_name, forbidden_client)

    payload = _error_payload(
        _sync_tool_call(getattr(module, tool_name), _PRIVATE_URL),
        provider,
        "url_not_public",
    )

    assert payload["retryable"] is False
    assert provider_called is False


@pytest.mark.parametrize(
    ("module", "client_name", "tool_name", "provider"),
    [
        (fastcrw_tools, "_get_fastcrw_client", "web_fetch_tool", "fastcrw"),
        (crawl4ai_tools, "_build_client", "web_fetch_tool", "crawl4ai"),
        (browserless_tools, "_get_browserless_client", "web_fetch_tool", "browserless"),
    ],
)
@pytest.mark.asyncio
async def test_delegated_local_fetch_rejects_private_url_with_stable_contract(
    monkeypatch,
    module,
    client_name: str,
    tool_name: str,
    provider: str,
) -> None:
    provider_called = False

    def forbidden_client(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(module, client_name, forbidden_client)
    result = await _async_tool_call(getattr(module, tool_name), _PRIVATE_URL)

    payload = _error_payload(result, provider, "url_not_public")
    assert payload["retryable"] is False
    assert provider_called is False


@pytest.mark.asyncio
async def test_jina_remote_fetch_rejects_private_reference_before_client_init(
    monkeypatch,
) -> None:
    provider_called = False

    class ForbiddenClient:
        def __init__(self) -> None:
            nonlocal provider_called
            provider_called = True

    monkeypatch.setattr(jina_tools, "JinaClient", ForbiddenClient)

    payload = _error_payload(
        await _async_tool_call(jina_tools.web_fetch_tool, _PRIVATE_URL),
        "jina",
        "url_not_public",
    )
    assert payload["retryable"] is False
    assert provider_called is False


@pytest.mark.parametrize(
    ("module", "getter_name", "provider"),
    [
        (exa_tools, "_get_exa_client", "exa"),
        (firecrawl_tools, "_get_firecrawl_client", "firecrawl"),
        (fastcrw_tools, "_get_fastcrw_client", "fastcrw"),
        (tavily_tools, "_get_tavily_client", "tavily"),
        (infoquest_tools, "_get_infoquest_client", "infoquest"),
    ],
)
def test_sync_search_provider_failure_never_returns_raw_exception(
    monkeypatch,
    module,
    getter_name: str,
    provider: str,
) -> None:
    class FailingClient:
        def search(self, *_args, **_kwargs):
            raise RuntimeError(_SECRET_EXCEPTION)

        def web_search(self, *_args, **_kwargs):
            raise RuntimeError(_SECRET_EXCEPTION)

    monkeypatch.setattr(module, getter_name, lambda *_args, **_kwargs: FailingClient())

    payload = _error_payload(
        _sync_tool_call(module.web_search_tool, "query"),
        provider,
        "provider_unavailable",
    )
    assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_searxng_failure_uses_stable_provider_contract(monkeypatch) -> None:
    class FailingClient:
        async def search(self, *_args, **_kwargs):
            raise RuntimeError(_SECRET_EXCEPTION)

    monkeypatch.setattr(searxng_tools, "_get_searxng_client", lambda: FailingClient())

    payload = _error_payload(
        await _async_tool_call(searxng_tools.web_search_tool, "query"),
        "searxng",
        "provider_unavailable",
    )
    assert payload["retryable"] is True


@pytest.mark.parametrize(
    ("module", "getter_name"),
    [
        (exa_tools, "_get_exa_client"),
        (firecrawl_tools, "_get_firecrawl_client"),
        (fastcrw_tools, "_get_fastcrw_client"),
        (tavily_tools, "_get_tavily_client"),
    ],
)
def test_graph_bubble_up_is_not_converted_to_provider_error(
    monkeypatch,
    module,
    getter_name: str,
) -> None:
    class BubbleClient:
        def search(self, *_args, **_kwargs):
            raise GraphBubbleUp()

    monkeypatch.setattr(module, getter_name, lambda *_args, **_kwargs: BubbleClient())

    with pytest.raises(GraphBubbleUp):
        _sync_tool_call(module.web_search_tool, "query")


@pytest.mark.asyncio
async def test_async_fetch_graph_bubble_up_is_preserved(monkeypatch) -> None:
    class BubbleClient:
        async def fetch_markdown(self, *_args, **_kwargs):
            raise GraphBubbleUp()

    monkeypatch.setattr(crawl4ai_tools, "_build_client", lambda _cfg: BubbleClient())
    monkeypatch.setattr(crawl4ai_tools, "validate_public_http_url", lambda *_args, **_kwargs: None)

    with pytest.raises(GraphBubbleUp):
        await _async_tool_call(crawl4ai_tools.web_fetch_tool, "https://example.com")


def test_typed_provider_error_is_serialized_without_losing_classification(
    monkeypatch,
) -> None:
    class FailingClient:
        def search(self, *_args, **_kwargs):
            raise CommunityToolError(
                provider="exa",
                code="provider_rate_limited",
                message="Exa API rate limit exceeded",
                retryable=True,
            )

    monkeypatch.setattr(exa_tools, "_get_exa_client", lambda *_args: FailingClient())

    payload = _error_payload(
        _sync_tool_call(exa_tools.web_search_tool, "query"),
        "exa",
        "provider_rate_limited",
    )
    assert payload["retryable"] is True


@pytest.mark.parametrize(
    ("module", "getter_name", "empty_result", "provider"),
    [
        (exa_tools, "_get_exa_client", SimpleNamespace(results=[]), "exa"),
        (firecrawl_tools, "_get_firecrawl_client", SimpleNamespace(web=[]), "firecrawl"),
        (fastcrw_tools, "_get_fastcrw_client", SimpleNamespace(web=[]), "fastcrw"),
        (tavily_tools, "_get_tavily_client", {"results": []}, "tavily"),
    ],
)
def test_empty_search_results_use_non_retryable_no_results(
    monkeypatch,
    module,
    getter_name: str,
    empty_result,
    provider: str,
) -> None:
    class EmptyClient:
        def search(self, *_args, **_kwargs):
            return empty_result

    monkeypatch.setattr(module, getter_name, lambda *_args, **_kwargs: EmptyClient())

    payload = _error_payload(
        _sync_tool_call(module.web_search_tool, "query"),
        provider,
        "no_results",
    )
    assert payload["retryable"] is False


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_retryable"),
    [
        (_crawl4ai_http_error(401), "provider_authentication_failed", False),
        (_browserless_http_error(429), "provider_rate_limited", True),
        (_jina_http_error(503), "provider_unavailable", True),
        (_searxng_http_error(400), "provider_request_failed", False),
        (_infoquest_http_error(403), "provider_authentication_failed", False),
    ],
)
def test_http_clients_classify_auth_rate_limit_and_availability(
    error: CommunityToolError,
    expected_code: str,
    expected_retryable: bool,
) -> None:
    assert error.code == expected_code
    assert error.retryable is expected_retryable


@pytest.mark.parametrize(
    ("module", "helper_name", "provider"),
    [
        (exa_tools, "_exa_error", "exa"),
        (firecrawl_tools, "_firecrawl_error", "firecrawl"),
        (fastcrw_tools, "_fastcrw_error", "fastcrw"),
        (tavily_tools, "_tavily_error", "tavily"),
    ],
)
def test_sdk_http_rate_limit_uses_stable_classification(
    module,
    helper_name: str,
    provider: str,
) -> None:
    error = RuntimeError(_SECRET_EXCEPTION)
    error.response = SimpleNamespace(status_code=429)

    payload = _error_payload(
        getattr(module, helper_name)("query", error),
        provider,
        "provider_rate_limited",
    )
    assert payload["retryable"] is True


def test_infoquest_reference_cleaner_drops_private_result_urls() -> None:
    results = InfoQuestClient.clean_results(
        [
            {
                "content": {
                    "results": {
                        "organic": [
                            {"title": "unsafe", "url": _PRIVATE_URL},
                            {
                                "title": "public",
                                "url": "https://example.com/public",
                            },
                        ]
                    }
                }
            }
        ]
    )

    assert results == [
        {
            "type": "page",
            "title": "public",
            "url": "https://example.com/public",
        }
    ]


@pytest.mark.parametrize(
    ("module", "getter_name", "unsafe_result", "provider"),
    [
        (
            exa_tools,
            "_get_exa_client",
            SimpleNamespace(
                results=[SimpleNamespace(title="unsafe", url=_PRIVATE_URL, highlights=[])],
            ),
            "exa",
        ),
        (
            firecrawl_tools,
            "_get_firecrawl_client",
            SimpleNamespace(
                web=[SimpleNamespace(title="unsafe", url=_PRIVATE_URL, description="")],
            ),
            "firecrawl",
        ),
        (
            fastcrw_tools,
            "_get_fastcrw_client",
            SimpleNamespace(
                web=[SimpleNamespace(title="unsafe", url=_PRIVATE_URL, description="")],
            ),
            "fastcrw",
        ),
        (
            tavily_tools,
            "_get_tavily_client",
            {
                "results": [
                    {"title": "unsafe", "url": _PRIVATE_URL, "content": ""},
                ]
            },
            "tavily",
        ),
    ],
)
def test_search_providers_drop_unsafe_reference_urls(
    monkeypatch,
    module,
    getter_name: str,
    unsafe_result,
    provider: str,
) -> None:
    class UnsafeClient:
        def search(self, *_args, **_kwargs):
            return unsafe_result

    monkeypatch.setattr(module, getter_name, lambda *_args, **_kwargs: UnsafeClient())

    payload = _error_payload(
        _sync_tool_call(module.web_search_tool, "query"),
        provider,
        "no_safe_results",
    )
    assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_searxng_drops_unsafe_reference_urls(monkeypatch) -> None:
    class UnsafeClient:
        async def search(self, *_args, **_kwargs):
            return [{"title": "unsafe", "url": _PRIVATE_URL, "content": ""}]

    monkeypatch.setattr(searxng_tools, "_get_searxng_client", lambda: UnsafeClient())

    payload = _error_payload(
        await _async_tool_call(searxng_tools.web_search_tool, "query"),
        "searxng",
        "no_safe_results",
    )
    assert payload["retryable"] is False


@pytest.mark.asyncio
async def test_browserless_capture_private_url_returns_structured_tool_message(
    monkeypatch,
) -> None:
    provider_called = False

    def forbidden_client(*_args, **_kwargs):
        nonlocal provider_called
        provider_called = True
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(browserless_tools, "_get_browserless_client", forbidden_client)
    command = await _async_tool_call(
        browserless_tools.web_capture_tool,
        SimpleNamespace(state=None, context=None),
        _PRIVATE_URL,
        "tool-call",
    )
    message = command.update["messages"][0]
    payload = _error_payload(message.content, "browserless", "url_not_public")

    assert payload["retryable"] is False
    assert provider_called is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("is_subagent", "expected_presentations"),
    [(True, 0), (False, 1)],
)
async def test_browserless_capture_only_lead_records_delivery_intent(
    monkeypatch: pytest.MonkeyPatch,
    is_subagent: bool,
    expected_presentations: int,
) -> None:
    class Authority:
        def __init__(self) -> None:
            self.writes: list[tuple[str, bytes]] = []
            self.presentations: list[tuple[tuple[str, ...], str]] = []

        async def write_output(self, relative_path: str, content: bytes) -> str:
            self.writes.append((relative_path, content))
            return f"/mnt/user-data/outputs/{relative_path}"

        async def record_presented_paths(
            self,
            paths: tuple[str, ...],
            *,
            tool_call_id: str,
        ) -> None:
            self.presentations.append((paths, tool_call_id))

    class Client:
        async def capture_screenshot(self, **_kwargs):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                content=b"image",
                target_status_code="200",
                target_status="OK",
            )

    authority = Authority()
    context = {
        "private_scope": object(),
        "__file_authority": authority,
    }
    if is_subagent:
        context[RuntimeContextKeys.IS_SUBAGENT] = True
    runtime = SimpleNamespace(
        state={
            "thread_data": {
                "outputs_path": "/mnt/user-data/outputs",
            },
        },
        context=context,
    )
    monkeypatch.setattr(
        browserless_tools,
        "_validate_capture_url",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        browserless_tools,
        "_get_browserless_client",
        lambda *_args, **_kwargs: Client(),
    )

    command = await _async_tool_call(
        browserless_tools.web_capture_tool,
        runtime,
        "https://example.com/report",
        "capture-1",
        "report.png",
    )

    assert authority.writes == [("report.png", b"image")]
    assert len(authority.presentations) == expected_presentations
    assert command.update["artifacts"] == [
        "/mnt/user-data/outputs/report.png",
    ]
    assert ".deerflow" not in repr(command.update)
