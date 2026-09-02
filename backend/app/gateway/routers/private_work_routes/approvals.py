from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from app.gateway.deps import private_work_context
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.gateway.routers.private_work_routes.contracts import (
    ExecutionApprovalDecisionRequest,
    ExecutionApprovalEnvelopeResponse,
    _execution_approval_response,
)
from app.gateway.routers.private_work_routes.dependencies import (
    _execution_approval_service,
    _raise_http,
)
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkError

router = APIRouter(route_class=PrivateWorkRoute)


@router.get(
    "/threads/{thread_id}/execution-approvals/active",
    response_model=ExecutionApprovalEnvelopeResponse,
)
async def get_active_execution_approval(
    thread_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ExecutionApprovalEnvelopeResponse:
    try:
        projection = await _execution_approval_service(
            request,
            context.request_id,
        ).active(context, str(thread_id))
    except PrivateWorkError as error:
        _raise_http(error)
    return _execution_approval_response(projection)


@router.get(
    "/threads/{thread_id}/execution-approvals/{approval_id}",
    response_model=ExecutionApprovalEnvelopeResponse,
)
async def get_execution_approval(
    thread_id: uuid.UUID,
    approval_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ExecutionApprovalEnvelopeResponse:
    try:
        projection = await _execution_approval_service(
            request,
            context.request_id,
        ).get(context, str(thread_id), approval_id)
    except PrivateWorkError as error:
        _raise_http(error)
    return _execution_approval_response(projection)


@router.post(
    "/threads/{thread_id}/runs/{source_run_id}/execution-approvals/{approval_id}/decision",
    response_model=ExecutionApprovalEnvelopeResponse,
)
async def decide_execution_approval(
    thread_id: uuid.UUID,
    source_run_id: uuid.UUID,
    approval_id: uuid.UUID,
    body: ExecutionApprovalDecisionRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> ExecutionApprovalEnvelopeResponse:
    try:
        projection = await _execution_approval_service(
            request,
            context.request_id,
        ).decide(
            context,
            thread_id=str(thread_id),
            source_run_id=str(source_run_id),
            approval_id=approval_id,
            decision=body.decision,
            expected_version=int(body.expected_version),
            idempotency_key=body.idempotency_key,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _execution_approval_response(projection)
