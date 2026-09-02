from __future__ import annotations

from fastapi import Request

from app.private_work.chat_controls import ProjectChatControlService
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkError, PrivateWorkUnavailable
from app.private_work.execution_approval_service import ExecutionApprovalService
from app.private_work.feedback_service import PrivateFeedbackService
from app.private_work.file_service import PrivateFileService
from app.private_work.file_streaming import PrivateFileStreamer
from app.private_work.run_service import PrivateRunService
from app.private_work.thread_service import PrivateThreadService
from deerflow.runtime.events.store import RunEventStore
from deerflow.runtime.runs.store import RunStore


def _thread_service(request: Request, request_id: str) -> PrivateThreadService:
    service = getattr(request.app.state, "private_thread_service", None)
    if not isinstance(service, PrivateThreadService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _execution_approval_service(request: Request, request_id: str) -> ExecutionApprovalService:
    service = getattr(request.app.state, "execution_approval_service", None)
    if not isinstance(service, ExecutionApprovalService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _chat_control_service(request: Request, request_id: str) -> ProjectChatControlService:
    service = getattr(request.app.state, "project_chat_control_service", None)
    if not isinstance(service, ProjectChatControlService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _file_service(request: Request, request_id: str) -> PrivateFileService:
    service = getattr(request.app.state, "private_file_service", None)
    if not isinstance(service, PrivateFileService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _file_streamer(request: Request, request_id: str) -> PrivateFileStreamer:
    streamer = getattr(request.app.state, "private_file_streamer", None)
    if not isinstance(streamer, PrivateFileStreamer):
        raise PrivateWorkUnavailable(request_id)
    return streamer


def _run_service(request: Request, request_id: str) -> PrivateRunService:
    service = getattr(request.app.state, "private_run_service", None)
    if not isinstance(service, PrivateRunService):
        raise PrivateWorkUnavailable(request_id)
    return service


async def _browser_chat_run_service(request: Request, context: PrivateWorkContext, thread_id: str) -> PrivateRunService:
    """Return the Run service after rejecting hidden Builder threads.

    The generic browser Run API is a projection of visible chat threads only.
    Builder orchestration keeps using the same internal Run substrate through
    its dedicated services, never through these public routes.
    """

    service = _run_service(request, context.request_id)
    await service.require_browser_chat_thread(context, thread_id)
    return service


def _run_event_store(request: Request, request_id: str) -> RunEventStore:
    store = getattr(request.app.state, "private_run_event_store", None)
    if not isinstance(store, RunEventStore):
        raise PrivateWorkUnavailable(request_id)
    return store


def _run_store(request: Request, request_id: str) -> RunStore:
    store = getattr(request.app.state, "run_store", None)
    if not isinstance(store, RunStore):
        raise PrivateWorkUnavailable(request_id)
    return store


def _feedback_service(request: Request, request_id: str) -> PrivateFeedbackService:
    service = getattr(request.app.state, "private_feedback_service", None)
    if not isinstance(service, PrivateFeedbackService):
        raise PrivateWorkUnavailable(request_id)
    return service


def _runtime_dependency(request: Request, request_id: str, name: str) -> object:
    dependency = getattr(request.app.state, name, None)
    if dependency is None:
        raise PrivateWorkUnavailable(request_id)
    return dependency


def _scoped_checkpointer(request: Request, request_id: str) -> ProjectScopedCheckpointer:
    checkpointer = getattr(request.app.state, "project_scoped_checkpointer", None)
    if not isinstance(checkpointer, ProjectScopedCheckpointer):
        raise PrivateWorkUnavailable(request_id)
    return checkpointer


def _raise_http(error: PrivateWorkError) -> None:
    raise private_work_http_exception(error) from None
