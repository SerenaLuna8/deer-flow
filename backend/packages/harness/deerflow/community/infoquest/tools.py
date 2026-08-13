import logging

from langchain.tools import tool
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError, community_error_json
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

from .infoquest_client import InfoQuestClient

readability_extractor = ReadabilityExtractor()
logger = logging.getLogger(__name__)


def _infoquest_error(context: str, error: Exception) -> str:
    if isinstance(error, CommunityToolError):
        return community_error_json(error, query=context)
    logger.error("InfoQuest tool failed; provider_error_type=%s", type(error).__name__)
    if isinstance(error, (FileNotFoundError, TypeError, ValueError)):
        code, message, retryable = (
            "configuration_error",
            "InfoQuest is not configured correctly",
            False,
        )
    else:
        code, message, retryable = (
            "provider_unavailable",
            "InfoQuest is temporarily unavailable",
            True,
        )
    return community_error_json(
        CommunityToolError(
            provider="infoquest",
            code=code,
            message=message,
            retryable=retryable,
        ),
        query=context,
    )


def _get_infoquest_client() -> InfoQuestClient:
    search_config = get_app_config().get_tool_config("web_search")
    search_time_range = -1
    search_extra = (search_config.model_extra or {}) if search_config is not None else {}
    if "search_time_range" in search_extra:
        search_time_range = search_extra.get("search_time_range")

    fetch_config = get_app_config().get_tool_config("web_fetch")
    fetch_time = -1
    fetch_extra = (fetch_config.model_extra or {}) if fetch_config is not None else {}
    if "fetch_time" in fetch_extra:
        fetch_time = fetch_extra.get("fetch_time")
    fetch_timeout = -1
    if "timeout" in fetch_extra:
        fetch_timeout = fetch_extra.get("timeout")
    navigation_timeout = -1
    if "navigation_timeout" in fetch_extra:
        navigation_timeout = fetch_extra.get("navigation_timeout")

    image_search_config = get_app_config().get_tool_config("image_search")
    image_search_time_range = -1
    image_extra = (image_search_config.model_extra or {}) if image_search_config is not None else {}
    if "image_search_time_range" in image_extra:
        image_search_time_range = image_extra.get("image_search_time_range")
    image_size = "i"
    if "image_size" in image_extra:
        image_size = image_extra.get("image_size")

    return InfoQuestClient(
        search_time_range=search_time_range,
        fetch_timeout=fetch_timeout,
        fetch_navigation_timeout=navigation_timeout,
        fetch_time=fetch_time,
        image_search_time_range=image_search_time_range,
        image_size=image_size,
    )


@tool("web_search", parse_docstring=True)
def web_search_tool(query: str) -> str:
    """Search the web.

    Args:
        query: The query to search for.
    """

    try:
        client = _get_infoquest_client()
        return client.web_search(query)
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _infoquest_error(query, error)


@tool("web_fetch", parse_docstring=True)
def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    # InfoQuest performs the target fetch in remote SaaS. Reject unsafe literal
    # references without local DNS; a local downloader must instead perform
    # DNS-aware validation and validate every redirect.
    if not sanitize_public_http_reference_url(url):
        return community_error_json(
            CommunityToolError(
                provider="infoquest",
                code="url_not_public",
                message="Only public http(s) URLs may be fetched",
                retryable=False,
            ),
            query=url,
        )
    try:
        client = _get_infoquest_client()
        result = client.fetch(url)
        article = readability_extractor.extract_article(result)
        return article.to_markdown()[:4096]
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _infoquest_error(url, error)


@tool("image_search", parse_docstring=True)
def image_search_tool(query: str) -> str:
    """Search for images online. Use this tool BEFORE image generation to find reference images for characters, portraits, objects, scenes, or any content requiring visual accuracy.

    **When to use:**
    - Before generating character/portrait images: search for similar poses, expressions, styles
    - Before generating specific objects/products: search for accurate visual references
    - Before generating scenes/locations: search for architectural or environmental references
    - Before generating fashion/clothing: search for style and detail references

    The returned image URLs can be used as reference images in image generation to significantly improve quality.

    Args:
        query: The query to search for images.
    """
    try:
        client = _get_infoquest_client()
        return client.image_search(query)
    except GraphBubbleUp:
        raise
    except Exception as error:
        return _infoquest_error(query, error)
