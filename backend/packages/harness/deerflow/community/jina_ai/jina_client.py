import logging
import os

import httpx
from langgraph.errors import GraphBubbleUp

from deerflow.community.errors import CommunityToolError

logger = logging.getLogger(__name__)

_api_key_warned = False


def _jina_http_error(status_code: int) -> CommunityToolError:
    if status_code in {401, 403}:
        code, retryable = "provider_authentication_failed", False
    elif status_code == 429:
        code, retryable = "provider_rate_limited", True
    elif status_code >= 500:
        code, retryable = "provider_unavailable", True
    else:
        code, retryable = "provider_request_failed", False
    return CommunityToolError(
        provider="jina",
        code=code,
        message=f"Jina request failed with HTTP {status_code}",
        retryable=retryable,
    )


class JinaClient:
    async def crawl(self, url: str, return_format: str = "html", timeout: int = 10, proxy: str | None = None, trust_env: bool = True) -> str:
        global _api_key_warned
        headers = {
            "Content-Type": "application/json",
            "X-Return-Format": return_format,
            "X-Timeout": str(timeout),
        }
        if os.getenv("JINA_API_KEY"):
            headers["Authorization"] = f"Bearer {os.getenv('JINA_API_KEY')}"
        elif not _api_key_warned:
            _api_key_warned = True
            logger.warning("Jina API key is not set. Provide your own key to access a higher rate limit. See https://jina.ai/reader for more information.")
        data = {"url": url}
        try:
            client_kwargs: dict[str, object] = {"trust_env": trust_env}
            if proxy:
                client_kwargs["proxy"] = proxy
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post("https://r.jina.ai/", headers=headers, json=data, timeout=timeout)

            if response.status_code != 200:
                logger.error("Jina returned HTTP %s", response.status_code)
                raise _jina_http_error(response.status_code)

            if not response.text or not response.text.strip():
                raise CommunityToolError(
                    provider="jina",
                    code="no_results",
                    message="Jina returned no content",
                    retryable=False,
                )

            return response.text
        except GraphBubbleUp:
            raise
        except CommunityToolError:
            raise
        except (TypeError, ValueError) as error:
            logger.warning("Jina client configuration failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="jina",
                code="configuration_error",
                message="Jina is not configured correctly",
                retryable=False,
            ) from error
        except httpx.TimeoutException as error:
            logger.warning("Jina request timed out")
            raise CommunityToolError(
                provider="jina",
                code="provider_unavailable",
                message="Jina is temporarily unavailable",
                retryable=True,
            ) from error
        except httpx.RequestError as error:
            logger.warning("Jina request failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="jina",
                code="provider_unavailable",
                message="Jina is temporarily unavailable",
                retryable=True,
            ) from error
        except Exception as error:
            logger.warning("Jina request failed; provider_error_type=%s", type(error).__name__)
            raise CommunityToolError(
                provider="jina",
                code="provider_unavailable",
                message="Jina is temporarily unavailable",
                retryable=True,
            ) from error
