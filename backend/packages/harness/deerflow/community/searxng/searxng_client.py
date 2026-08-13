import logging
from typing import Any

import httpx
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError

logger = logging.getLogger(__name__)


def _searxng_http_error(status_code: int) -> CommunityToolError:
    if status_code in {401, 403}:
        code, retryable = "provider_authentication_failed", False
    elif status_code == 429:
        code, retryable = "provider_rate_limited", True
    elif status_code >= 500:
        code, retryable = "provider_unavailable", True
    else:
        code, retryable = "provider_request_failed", False
    return CommunityToolError(
        provider="searxng",
        code=code,
        message=f"SearXNG request failed with HTTP {status_code}",
        retryable=retryable,
    )


class SearxngClient:
    """Client for SearXNG meta search engine API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    async def search(
        self,
        query: str,
        max_results: int = 5,
        categories: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Search the web using SearXNG.

        Args:
            query: The search query.
            max_results: Maximum number of results to return.
            categories: Search categories to use.

        Returns:
            List of search result dictionaries.
        """
        params: dict[str, Any] = {
            "q": query,
            "format": "json",
            "language": "auto",
            "pageno": 1,
        }
        if max_results:
            params["limit"] = max_results
        if categories:
            params["categories"] = ",".join(categories)

        logger.debug("Searching via configured SearXNG endpoint")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{self.base_url}/search",
                    params=params,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; ActWeave/1.0)",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    raise CommunityToolError(
                        provider="searxng",
                        code="provider_response_invalid",
                        message="SearXNG returned an unexpected response format",
                        retryable=True,
                    )
                results = data.get("results", [])
                if not isinstance(results, list):
                    raise CommunityToolError(
                        provider="searxng",
                        code="provider_response_invalid",
                        message="SearXNG returned an unexpected response format",
                        retryable=True,
                    )
                return results[:max_results] if max_results else results
        except GraphBubbleUp:
            raise
        except CommunityToolError:
            raise
        except httpx.HTTPStatusError as error:
            logger.error("SearXNG returned HTTP %s", error.response.status_code)
            raise _searxng_http_error(error.response.status_code) from error
        except httpx.RequestError as error:
            logger.error("SearXNG request failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="searxng",
                code="provider_unavailable",
                message="SearXNG is temporarily unavailable",
                retryable=True,
            ) from error
        except (TypeError, ValueError) as error:
            logger.error("SearXNG response parsing failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="searxng",
                code="provider_response_invalid",
                message="SearXNG returned an unexpected response format",
                retryable=True,
            ) from error
        except Exception as error:
            logger.error("SearXNG search failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="searxng",
                code="provider_unavailable",
                message="SearXNG is temporarily unavailable",
                retryable=True,
            ) from error
