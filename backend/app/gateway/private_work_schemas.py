from __future__ import annotations

from collections.abc import Mapping

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from app.private_work.context import strip_private_client_fields
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkInvalid
from deerflow.trace_context import generate_trace_id, get_current_trace_id


class StrictPrivateWorkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictPrivateWorkRequest(StrictPrivateWorkModel):
    pass


class StrictPrivateWorkResponse(StrictPrivateWorkModel):
    pass


class PrivateWorkRoute(APIRoute):
    """Give all project-private validation failures one stable error shape."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except RequestValidationError:
                request_id = get_current_trace_id() or generate_trace_id()
                raise private_work_http_exception(PrivateWorkInvalid(request_id)) from None

        return handler


def strip_client_authority_fields(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Remove client-supplied private authority recursively."""

    return strip_private_client_fields(value)


__all__ = [
    "PrivateWorkRoute",
    "StrictPrivateWorkModel",
    "StrictPrivateWorkRequest",
    "StrictPrivateWorkResponse",
    "strip_client_authority_fields",
]
