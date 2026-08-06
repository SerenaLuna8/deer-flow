from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Annotated, Literal

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.private_work.context import strip_private_client_fields
from app.private_work.error_mapping import private_work_http_exception
from app.private_work.errors import PrivateWorkInvalid
from deerflow.trace_context import generate_trace_id, get_current_trace_id
from deerflow.utils.messages import ORIGINAL_USER_CONTENT_KEY


class StrictPrivateWorkModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictPrivateWorkRequest(StrictPrivateWorkModel):
    pass


class StrictPrivateWorkResponse(StrictPrivateWorkModel):
    pass


PrivateRunStreamMode = Literal[
    "values",
    "messages",
    "messages-tuple",
    "updates",
    "events",
    "debug",
    "tasks",
    "checkpoints",
    "custom",
]

_PUBLIC_USER_MESSAGE_TYPES = frozenset({"human", "user"})


def _is_public_user_message(message: Mapping[str, object]) -> bool:
    """Accept only an explicitly user/human message at public Run ingress."""

    discriminators = [message.get(key) for key in ("role", "type") if message.get(key) is not None]
    return bool(discriminators) and all(isinstance(value, str) and value.lower() in _PUBLIC_USER_MESSAGE_TYPES for value in discriminators)


def _sanitize_public_user_message(
    value: object,
) -> dict[str, object] | None:
    if not isinstance(value, Mapping) or not _is_public_user_message(value):
        return None

    message = copy.deepcopy(dict(value))
    # Message names control framework classification (notably ``summary``).
    # Public callers never issue a server-classified message.
    message.pop("name", None)

    additional = message.get("additional_kwargs")
    if isinstance(additional, Mapping):
        safe_additional = strip_private_client_fields(additional)
        safe_additional.pop(ORIGINAL_USER_CONTENT_KEY, None)
        # Visibility is server-owned audit provenance. Even a shape-valid
        # human_input_response supplied at this public boundary may not hide
        # itself; trusted internal paths bypass this request model.
        safe_additional.pop("hide_from_ui", None)
        message["additional_kwargs"] = safe_additional
    else:
        message.pop("additional_kwargs", None)
    return message


def _sanitize_public_messages(value: object) -> object:
    if not isinstance(value, list):
        return []
    return [message for raw in value if (message := _sanitize_public_user_message(raw)) is not None]


def _sanitize_public_run_input(value: object) -> object:
    if isinstance(value, Mapping):
        # The public endpoint admits user turns, not arbitrary AgentState
        # channels. Goal, summary, promotion, delegation and runtime state are
        # written only by their dedicated server-owned paths.
        return {"messages": _sanitize_public_messages(value["messages"])} if "messages" in value else {}
    if isinstance(value, list):
        return _sanitize_public_messages(value)
    return {}


def _sanitize_public_run_command(value: object) -> object:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    if "resume" in value and value["resume"] is not None:
        # Resume payloads are opaque interrupt answers, not graph state.
        result["resume"] = copy.deepcopy(value["resume"])
    update = value.get("update")
    if isinstance(update, Mapping) and "messages" in update:
        messages = _sanitize_public_messages(update["messages"])
        if messages:
            result["update"] = {"messages": messages}
    return result


class PrivateRunCheckpoint(StrictPrivateWorkRequest):
    checkpoint_ns: Literal[""] = ""
    checkpoint_id: str | None = Field(default=None, min_length=1, max_length=128)
    checkpoint_map: None = None


class PrivateRunCreateRequest(StrictPrivateWorkRequest):
    assistant_id: str | None = None
    input: dict[str, object] | list[object] | str | None = None
    command: dict[str, object] | None = None
    config: dict[str, object] = Field(default_factory=dict)
    context: dict[str, object] = Field(default_factory=dict)
    metadata: dict[str, object] = Field(default_factory=dict)
    multitask_strategy: Literal["reject", "interrupt", "rollback"] = "reject"
    checkpoint: PrivateRunCheckpoint | None = None
    on_disconnect: Literal["cancel", "continue"] = "cancel"
    stream_mode: list[PrivateRunStreamMode] = Field(
        default_factory=lambda: ["values"],
        min_length=1,
        max_length=9,
    )
    stream_resumable: bool = False
    stream_subgraphs: bool = False

    @field_validator("stream_mode", mode="before")
    @classmethod
    def normalize_stream_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value

    @field_validator("stream_mode")
    @classmethod
    def reject_duplicate_stream_modes(
        cls,
        value: list[PrivateRunStreamMode],
    ) -> list[PrivateRunStreamMode]:
        if len(value) != len(set(value)):
            raise ValueError("stream modes must be unique")
        return value

    @model_validator(mode="after")
    def strip_nested_client_authority(self) -> PrivateRunCreateRequest:
        for field_name in ("metadata", "config", "context"):
            setattr(
                self,
                field_name,
                strip_client_authority_fields(getattr(self, field_name)),
            )
        self.input = _sanitize_public_run_input(self.input)
        command = _sanitize_public_run_command(self.command)
        self.command = command or None
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


class PrivateThreadContextTokenTriggerResponse(StrictPrivateWorkResponse):
    type: Literal["tokens"]
    configured_value: int = Field(ge=1)
    current_value: int = Field(ge=0)
    threshold_value: int = Field(ge=1)
    remaining_value: int = Field(ge=0)
    progress_percent: float = Field(ge=0, le=100)
    reached: bool
    threshold_tokens: int = Field(ge=1)


class PrivateThreadContextFractionTriggerResponse(StrictPrivateWorkResponse):
    type: Literal["fraction"]
    configured_value: float = Field(gt=0, le=1)
    current_value: float = Field(ge=0)
    threshold_value: float = Field(gt=0, le=1)
    remaining_value: float = Field(ge=0)
    progress_percent: float = Field(ge=0, le=100)
    reached: bool
    context_window_tokens: int = Field(ge=1)
    threshold_tokens: int = Field(ge=1)


class PrivateThreadContextMessageTriggerResponse(StrictPrivateWorkResponse):
    type: Literal["messages"]
    configured_value: int = Field(ge=1)
    current_value: int = Field(ge=0)
    threshold_value: int = Field(ge=1)
    remaining_value: int = Field(ge=0)
    progress_percent: float = Field(ge=0, le=100)
    reached: bool


PrivateThreadContextTriggerResponse = Annotated[
    PrivateThreadContextTokenTriggerResponse | PrivateThreadContextFractionTriggerResponse | PrivateThreadContextMessageTriggerResponse,
    Field(discriminator="type"),
]


class PrivateThreadContextUsageResponse(StrictPrivateWorkResponse):
    thread_id: str
    enabled: bool
    estimated_tokens: int = Field(ge=0)
    message_count: int = Field(ge=0)
    summary_present: bool
    context_window_tokens: int | None = Field(default=None, ge=1)
    triggers: list[PrivateThreadContextTriggerResponse] = Field(
        default_factory=list,
        max_length=8,
    )
    primary_trigger: PrivateThreadContextTriggerResponse | None = None


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
    "PrivateRunCheckpoint",
    "PrivateRunCreateRequest",
    "PrivateThreadContextUsageResponse",
    "PrivateThreadTokenUsageResponse",
    "PrivateWorkRoute",
    "StrictPrivateWorkModel",
    "StrictPrivateWorkRequest",
    "StrictPrivateWorkResponse",
    "strip_client_authority_fields",
]
