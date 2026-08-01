"""Web tool provider definitions for the setup wizard.

Model definitions deliberately do not live in the setup wizard. A system
administrator manages the versioned model catalog and its Credential bindings
through ``/admin/settings/models`` after PostgreSQL initialization.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WebProvider:
    name: str
    display_name: str
    description: str
    use: str
    env_var: str | None
    tool_name: str
    extra_config: dict = field(default_factory=dict)


@dataclass
class SearchProvider:
    name: str
    display_name: str
    description: str
    use: str
    env_var: str | None
    tool_name: str = "web_search"
    extra_config: dict = field(default_factory=dict)


SEARCH_PROVIDERS: list[SearchProvider] = [
    SearchProvider(
        name="ddg",
        display_name="DuckDuckGo (free, no key needed)",
        description="No API key required",
        use="deerflow.community.ddg_search.tools:web_search_tool",
        env_var=None,
        extra_config={"max_results": 5},
    ),
    SearchProvider(
        name="tavily",
        display_name="Tavily",
        description="Recommended, free tier available",
        use="deerflow.community.tavily.tools:web_search_tool",
        env_var="TAVILY_API_KEY",
        extra_config={"max_results": 5},
    ),
    SearchProvider(
        name="infoquest",
        display_name="InfoQuest",
        description="Higher quality vertical search, API key required",
        use="deerflow.community.infoquest.tools:web_search_tool",
        env_var="INFOQUEST_API_KEY",
        extra_config={"search_time_range": 10},
    ),
    SearchProvider(
        name="exa",
        display_name="Exa",
        description="Neural + keyword web search, API key required",
        use="deerflow.community.exa.tools:web_search_tool",
        env_var="EXA_API_KEY",
        extra_config={
            "max_results": 5,
            "search_type": "auto",
            "contents_max_characters": 1000,
        },
    ),
    SearchProvider(
        name="firecrawl",
        display_name="Firecrawl",
        description="Search + crawl via Firecrawl API",
        use="deerflow.community.firecrawl.tools:web_search_tool",
        env_var="FIRECRAWL_API_KEY",
        extra_config={"max_results": 5},
    ),
    SearchProvider(
        name="fastcrw",
        display_name="fastCRW",
        description="Firecrawl-compatible web scraper, single binary, self-host or cloud",
        use="deerflow.community.fastcrw.tools:web_search_tool",
        env_var="CRW_API_KEY",
        extra_config={"max_results": 5},
    ),
    SearchProvider(
        name="brave",
        display_name="Brave Search",
        description="Independent index, official API, API key required",
        use="deerflow.community.brave.tools:web_search_tool",
        env_var="BRAVE_SEARCH_API_KEY",
        extra_config={"max_results": 5},
    ),
    SearchProvider(
        name="groundroute",
        display_name="GroundRoute",
        description="One key across six engines, price-routed with failover, API key required",
        use="deerflow.community.groundroute.tools:web_search_tool",
        env_var="GROUNDROUTE_API_KEY",
        extra_config={"max_results": 5},
    ),
]

WEB_FETCH_PROVIDERS: list[WebProvider] = [
    WebProvider(
        name="jina_ai",
        display_name="Jina AI Reader",
        description="Good default reader, no API key required",
        use="deerflow.community.jina_ai.tools:web_fetch_tool",
        env_var=None,
        tool_name="web_fetch",
        extra_config={"timeout": 10},
    ),
    WebProvider(
        name="exa",
        display_name="Exa",
        description="API key required",
        use="deerflow.community.exa.tools:web_fetch_tool",
        env_var="EXA_API_KEY",
        tool_name="web_fetch",
    ),
    WebProvider(
        name="infoquest",
        display_name="InfoQuest",
        description="API key required",
        use="deerflow.community.infoquest.tools:web_fetch_tool",
        env_var="INFOQUEST_API_KEY",
        tool_name="web_fetch",
        extra_config={
            "timeout": 10,
            "fetch_time": 10,
            "navigation_timeout": 30,
        },
    ),
    WebProvider(
        name="firecrawl",
        display_name="Firecrawl",
        description="Search-grade crawl with markdown output, API key required",
        use="deerflow.community.firecrawl.tools:web_fetch_tool",
        env_var="FIRECRAWL_API_KEY",
        tool_name="web_fetch",
    ),
    WebProvider(
        name="groundroute",
        display_name="GroundRoute",
        description="Page fetch via routed engines, API key required",
        use="deerflow.community.groundroute.tools:web_fetch_tool",
        env_var="GROUNDROUTE_API_KEY",
        tool_name="web_fetch",
    ),
    WebProvider(
        name="fastcrw",
        display_name="fastCRW",
        description="Firecrawl-compatible web scraper with markdown output, self-host or cloud",
        use="deerflow.community.fastcrw.tools:web_fetch_tool",
        env_var="CRW_API_KEY",
        tool_name="web_fetch",
    ),
    WebProvider(
        name="crawl4ai",
        display_name="Crawl4AI",
        description="Self-hosted headless Chromium with markdown output, no API key required",
        use="deerflow.community.crawl4ai.tools:web_fetch_tool",
        env_var=None,
        tool_name="web_fetch",
        extra_config={
            "base_url": "http://localhost:11235",
            "timeout": 30,
        },
    ),
]
