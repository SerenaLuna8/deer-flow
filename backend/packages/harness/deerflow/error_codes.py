"""Closed error codes shared by runtime exception boundaries."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

TOOL_EXECUTION_FAILED_ERROR_CODE: Final = "TOOL_EXECUTION_FAILED"
RUN_EXECUTION_FAILED_ERROR_CODE: Final = "RUN_EXECUTION_FAILED"

SUBAGENT_EXECUTION_FAILED_ERROR_CODE: Final = "SUBAGENT_EXECUTION_FAILED"
SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE_ERROR_CODE: Final = "SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE"

_LLM_ERROR_CODE_BY_REASON: Final[Mapping[str, str]] = MappingProxyType(
    {
        "quota": "LLM_QUOTA_EXCEEDED",
        "auth": "LLM_AUTHENTICATION_FAILED",
        "busy": "LLM_PROVIDER_BUSY",
        "transient": "LLM_PROVIDER_UNAVAILABLE",
        "generic": "LLM_REQUEST_FAILED",
        "circuit_open": "LLM_CIRCUIT_OPEN",
    }
)
LLM_PUBLIC_ERROR_CODES: Final[frozenset[str]] = frozenset(_LLM_ERROR_CODE_BY_REASON.values())


class PublicRunErrorCode(StrEnum):
    """Closed reasons whose messages are safe to expose at the Run boundary."""

    PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE = "PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE"
    SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED = "SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED"
    LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED = "LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED"


_PUBLIC_RUN_ERROR_MESSAGE_BY_CODE: Final[Mapping[PublicRunErrorCode, str]] = MappingProxyType(
    {
        PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE: ("Private Run pre-run message boundary is unavailable"),
        PublicRunErrorCode.SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED: ("Configured sandbox provider does not support run-scoped read-only mounts"),
        PublicRunErrorCode.LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED: ("Local private runtime cannot enforce read-only mounts when host bash is enabled"),
    }
)


class PublicRunError(RuntimeError):
    """A classified Run failure with a closed, pre-reviewed public message."""

    def __init__(self, code: PublicRunErrorCode) -> None:
        if type(code) is not PublicRunErrorCode:
            raise TypeError("PublicRunError requires a PublicRunErrorCode")
        self.code = code
        self.public_message = _PUBLIC_RUN_ERROR_MESSAGE_BY_CODE[code]
        super().__init__(self.public_message)


def normalize_llm_error_reason(reason: object) -> str:
    """Collapse an arbitrary reason to the closed public reason vocabulary."""

    return reason if isinstance(reason, str) and reason in _LLM_ERROR_CODE_BY_REASON else "generic"


def llm_error_code_for_reason(reason: object) -> str:
    """Return the stable public code for one classified provider failure."""

    return _LLM_ERROR_CODE_BY_REASON[normalize_llm_error_reason(reason)]
