import logging
from dataclasses import dataclass
from typing import Any

import httpx
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError

logger = logging.getLogger(__name__)


def _browserless_http_error(status_code: int) -> CommunityToolError:
    if status_code in {401, 403}:
        code, retryable = "provider_authentication_failed", False
    elif status_code == 429:
        code, retryable = "provider_rate_limited", True
    elif status_code >= 500:
        code, retryable = "provider_unavailable", True
    else:
        code, retryable = "provider_request_failed", False
    return CommunityToolError(
        provider="browserless",
        code=code,
        message=f"Browserless request failed with HTTP {status_code}",
        retryable=retryable,
    )


@dataclass(frozen=True)
class BrowserlessFetchResult:
    html: str
    target_status_code: str
    target_status: str


@dataclass(frozen=True)
class BrowserlessScreenshotResult:
    content: bytes
    content_type: str
    target_status_code: str
    target_status: str
    final_url: str


def _get_header(headers: Any, name: str) -> str:
    value = headers.get(name)
    if value:
        return str(value)
    return str(headers.get(name.lower(), ""))


class BrowserlessClient:
    """Client for Browserless headless Chrome API."""

    def __init__(self, base_url: str, token: str = "", timeout_s: float = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    async def fetch_html(
        self,
        url: str,
        wait_for_event: str = "",
        wait_for_timeout_ms: int = 0,
        wait_for_selector: str = "",
        wait_for_selector_timeout_ms: int = 5000,
        reject_resource_types: list[str] | None = None,
        reject_request_pattern: list[str] | None = None,
    ) -> str:
        """Fetch rendered HTML while preserving the public string contract."""

        result = await self.fetch_html_with_status(
            url=url,
            wait_for_event=wait_for_event,
            wait_for_timeout_ms=wait_for_timeout_ms,
            wait_for_selector=wait_for_selector,
            wait_for_selector_timeout_ms=wait_for_selector_timeout_ms,
            reject_resource_types=reject_resource_types,
            reject_request_pattern=reject_request_pattern,
        )
        return result.html

    async def fetch_html_with_status(
        self,
        url: str,
        wait_for_event: str = "",
        wait_for_timeout_ms: int = 0,
        wait_for_selector: str = "",
        wait_for_selector_timeout_ms: int = 5000,
        reject_resource_types: list[str] | None = None,
        reject_request_pattern: list[str] | None = None,
    ) -> BrowserlessFetchResult:
        """Fetch rendered HTML and expose the target page's real status.

        Only sends accepted parameters for the current Browserless API version.
        Sets a default navigation timeout (30s) via query param.

        Args:
            url: The URL to fetch.
            wait_for_event: Wait for a page event (e.g. "networkidle", "load").
            wait_for_timeout_ms: Extra wait after page load.
            wait_for_selector: CSS selector to wait for.
            wait_for_selector_timeout_ms: Timeout for selector wait.
            reject_resource_types: Resource types to block (e.g. ["image"]).
            reject_request_pattern: URL patterns to block.

        Returns:
            A status-aware fetch result.

        Raises:
            CommunityToolError: When Browserless fails or returns no content.
        """
        payload: dict[str, Any] = {
            "url": url,
        }

        if self.token:
            payload["token"] = self.token
        if wait_for_event:
            payload["waitForEvent"] = wait_for_event
        if wait_for_timeout_ms > 0:
            payload["waitForTimeout"] = wait_for_timeout_ms
        if wait_for_selector:
            payload["waitForSelector"] = {
                "selector": wait_for_selector,
                "timeout": wait_for_selector_timeout_ms,
            }
        if reject_resource_types:
            payload["rejectResourceTypes"] = reject_resource_types
        if reject_request_pattern:
            payload["rejectRequestPattern"] = reject_request_pattern

        logger.debug("Fetching public URL via Browserless")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/content",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                    },
                )

                code = resp.status_code
                target_code = resp.headers.get("X-Response-Code", "")
                target_status = resp.headers.get("X-Response-Status", "")

                logger.debug(f"Browserless response: code={code}, target_code={target_code}, target_status={target_status}")

                if code != 200:
                    raise _browserless_http_error(code)

                html = resp.text
                if not html or not html.strip():
                    raise CommunityToolError(
                        provider="browserless",
                        code="no_results",
                        message="Browserless returned no content",
                        retryable=False,
                    )

                return BrowserlessFetchResult(
                    html=html,
                    target_status_code=_get_header(
                        resp.headers,
                        "X-Response-Code",
                    ),
                    target_status=_get_header(
                        resp.headers,
                        "X-Response-Status",
                    ),
                )

        except GraphBubbleUp:
            raise
        except CommunityToolError:
            raise
        except httpx.TimeoutException as error:
            logger.error("Browserless request timed out")
            raise CommunityToolError(
                provider="browserless",
                code="provider_unavailable",
                message="Browserless is temporarily unavailable",
                retryable=True,
            ) from error
        except httpx.RequestError as error:
            logger.error("Browserless request failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="browserless",
                code="provider_unavailable",
                message="Browserless is temporarily unavailable",
                retryable=True,
            ) from error
        except Exception as error:
            logger.error("Browserless fetch failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="browserless",
                code="provider_unavailable",
                message="Browserless is temporarily unavailable",
                retryable=True,
            ) from error

    async def capture_screenshot(
        self,
        url: str,
        full_page: bool = True,
        output_format: str = "png",
        quality: int | None = None,
        viewport: dict[str, int] | None = None,
        wait_for_selector: str = "",
        wait_for_selector_timeout_ms: int = 5000,
        wait_for_timeout_ms: int = 0,
        best_attempt: bool = False,
    ) -> BrowserlessScreenshotResult:
        """Capture a rendered screenshot of a URL using Browserless.

        Args:
            url: URL to render.
            full_page: Capture the full page instead of just the viewport.
            output_format: Image format: png, jpeg, or webp.
            quality: Optional quality for jpeg/webp outputs.
            viewport: Optional browser viewport dictionary.
            wait_for_selector: CSS selector to wait for before capture.
            wait_for_selector_timeout_ms: Timeout for selector wait.
            wait_for_timeout_ms: Extra wait after navigation.
            best_attempt: Continue when waits time out.

        Returns:
            Screenshot result with binary content.

        Raises:
            CommunityToolError: When Browserless fails or returns no image.
        """
        payload: dict[str, Any] = {
            "url": url,
            "options": {
                "fullPage": full_page,
                "type": output_format,
            },
        }
        if quality is not None:
            payload["options"]["quality"] = quality
        if viewport:
            payload["viewport"] = viewport
        if wait_for_selector:
            payload["waitForSelector"] = {
                "selector": wait_for_selector,
                "timeout": wait_for_selector_timeout_ms,
            }
        if wait_for_timeout_ms > 0:
            payload["waitForTimeout"] = wait_for_timeout_ms
        if best_attempt:
            payload["bestAttempt"] = True

        params = {"token": self.token} if self.token else None

        logger.debug("Capturing public URL screenshot via Browserless")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(
                    f"{self.base_url}/screenshot",
                    json=payload,
                    params=params,
                    headers={
                        "Content-Type": "application/json",
                        "Cache-Control": "no-cache",
                    },
                )

                code = resp.status_code
                logger.debug(
                    "Browserless screenshot response: code=%s, target_code=%s, target_status=%s",
                    code,
                    resp.headers.get("X-Response-Code", ""),
                    resp.headers.get("X-Response-Status", ""),
                )

                if code != 200:
                    raise _browserless_http_error(code)

                content = resp.content
                if not content:
                    raise CommunityToolError(
                        provider="browserless",
                        code="no_results",
                        message="Browserless returned no screenshot",
                        retryable=False,
                    )

                return BrowserlessScreenshotResult(
                    content=content,
                    content_type=_get_header(resp.headers, "Content-Type"),
                    target_status_code=_get_header(resp.headers, "X-Response-Code"),
                    target_status=_get_header(resp.headers, "X-Response-Status"),
                    final_url=_get_header(resp.headers, "X-Response-URL"),
                )

        except GraphBubbleUp:
            raise
        except CommunityToolError:
            raise
        except httpx.TimeoutException as error:
            logger.error("Browserless screenshot request timed out")
            raise CommunityToolError(
                provider="browserless",
                code="provider_unavailable",
                message="Browserless is temporarily unavailable",
                retryable=True,
            ) from error
        except httpx.RequestError as error:
            logger.error("Browserless screenshot request failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="browserless",
                code="provider_unavailable",
                message="Browserless is temporarily unavailable",
                retryable=True,
            ) from error
        except Exception as error:
            logger.error("Browserless screenshot failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="browserless",
                code="provider_unavailable",
                message="Browserless is temporarily unavailable",
                retryable=True,
            ) from error
