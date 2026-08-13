import json
import logging
from typing import Any

import httpx
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError

logger = logging.getLogger(__name__)


def _crawl4ai_http_error(status_code: int) -> CommunityToolError:
    if status_code in {401, 403}:
        code, retryable = "provider_authentication_failed", False
    elif status_code == 429:
        code, retryable = "provider_rate_limited", True
    elif status_code >= 500:
        code, retryable = "provider_unavailable", True
    else:
        code, retryable = "provider_request_failed", False
    return CommunityToolError(
        provider="crawl4ai",
        code=code,
        message=f"Crawl4AI request failed with HTTP {status_code}",
        retryable=retryable,
    )


class Crawl4AiClient:
    """Client for a self-hosted Crawl4AI Docker server (POST /md)."""

    def __init__(self, base_url: str, token: str = "", timeout_s: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    async def fetch_markdown(self, url: str, filter_mode: str = "fit") -> str:
        """Fetch a page's clean markdown via Crawl4AI's POST /md endpoint.

        Args:
            url: The URL to fetch.
            filter_mode: Crawl4AI markdown filter ("fit", "raw", "bm25", "llm").

        Returns:
            Markdown content.

        Raises:
            CommunityToolError: When the provider fails or returns invalid data.
        """
        payload: dict[str, Any] = {"url": url, "f": filter_mode}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        logger.debug("Fetching public URL via Crawl4AI")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(f"{self.base_url}/md", json=payload, headers=headers)

                if resp.status_code != 200:
                    raise _crawl4ai_http_error(resp.status_code)

                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError) as error:
                    raise CommunityToolError(
                        provider="crawl4ai",
                        code="provider_response_invalid",
                        message="Crawl4AI returned an unexpected response format",
                        retryable=True,
                    ) from error

                if not isinstance(data, dict):
                    raise CommunityToolError(
                        provider="crawl4ai",
                        code="provider_response_invalid",
                        message="Crawl4AI returned an unexpected response format",
                        retryable=True,
                    )

                if not data.get("success", False):
                    raise CommunityToolError(
                        provider="crawl4ai",
                        code="provider_request_failed",
                        message="Crawl4AI could not fetch the requested URL",
                        retryable=False,
                    )

                markdown = data.get("markdown") or ""
                if not markdown.strip():
                    raise CommunityToolError(
                        provider="crawl4ai",
                        code="no_results",
                        message="Crawl4AI returned no content",
                        retryable=False,
                    )

                return markdown

        except GraphBubbleUp:
            raise
        except CommunityToolError:
            raise
        except httpx.TimeoutException as error:
            logger.error("Crawl4AI request timed out")
            raise CommunityToolError(
                provider="crawl4ai",
                code="provider_unavailable",
                message="Crawl4AI is temporarily unavailable",
                retryable=True,
            ) from error
        except httpx.RequestError as error:
            logger.error("Crawl4AI request failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="crawl4ai",
                code="provider_unavailable",
                message="Crawl4AI is temporarily unavailable",
                retryable=True,
            ) from error
        except Exception as error:
            logger.error("Crawl4AI fetch failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="crawl4ai",
                code="provider_unavailable",
                message="Crawl4AI is temporarily unavailable",
                retryable=True,
            ) from error
