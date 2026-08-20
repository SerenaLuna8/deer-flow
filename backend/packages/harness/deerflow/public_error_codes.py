"""Runtime-neutral public provider error-code vocabulary."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

_LLM_ERROR_CODE_BY_REASON: Final[Mapping[str, str]] = MappingProxyType(
    {
        "quota": "LLM_QUOTA_EXCEEDED",
        "auth": "LLM_AUTHENTICATION_FAILED",
        "busy": "LLM_PROVIDER_BUSY",
        "transient": "LLM_PROVIDER_UNAVAILABLE",
        "generic": "LLM_REQUEST_FAILED",
        "current_upload": "CURRENT_UPLOAD_UNAVAILABLE",
        "circuit_open": "LLM_CIRCUIT_OPEN",
    }
)
LLM_PUBLIC_ERROR_CODES: Final[frozenset[str]] = frozenset(_LLM_ERROR_CODE_BY_REASON.values())


def normalize_llm_error_reason(reason: object) -> str:
    """Collapse an arbitrary reason to the closed public reason vocabulary."""

    return reason if isinstance(reason, str) and reason in _LLM_ERROR_CODE_BY_REASON else "generic"


def llm_error_code_for_reason(reason: object) -> str:
    """Return the stable public code for one classified provider failure."""

    return _LLM_ERROR_CODE_BY_REASON[normalize_llm_error_reason(reason)]


__all__ = [
    "LLM_PUBLIC_ERROR_CODES",
    "llm_error_code_for_reason",
    "normalize_llm_error_reason",
]
