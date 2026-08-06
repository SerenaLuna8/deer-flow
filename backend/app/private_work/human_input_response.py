from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.private_work.checkpoint_state import (
    bind_transaction_checkpoint_state,
    checkpoint_config,
)
from app.private_work.checkpointer import ProjectScopedCheckpointer
from app.private_work.context import PrivateWorkContext
from deerflow.agents.human_input import (
    HumanInputResponse,
    read_human_input_response,
)
from deerflow.config.app_config import AppConfig

_ASK_CLARIFICATION = "ask_clarification"
_HUMAN_MESSAGE_TYPES = frozenset({"human", "user"})
_CANONICAL_RESPONSE_PREFIX = 'For your clarification "'


@dataclass(frozen=True, slots=True)
class _HumanInputOption:
    option_id: str
    value: str


@dataclass(frozen=True, slots=True)
class _HumanInputRequest:
    request_id: str
    tool_call_id: str
    question: str
    input_mode: str
    options: tuple[_HumanInputOption, ...]
    source_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class HumanInputResponsePromotion:
    """Server-authorized reply plus its exact logical continuation source."""

    kwargs: dict[str, object]
    continuation_run_id: str | None = None
    continuation_verified: bool = False


def _message_value(message: object, key: str) -> object:
    if isinstance(message, Mapping):
        return message.get(key)
    return getattr(message, key, None)


