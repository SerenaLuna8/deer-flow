"""Closed error codes shared by runtime exception boundaries."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from langgraph.errors import GraphBubbleUp

TOOL_EXECUTION_FAILED_ERROR_CODE: Final = "TOOL_EXECUTION_FAILED"
RUN_EXECUTION_FAILED_ERROR_CODE: Final = "RUN_EXECUTION_FAILED"
ROLLBACK_FAILED_ERROR_CODE: Final = "ROLLBACK_FAILED"
PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE: Final = "PRIVATE_RUN_EXECUTION_FAILED"
MEMORY_AUTHORITY_UNAVAILABLE_MESSAGE: Final = "Memory authority unavailable"
CURRENT_UPLOAD_FAILURE_DETAIL: Final = "Current image upload is unavailable, unauthorized, invalid, changed, or exceeds vision input limits"

SUBAGENT_EXECUTION_FAILED_ERROR_CODE: Final = "SUBAGENT_EXECUTION_FAILED"
SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE_ERROR_CODE: Final = "SUBAGENT_COMMAND_EXECUTION_UNAVAILABLE"
LOOP_FINALIZATION_FAILED_ERROR_CODE: Final = "LOOP_FINALIZATION_FAILED"
TOOL_CALL_CONTROL_STATE_INVALID_ERROR_CODE: Final = "TOOL_CALL_CONTROL_STATE_INVALID"


class MemoryAuthorityUnavailable(GraphBubbleUp):
    """Fatal internal signal when live Memory authority cannot be evaluated.

    ``GraphBubbleUp`` prevents tool middleware from converting a failed live
    authority check into a recoverable ToolMessage.  The runtime preserves the
    signal for its durable executor, which maps it to the existing private-Run
    retry/dead code without expanding the public error protocol.
    """

    public_error_code: Final = PRIVATE_RUN_EXECUTION_FAILED_ERROR_CODE

    def __init__(self) -> None:
        super().__init__(MEMORY_AUTHORITY_UNAVAILABLE_MESSAGE)


class PublicRunErrorCode(StrEnum):
    """Closed reasons whose messages are safe to expose at the Run boundary."""

    PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE = "PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE"
    MODEL_OUTPUT_LIMIT = "MODEL_OUTPUT_LIMIT"
    LOOP_SAFETY_LIMIT = "LOOP_SAFETY_LIMIT"
    LOOP_FINALIZATION_FAILED = LOOP_FINALIZATION_FAILED_ERROR_CODE
    TOOL_CALL_CONTROL_STATE_INVALID = TOOL_CALL_CONTROL_STATE_INVALID_ERROR_CODE
    OUTPUT_DELIVERY_INCOMPLETE = "OUTPUT_DELIVERY_INCOMPLETE"
    CURRENT_UPLOAD_UNAVAILABLE = "CURRENT_UPLOAD_UNAVAILABLE"
    SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED = "SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED"
    LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED = "LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED"
    PROVIDER_REQUEST_USAGE_UNSUPPORTED = "PROVIDER_REQUEST_USAGE_UNSUPPORTED"
    PROVIDER_REQUEST_PROFILE_DRIFT = "PROVIDER_REQUEST_PROFILE_DRIFT"
    PROVIDER_REQUEST_CAPACITY_EXCEEDED = "PROVIDER_REQUEST_CAPACITY_EXCEEDED"


_PUBLIC_RUN_ERROR_MESSAGE_BY_CODE: Final[Mapping[PublicRunErrorCode, str]] = MappingProxyType(
    {
        PublicRunErrorCode.PRIVATE_RUN_MESSAGE_BOUNDARY_UNAVAILABLE: ("Private Run pre-run message boundary is unavailable"),
        PublicRunErrorCode.MODEL_OUTPUT_LIMIT: ("The model reached its output limit before completing the response"),
        PublicRunErrorCode.LOOP_SAFETY_LIMIT: ("The Run stopped after reaching the loop safety limit"),
        PublicRunErrorCode.LOOP_FINALIZATION_FAILED: ("The model did not complete the required tool-free final response"),
        PublicRunErrorCode.TOOL_CALL_CONTROL_STATE_INVALID: ("The Run stopped because its tool-control state could not be validated"),
        PublicRunErrorCode.OUTPUT_DELIVERY_INCOMPLETE: ("The required output file was not presented"),
        PublicRunErrorCode.CURRENT_UPLOAD_UNAVAILABLE: ("The current image attachment could not be read or validated"),
        PublicRunErrorCode.SANDBOX_READ_ONLY_MOUNTS_UNSUPPORTED: ("Configured sandbox provider does not support run-scoped read-only mounts"),
        PublicRunErrorCode.LOCAL_HOST_BASH_READ_ONLY_MOUNTS_UNSUPPORTED: ("Local private runtime cannot enforce read-only mounts when host bash is enabled"),
        PublicRunErrorCode.PROVIDER_REQUEST_USAGE_UNSUPPORTED: ("Provider request usage cannot be measured safely for this request"),
        PublicRunErrorCode.PROVIDER_REQUEST_PROFILE_DRIFT: ("The final provider request no longer matches its frozen usage profile"),
        PublicRunErrorCode.PROVIDER_REQUEST_CAPACITY_EXCEEDED: ("The provider request safety value exceeds the selected model input capacity"),
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
