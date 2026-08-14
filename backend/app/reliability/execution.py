"""Compatibility facade for private Run execution."""

from app.reliability.run_execution.boundary import PrivateRunExecutionBoundary
from app.reliability.run_execution.contracts import (
    AgentExecutionResult,
    PrivateRunExecution,
)
from app.reliability.run_execution.contracts import (
    PrivateRunUsageSnapshot as PrivateRunUsageSnapshot,
)
from app.reliability.run_execution.errors import (
    AmbiguousExternalSideEffect,
    PermanentExecutionError,
    TransientExecutionError,
)
from app.reliability.run_execution.executor import RunAgentPrivateExecutor
from app.reliability.run_execution.handler import PrivateRunJobHandler
from app.reliability.run_execution.ports import PrivateRunExecutor
from app.reliability.run_execution.settlement import PrivateRunJobTerminalPort
from app.reliability.run_execution.stream_authority import (
    LeaseAuthorizedRunEventStore,
    LeaseAuthorizedStreamBridge,
)

__all__ = [
    "AgentExecutionResult",
    "AmbiguousExternalSideEffect",
    "LeaseAuthorizedRunEventStore",
    "LeaseAuthorizedStreamBridge",
    "PrivateRunExecution",
    "PrivateRunExecutionBoundary",
    "PrivateRunExecutor",
    "PrivateRunJobHandler",
    "PrivateRunJobTerminalPort",
    "PermanentExecutionError",
    "RunAgentPrivateExecutor",
    "TransientExecutionError",
]