def _non_empty_string(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _message_type(message: object) -> str | None:
    value = _message_value(message, "type") or _message_value(message, "role")
    return value.lower() if isinstance(value, str) else None


def _additional_kwargs(message: object) -> Mapping[str, object]:
    value = _message_value(message, "additional_kwargs")
    return value if isinstance(value, Mapping) else {}


def _read_request(message: object) -> _HumanInputRequest | None:
    if _message_type(message) != "tool" or _message_value(message, "name") != _ASK_CLARIFICATION:
        return None
    message_id = _non_empty_string(_message_value(message, "id"))
    tool_call_id = _non_empty_string(_message_value(message, "tool_call_id"))
    artifact = _message_value(message, "artifact")
    if message_id is None or tool_call_id is None or not isinstance(artifact, Mapping):
        return None
    payload = artifact.get("human_input")
    if not isinstance(payload, Mapping):
        return None
    version = payload.get("version")
    if version not in {1, 2} or payload.get("kind") != "human_input_request" or payload.get("source") != _ASK_CLARIFICATION:
        return None
    request_id = _non_empty_string(payload.get("request_id"))
    payload_tool_call_id = _non_empty_string(payload.get("tool_call_id"))
    question = _non_empty_string(payload.get("question"))
    input_mode = _non_empty_string(payload.get("input_mode"))
    if request_id != message_id or payload_tool_call_id != tool_call_id or question is None or input_mode not in {"free_text", "single_choice", "choice_with_other", "form"}:
        return None

    options: list[_HumanInputOption] = []
    seen_option_ids: set[str] = set()
    raw_options = payload.get("options", [])
    if not isinstance(raw_options, list):
        return None
    for raw_option in raw_options:
        if not isinstance(raw_option, Mapping):
            return None
        option_id = _non_empty_string(raw_option.get("id"))
        option_value = _non_empty_string(raw_option.get("value"))
        if option_id is None or option_value is None or option_id in seen_option_ids:
            return None
        seen_option_ids.add(option_id)
        options.append(
            _HumanInputOption(
                option_id=option_id,
                value=option_value,
            )
        )
    choice_modes = {"single_choice", "choice_with_other"}
    if input_mode in choice_modes and not options:
        return None
    if input_mode not in choice_modes and options:
        return None
    raw_fields = payload.get("fields")
    if input_mode == "form":
        if version != 2 or not isinstance(raw_fields, list) or not raw_fields:
            return None
    elif version != 1 or "fields" in payload:
        return None
    source_run_id = None
    if "source_run_id" in payload:
        source_run_id = _non_empty_string(payload.get("source_run_id"))
        if source_run_id is None or len(source_run_id) > 64:
            return None
    return _HumanInputRequest(
        request_id=request_id,
        tool_call_id=tool_call_id,
        question=question,
        input_mode=input_mode,
        options=tuple(options),
        source_run_id=source_run_id,
    )


def _read_response(message: object) -> HumanInputResponse | None:
    if _message_type(message) not in _HUMAN_MESSAGE_TYPES:
        return None
    return read_human_input_response(_additional_kwargs(message))


def _is_visible_plain_human(message: object) -> bool:
    if _message_type(message) not in _HUMAN_MESSAGE_TYPES:
        return False
    if _message_value(message, "name") == "summary":
        return False
    return _additional_kwargs(message).get("hide_from_ui") is not True


def _latest_open_request(
    checkpoint_messages: Sequence[object],
) -> _HumanInputRequest | None:
    requests: dict[str, _HumanInputRequest] = {}
    request_order: list[str] = []
    ambiguous_request_ids: set[str] = set()
    answered_request_ids: set[str] = set()
    current_run_id: str | None = None

    for message in checkpoint_messages:
        if _message_type(message) in _HUMAN_MESSAGE_TYPES and _message_value(message, "name") != "summary":
            # Private Worker execution overwrites this field on every admitted
            # HumanMessage.  It is therefore a server-owned checkpoint
            # coordinate, not a continuation target supplied by the reply.
            stamped_run_id = _non_empty_string(
                _additional_kwargs(message).get("run_id"),
            )
            if stamped_run_id is not None:
                current_run_id = stamped_run_id
        request = _read_request(message)
        if request is not None:
            if request.source_run_id is None:
                request = replace(request, source_run_id=current_run_id)
            existing = requests.get(request.request_id)
            if existing is not None and existing != request:
                ambiguous_request_ids.add(request.request_id)
                continue
            if existing is None:
                requests[request.request_id] = request
                request_order.append(request.request_id)
            continue

        response = _read_response(message)
        if response is not None and response["request_id"] in requests:
            answered_request_ids.add(response["request_id"])
            continue

        if _is_visible_plain_human(message):
            latest_unanswered = next(
                (request_id for request_id in reversed(request_order) if request_id not in answered_request_ids),
                None,
            )
            if latest_unanswered is not None:
                answered_request_ids.add(latest_unanswered)

    latest_open_id = next(
        (request_id for request_id in reversed(request_order) if request_id not in answered_request_ids and request_id not in ambiguous_request_ids),
        None,
    )
    return requests.get(latest_open_id) if latest_open_id is not None else None


def _candidate_message(kwargs: Mapping[str, object]) -> Mapping[str, object] | None:
    graph_input = kwargs.get("input")
    if not isinstance(graph_input, Mapping):
        return None
    messages = graph_input.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        return None
    message = messages[0]
    if not isinstance(message, Mapping) or _message_type(message) not in _HUMAN_MESSAGE_TYPES or _message_value(message, "name") is not None:
        return None
    additional_kwargs = message.get("additional_kwargs")
    if not isinstance(additional_kwargs, Mapping) or set(additional_kwargs) != {"human_input_response"}:
        return None
    return message


def has_human_input_response_candidate(kwargs: Mapping[str, object]) -> bool:
    message = _candidate_message(kwargs)
    return message is not None and read_human_input_response(_additional_kwargs(message)) is not None


def _response_matches_request(
    response: HumanInputResponse,
    request: _HumanInputRequest,
) -> bool:
    if response["source"] != _ASK_CLARIFICATION or response["request_id"] != request.request_id:
        return False
    if response["response_kind"] == "option":
        if request.input_mode not in {"single_choice", "choice_with_other"}:
            return False
        return any(option.option_id == response["option_id"] and option.value == response["value"] for option in request.options)
    return response["response_kind"] == "text" and request.input_mode in {
        "free_text",
        "choice_with_other",
        "form",
    }


def _canonical_response_text(
    request: _HumanInputRequest,
    response: HumanInputResponse,
) -> str:
    return f'{_CANONICAL_RESPONSE_PREFIX}{request.question}", my answer is: {response["value"]}'


def _has_canonical_content(message: Mapping[str, object], expected: str) -> bool:
    content = message.get("content")
    if isinstance(content, str):
        return content == expected
    if not isinstance(content, list) or len(content) != 1:
        return False
    block = content[0]
    return isinstance(block, Mapping) and set(block) == {"type", "text"} and block.get("type") == "text" and block.get("text") == expected


def authorize_matching_human_input_response(
    kwargs: Mapping[str, object],
    *,
    checkpoint_messages: Sequence[object],
) -> HumanInputResponsePromotion:
    """Authorize one exact reply and bind it to the open request's source Run."""

    result = copy.deepcopy(dict(kwargs))
    candidate = _candidate_message(kwargs)
    request = _latest_open_request(checkpoint_messages)
    if candidate is None or request is None:
        return HumanInputResponsePromotion(result)
    response = read_human_input_response(_additional_kwargs(candidate))
    if response is None or not _response_matches_request(response, request):
        return HumanInputResponsePromotion(result)
    candidate_id = _non_empty_string(candidate.get("id"))
    if candidate_id is not None and any(_non_empty_string(_message_value(message, "id")) == candidate_id for message in checkpoint_messages):
        return HumanInputResponsePromotion(result)
    expected_content = _canonical_response_text(request, response)
    if not _has_canonical_content(candidate, expected_content):
        return HumanInputResponsePromotion(result)

    graph_input = result.get("input")
    if not isinstance(graph_input, dict):
        return HumanInputResponsePromotion(result)
    messages = graph_input.get("messages")
    if not isinstance(messages, list) or len(messages) != 1:
        return HumanInputResponsePromotion(result)
    promoted_message: dict[str, object] = {
        "type": "human",
        "content": [{"type": "text", "text": expected_content}],
        "additional_kwargs": {
            "hide_from_ui": True,
            "human_input_response": copy.deepcopy(response),
        },
    }
    if candidate_id is not None:
        promoted_message["id"] = candidate_id
    graph_input["messages"] = [promoted_message]
    return HumanInputResponsePromotion(
        result,
        continuation_run_id=request.source_run_id,
        continuation_verified=True,
    )


def promote_matching_human_input_response(
    kwargs: Mapping[str, object],
    *,
    checkpoint_messages: Sequence[object],
) -> dict[str, object]:
    """Add server-owned visibility metadata to one exact open clarification reply."""

    return authorize_matching_human_input_response(
        kwargs,
        checkpoint_messages=checkpoint_messages,
    ).kwargs


class CheckpointHumanInputResponsePromoter:
    """Promote replies only after reading the locked Thread's materialized state."""

    def __init__(
        self,
        project_checkpointer: ProjectScopedCheckpointer,
        app_config: AppConfig,
    ) -> None:
        self._project_checkpointer = project_checkpointer
        self._app_config = app_config

    async def promote(
        self,
        session: AsyncSession,
        context: PrivateWorkContext,
        thread_id: str,
        kwargs: Mapping[str, object],
    ) -> HumanInputResponsePromotion:
        if not has_human_input_response_candidate(kwargs):
            return HumanInputResponsePromotion(copy.deepcopy(dict(kwargs)))
        accessor = bind_transaction_checkpoint_state(
            self._project_checkpointer.for_context(context),
            session,
            self._app_config,
            as_node="human_input_response_admission",
        )
        snapshot = await accessor.aget(checkpoint_config(thread_id))
        values = getattr(snapshot, "values", None)
        messages = values.get("messages", []) if isinstance(values, Mapping) else []
        return authorize_matching_human_input_response(
            kwargs,
            checkpoint_messages=(messages if isinstance(messages, list) else []),
        )


__all__ = [
    "CheckpointHumanInputResponsePromoter",
    "HumanInputResponsePromotion",
    "authorize_matching_human_input_response",
    "has_human_input_response_candidate",
    "promote_matching_human_input_response",
]
