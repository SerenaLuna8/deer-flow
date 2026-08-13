"""Stable, secret-free error contract shared by community tools."""

from __future__ import annotations

import json


class CommunityToolError(RuntimeError):
    """Typed provider failure that remains distinct from an empty result set."""

    def __init__(
        self,
        *,
        provider: str,
        code: str,
        message: str,
        retryable: bool,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.message = message
        self.retryable = retryable


def community_error_json(
    error: CommunityToolError,
    *,
    query: str,
) -> str:
    """Serialize a provider failure without exposing its underlying exception."""

    return json.dumps(
        {
            "error": error.message,
            "error_code": error.code,
            "provider": error.provider,
            "retryable": error.retryable,
            "query": query,
        },
        ensure_ascii=False,
    )


def no_results_json(
    *,
    provider: str,
    query: str,
    message: str,
    code: str = "no_results",
) -> str:
    """Serialize a successful provider request that produced no usable results."""

    return community_error_json(
        CommunityToolError(
            provider=provider,
            code=code,
            message=message,
            retryable=False,
        ),
        query=query,
    )
