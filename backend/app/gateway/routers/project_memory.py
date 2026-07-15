from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from app.gateway.deps import private_work_context, require_project_private_open
from app.gateway.private_work_schemas import (
    PrivateWorkRoute,
    StrictPrivateWorkRequest,
    StrictPrivateWorkResponse,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkError
from app.private_work.memory_service import PrivateMemoryService, PrivateMemoryStatus
from deerflow.agents.memory.storage import ProjectMemorySnapshot
from deerflow.persistence.engine import get_session_factory

router = APIRouter(
    prefix="/api/projects/{project_id}/memory",
    tags=["project-memory"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)

Namespace = Annotated[str, Query(min_length=1, max_length=128)]


class ProjectMemoryResponse(StrictPrivateWorkResponse):
    namespace: str
    version: int
    memory: dict[str, Any]


class ProjectMemoryStatusResponse(StrictPrivateWorkResponse):
    namespace: str
    version: int
    fact_count: int
    last_updated: str


class ProjectMemoryImportRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=0)
    memory: dict[str, Any]


class ProjectMemoryUpdateRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=0)
    content: str | None = None
    category: str | None = None
    confidence: float | None = None


class ProjectMemoryDeleteRequest(StrictPrivateWorkRequest):
    expected_version: int = Field(ge=0)


def _service(request: Request) -> PrivateMemoryService:
    service = getattr(request.app.state, "project_memory_service", None)
    if isinstance(service, PrivateMemoryService):
        return service
    service = PrivateMemoryService(get_session_factory())
    request.app.state.project_memory_service = service
    return service


def _snapshot_response(
    snapshot: ProjectMemorySnapshot,
    namespace: str,
) -> ProjectMemoryResponse:
    return ProjectMemoryResponse(
        namespace=namespace,
        version=snapshot.version,
        memory=snapshot.memory,
    )


async def _call(operation):
    try:
        return await operation
    except PrivateWorkError as exc:
        raise private_work_http_exception(exc) from None


@router.get("", response_model=ProjectMemoryResponse)
async def list_project_memory(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(_service(request).list(context, namespace=namespace))
    return _snapshot_response(snapshot, namespace)


@router.get("/status", response_model=ProjectMemoryStatusResponse)
async def get_project_memory_status(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryStatusResponse:
    status: PrivateMemoryStatus = await _call(_service(request).status(context, namespace=namespace))
    return ProjectMemoryStatusResponse(
        namespace=status.namespace,
        version=status.version,
        fact_count=status.fact_count,
        last_updated=status.last_updated,
    )


@router.post("/reload", response_model=ProjectMemoryResponse)
async def reload_project_memory(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(_service(request).reload(context, namespace=namespace))
    return _snapshot_response(snapshot, namespace)


@router.get("/export")
async def export_project_memory(
    request: Request,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> dict[str, Any]:
    return await _call(_service(request).export(context, namespace=namespace))


@router.post("/import", response_model=ProjectMemoryResponse)
async def import_project_memory(
    request: Request,
    body: ProjectMemoryImportRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).import_memory(
            context,
            body.memory,
            namespace=namespace,
            expected_version=body.expected_version,
        )
    )
    return _snapshot_response(snapshot, namespace)


@router.patch("/facts/{fact_id}", response_model=ProjectMemoryResponse)
async def update_project_memory_fact(
    request: Request,
    fact_id: str,
    body: ProjectMemoryUpdateRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).update(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
            content=body.content,
            category=body.category,
            confidence=body.confidence,
        )
    )
    return _snapshot_response(snapshot, namespace)


@router.delete("/facts/{fact_id}", response_model=ProjectMemoryResponse)
async def delete_project_memory_fact(
    request: Request,
    fact_id: str,
    body: ProjectMemoryDeleteRequest,
    namespace: Namespace = "default",
    context: PrivateWorkContext = Depends(private_work_context),
) -> ProjectMemoryResponse:
    snapshot = await _call(
        _service(request).delete(
            context,
            fact_id,
            namespace=namespace,
            expected_version=body.expected_version,
        )
    )
    return _snapshot_response(snapshot, namespace)
