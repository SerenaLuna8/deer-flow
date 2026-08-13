import asyncio
import logging

from langchain.tools import tool
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError, community_error_json
from deerflow.community.jina_ai.jina_client import JinaClient
from deerflow.community.url_safety import sanitize_public_http_reference_url
from deerflow.config import get_app_config
from deerflow.utils.readability import ReadabilityExtractor

readability_extractor = ReadabilityExtractor()
logger = logging.getLogger(__name__)


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_timeout(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _coerce_proxy(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    proxy = value.strip()
    return proxy or None


@tool("web_fetch", parse_docstring=True)
async def web_fetch_tool(url: str) -> str:
    """Fetch the contents of a web page at a given URL.
    Only fetch EXACT URLs that have been provided directly by the user or have been returned in results from the web_search and web_fetch tools.
    This tool can NOT access content that requires authentication, such as private Google Docs or pages behind login walls.
    Do NOT add www. to URLs that do NOT have them.
    URLs must include the schema: https://example.com is a valid URL while example.com is an invalid URL.

    Args:
        url: The URL to fetch the contents of.
    """
    # Jina performs the target fetch in its remote SaaS. Reject unsafe literal
    # references without local DNS; a local downloader would instead require
    # validate_public_http_url and redirect revalidation.
    if not sanitize_public_http_reference_url(url):
        return community_error_json(
            CommunityToolError(
                provider="jina",
                code="url_not_public",
                message="Only public http(s) URLs may be fetched",
                retryable=False,
            ),
            query=url,
        )

    try:
        jina_client = JinaClient()
        timeout = 10
        proxy = None
        trust_env = True
        config = get_app_config().get_tool_config("web_fetch")
        if config is not None:
            timeout = _coerce_timeout(config.model_extra.get("timeout"), timeout)
            proxy = _coerce_proxy(config.model_extra.get("proxy"))
            trust_env = _coerce_bool(config.model_extra.get("trust_env"), trust_env)
        html_content = await jina_client.crawl(
            url,
            return_format="html",
            timeout=timeout,
            proxy=proxy,
            trust_env=trust_env,
        )
        article = await asyncio.to_thread(readability_extractor.extract_article, html_content)
        return article.to_markdown()[:4096]
    except GraphBubbleUp:
        raise
    except CommunityToolError as error:
        return community_error_json(error, query=url)
    except (FileNotFoundError, TypeError, ValueError) as error:
        logger.error("Jina configuration failed; provider_error_type=%s", type(error).__name__)
        return community_error_json(
            CommunityToolError(
                provider="jina",
                code="configuration_error",
                message="Jina is not configured correctly",
                retryable=False,
            ),
            query=url,
        )
    except Exception as error:
        logger.error("Jina tool failed; provider_error_type=%s", type(error).__name__)
        return community_error_json(
            CommunityToolError(
                provider="jina",
                code="provider_response_invalid",
                message="Jina returned unusable content",
                retryable=True,
            ),
            query=url,
        )
