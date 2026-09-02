from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from app.gateway.deps import private_work_context
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.gateway.routers.private_work_routes.contracts import (
    PrivateFeedbackCreateRequest,
    PrivateFeedbackResponse,
    _feedback_response,
)
from app.gateway.routers.private_work_routes.dependencies import _feedback_service, _raise_http
from app.private_work.context import PrivateWorkContext
from app.private_work.errors import PrivateWorkError

router = APIRouter(route_class=PrivateWorkRoute)


@router.get(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse | None,
)
async def get_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse | None:
    try:
        record = await _feedback_service(
            request,
            context.request_id,
        ).get(
            context,
            thread_id=str(thread_id),
            run_id=str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return None if record is None else _feedback_response(record)


async def _upsert_private_feedback(
    *,
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext,
) -> PrivateFeedbackResponse:
    try:
        record = await _feedback_service(
            request,
            context.request_id,
        ).upsert(
            context,
            thread_id=str(thread_id),
            run_id=str(run_id),
            rating=body.rating,
            message_id=body.message_id,
            comment=body.comment,
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return _feedback_response(record)


@router.put(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse,
)
async def upsert_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse:
    return await _upsert_private_feedback(
        thread_id=thread_id,
        run_id=run_id,
        body=body,
        request=request,
        context=context,
    )


@router.delete(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> Response:
    try:
        await _feedback_service(
            request,
            context.request_id,
        ).delete(
            context,
            thread_id=str(thread_id),
            run_id=str(run_id),
        )
    except PrivateWorkError as error:
        _raise_http(error)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/threads/{thread_id}/runs/{run_id}/feedback",
    response_model=PrivateFeedbackResponse,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
)
async def create_private_feedback(
    thread_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PrivateFeedbackCreateRequest,
    request: Request,
    context: PrivateWorkContext = Depends(private_work_context),
) -> PrivateFeedbackResponse:
    return await _upsert_private_feedback(
        thread_id=thread_id,
        run_id=run_id,
        body=body,
        request=request,
        context=context,
    )
