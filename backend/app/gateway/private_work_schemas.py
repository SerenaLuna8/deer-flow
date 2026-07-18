from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, model_validator

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


class PrivateRunCreateRequest(StrictPrivateWorkRequest):
    assistant_id: str | None = None
    input: dict[str, object] | list[object] | str | None = None
    command: dict[str, object] | None = None
    config: dict[str, object] = Field(default_factory=dict)
    context: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"

    @model_validator(mode="after")
    def strip_nested_client_authority(self) -> PrivateRunCreateRequest:
        for field_name in ("metadata", "config", "context"):
            setattr(
                self,
                field_name,
                strip_client_authority_fields(getattr(self, field_name)),
            )
        return self


class PrivateThreadTokenUsageModelBreakdown(StrictPrivateWorkResponse):
    tokens: int = 0
    runs: int = 0


class PrivateThreadTokenUsageCallerBreakdown(StrictPrivateWorkResponse):
    lead_agent: int = 0
    subagent: int = 0
    middleware: int = 0


class PrivateThreadTokenUsageResponse(StrictPrivateWorkResponse):
    thread_id: str
    total_tokens: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_runs: int = 0
    by_model: dict[str, PrivateThreadTokenUsageModelBreakdown] = Field(
        default_factory=dict,
    )
    by_caller: PrivateThreadTokenUsageCallerBreakdown = Field(
        default_factory=PrivateThreadTokenUsageCallerBreakdown,
    )


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
    "PrivateRunCreateRequest",
    "PrivateThreadTokenUsageResponse",
    "PrivateWorkRoute",
    "StrictPrivateWorkModel",
    "StrictPrivateWorkRequest",
    "StrictPrivateWorkResponse",
    "strip_client_authority_fields",
]
