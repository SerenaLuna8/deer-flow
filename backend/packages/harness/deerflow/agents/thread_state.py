import copy
import uuid
from collections.abc import Mapping, Sequence
from functools import cache
from typing import Annotated, Any, NotRequired, TypedDict, cast, get_type_hints

from langchain.agents import AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import (
    AnyMessage,
    BaseMessageChunk,
    RemoveMessage,
    convert_to_messages,
    message_chunk_to_message,
)
from langgraph.channels import DeltaChannel
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import deerflow.checkpoint_patches as _checkpoint_patches  # noqa: F401
from deerflow.agents.goal_state import GoalState
from deerflow.agents.memory.snip import MemoryArchiveReceipt
from deerflow.config.database_config import (
    DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
    CheckpointChannelMode,
)
from deerflow.config.paths import VIRTUAL_PATH_PREFIX
from deerflow.subagents.status_contract import SUBAGENT_STATUS_VALUES


def _resolve_snapshot_frequency(snapshot_frequency: int | None) -> int:
    """Resolve the explicit, process-frozen, or default delta cadence."""
    if snapshot_frequency is not None:
        return snapshot_frequency
    from deerflow.runtime.checkpoint_mode import (
        resolve_checkpoint_snapshot_frequency,
    )

    return resolve_checkpoint_snapshot_frequency()


class SandboxState(TypedDict):
    sandbox_id: NotRequired[str | None]
    run_id: NotRequired[str | None]


class ThreadDataState(TypedDict):
    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class ViewedImageFileRef(TypedDict):
    """Run-bound sandbox coordinates; never contains a host filesystem path."""

    path: str
    sandbox_id: str
    run_id: str
    project_id: NotRequired[str]
    owner_user_id: NotRequired[str]


class ViewedImageData(TypedDict):
    mime_type: str
    size: int
    sha256: str
    file_ref: ViewedImageFileRef


def merge_sandbox(existing: SandboxState | None, new: SandboxState | None) -> SandboxState | None:
    """Reducer for sandbox state with run-scoped private lease rotation.

    Multiple sandbox tools can initialize lazily in the same graph step and
    emit the same sandbox_id via Command(update=...). LangGraph needs an
    explicit reducer for that shared state key. Private Runs intentionally use
    a fresh lease, so a new run_id may replace the checkpointed sandbox. Within
    one Run, different sandbox ids still indicate a lifecycle/isolation bug and
    fail closed.
    """
    if new is None:
        return existing
    if existing is None:
        return new

    existing_id = existing.get("sandbox_id")
    new_id = new.get("sandbox_id")
    if existing_id == new_id:
        return {**existing, **new}

    existing_run_id = existing.get("run_id")
    new_run_id = new.get("run_id")
    if isinstance(new_run_id, str) and new_run_id and new_run_id != existing_run_id:
        return new
    raise ValueError(f"Conflicting sandbox state updates: {existing_id!r} != {new_id!r}")


SandboxStateField = Annotated[NotRequired[SandboxState | None], merge_sandbox]


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    """Reducer for artifacts list - merges and deduplicates artifacts."""
    if existing is None:
        return new or []
    if new is None:
        return existing
    # Use dict.fromkeys to deduplicate while preserving order
    return list(dict.fromkeys(existing + new))


ViewedImageScope = tuple[str, str, str | None, str | None]
_VIEWED_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_VIEWED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
    }
)
_VIEWED_IMAGE_VIRTUAL_ROOTS = (
    f"{VIRTUAL_PATH_PREFIX}/workspace",
    f"{VIRTUAL_PATH_PREFIX}/uploads",
    f"{VIRTUAL_PATH_PREFIX}/outputs",
)


def _is_viewed_image_virtual_path(path: str) -> bool:
    return any(path == root or path.startswith(f"{root}/") for root in _VIEWED_IMAGE_VIRTUAL_ROOTS)


def normalize_viewed_images(
    images: Mapping[str, object] | None,
) -> dict[str, ViewedImageData]:
    """Drop legacy base64 payloads and malformed checkpoint references."""

    normalized: dict[str, ViewedImageData] = {}
    for image_path, raw_image_data in (images or {}).items():
        if not isinstance(image_path, str) or not isinstance(raw_image_data, Mapping):
            continue
        raw_ref = raw_image_data.get("file_ref")
        mime_type = raw_image_data.get("mime_type")
        size = raw_image_data.get("size")
        sha256 = raw_image_data.get("sha256")
        if (
            not _is_viewed_image_virtual_path(image_path)
            or not isinstance(raw_ref, Mapping)
            or raw_ref.get("path") != image_path
            or not isinstance(raw_ref.get("sandbox_id"), str)
            or not raw_ref["sandbox_id"]
            or not isinstance(raw_ref.get("run_id"), str)
            or not raw_ref["run_id"]
            or mime_type not in _VIEWED_IMAGE_MIME_TYPES
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 <= size <= _VIEWED_IMAGE_MAX_BYTES
            or not isinstance(sha256, str)
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            continue

        project_id = raw_ref.get("project_id")
        owner_user_id = raw_ref.get("owner_user_id")
        if (project_id is None) != (owner_user_id is None):
            continue
        if project_id is not None and (not isinstance(project_id, str) or not project_id or not isinstance(owner_user_id, str) or not owner_user_id):
            continue

        file_ref: ViewedImageFileRef = {
            "path": image_path,
            "sandbox_id": raw_ref["sandbox_id"],
            "run_id": raw_ref["run_id"],
        }
        if isinstance(project_id, str) and isinstance(owner_user_id, str):
            file_ref["project_id"] = project_id
            file_ref["owner_user_id"] = owner_user_id
        normalized[image_path] = {
            "mime_type": mime_type,
            "size": size,
            "sha256": sha256,
            "file_ref": file_ref,
        }
    return normalized


def _viewed_image_scope(image_data: ViewedImageData) -> ViewedImageScope:
    file_ref = image_data["file_ref"]
    return (
        file_ref["sandbox_id"],
        file_ref["run_id"],
        file_ref.get("project_id"),
        file_ref.get("owner_user_id"),
    )


def merge_viewed_images(
    existing: dict[str, ViewedImageData] | None,
    new: dict[str, ViewedImageData] | None,
) -> dict[str, ViewedImageData]:
    """Keep only lightweight references from one exact Run/sandbox scope."""

    normalized_existing = normalize_viewed_images(existing)
    if new is None:
        return normalized_existing
    if not new:
        return {}

    normalized_new = normalize_viewed_images(new)
    new_scopes = {_viewed_image_scope(image_data) for image_data in normalized_new.values()}
    if len(new_scopes) != 1:
        return {}
    current_scope = next(iter(new_scopes))
    retained_existing = {path: image_data for path, image_data in normalized_existing.items() if _viewed_image_scope(image_data) == current_scope}
    return {**retained_existing, **normalized_new}


def merge_todos(existing: list | None, new: list | None) -> list | None:
    """Reducer for todos list - keeps the last non-None value.

    Semantics:
    - If `new` is None (node didn't touch todos), preserve `existing`.
    - If `new` is provided (even empty list), it represents an explicit
      update and wins over `existing`.
    """
    if new is None:
        return existing
    return new


def merge_goal(existing: GoalState | None, new: GoalState | None) -> GoalState | None:
    """Reducer for goal state - preserves existing when a node does not touch it."""
    if new is None:
        return existing
    return new


class PromotedTools(TypedDict):
    catalog_hash: str
    names: list[str]


def merge_promoted(existing: PromotedTools | None, new: PromotedTools | None) -> PromotedTools | None:
    """Reducer for deferred-tool promotions, scoped by catalog hash.

    - new None/empty -> preserve existing (node didn't touch promotions).
    - catalog_hash changed -> replace wholesale, dropping stale names (prevents a
      persisted bare name from exposing a different tool after catalog drift).
    - same catalog_hash -> union names, dedupe, preserve order.
    """
    if not new:
        return existing
    if existing is None or existing.get("catalog_hash") != new["catalog_hash"]:
        return {
            "catalog_hash": new["catalog_hash"],
            "names": list(dict.fromkeys(new["names"])),
        }
    return {
        "catalog_hash": existing["catalog_hash"],
        "names": list(dict.fromkeys(existing["names"] + new["names"])),
    }


TERMINAL_STATUSES: frozenset[str] = frozenset(SUBAGENT_STATUS_VALUES)
_DELEGATION_LEDGER_MAX_ENTRIES = 50


class DelegationEntry(TypedDict):
    id: str
    occurrence: NotRequired[int]
    dispatch_ref: NotRequired[str]
    project_id: NotRequired[str]
    owner_user_id: NotRequired[str]
    run_id: NotRequired[str]
    description: str
    subagent_type: str
    status: str
    result_brief: NotRequired[str]
    result_sha256: NotRequired[str]
    result_ref: NotRequired[str]
    # Why a guardrail cap ended the run early (#3875 Phase 2): token_capped /
    # turn_capped / loop_capped. The status stays completed/failed; this field
    # is the additive signal that distinguishes a capped run from a clean one.
    stop_reason: NotRequired[str]
    created_at: str


DelegationIdentity = tuple[str | None, str | None, str | None, str, int]
_DELEGATION_SCOPE_FIELDS = ("project_id", "owner_user_id", "run_id")


def delegation_occurrence(entry: Mapping[str, object]) -> int:
    """Normalize legacy entries without an occurrence to the first call."""
    occurrence = entry.get("occurrence")
    if isinstance(occurrence, int) and not isinstance(occurrence, bool) and occurrence >= 1:
        return occurrence
    return 1


def delegation_identity(entry: Mapping[str, object]) -> DelegationIdentity:
    """Return the private Run- and occurrence-scoped delegation identity."""
    return (
        str(entry["project_id"]) if entry.get("project_id") is not None else None,
        str(entry["owner_user_id"]) if entry.get("owner_user_id") is not None else None,
        str(entry["run_id"]) if entry.get("run_id") is not None else None,
        str(entry["id"]),
        delegation_occurrence(entry),
    )


def _has_delegation_scope(entry: Mapping[str, object]) -> bool:
    return any(entry.get(field) is not None for field in _DELEGATION_SCOPE_FIELDS)


def merge_delegations(existing: list[DelegationEntry] | None, new: list[DelegationEntry] | None) -> list[DelegationEntry]:
    """Reducer for the delegation ledger.

    - new None/empty -> preserve existing.
    - append entries, replacing same id with the latest version while preserving
      first-seen order.
    - terminal status is never overwritten by a non-terminal status.
    """
    if not new:
        return existing or []

    by_identity: dict[DelegationIdentity, DelegationEntry] = {}
    order: list[DelegationIdentity] = []
    for entry in existing or []:
        identity = delegation_identity(entry)
        if identity not in by_identity:
            order.append(identity)
        by_identity[identity] = entry

    for entry in new:
        identity = delegation_identity(entry)
        previous = by_identity.get(identity)

        # Older extraction paths emitted an update without scope fields. If
        # exactly one retained entry has this provider call id, bind the update
        # to that entry and preserve its server-issued private Run scope. If
        # multiple scoped entries reused the same provider id, ambiguity fails
        # closed by retaining a separate unscoped legacy entry.
        if previous is None and not _has_delegation_scope(entry):
            matches = [candidate for candidate in order if candidate[3] == str(entry["id"]) and candidate[4] == delegation_occurrence(entry)]
            if len(matches) == 1:
                identity = matches[0]
                previous = by_identity[identity]
                entry = {
                    **entry,
                    **{field: previous[field] for field in _DELEGATION_SCOPE_FIELDS if previous.get(field) is not None},
                }

        if previous is not None and previous["status"] in TERMINAL_STATUSES and entry["status"] not in TERMINAL_STATUSES:
            continue
        if identity not in by_identity:
            order.append(identity)
        elif previous.get("created_at"):
            entry = {**entry, "created_at": previous["created_at"]}
        by_identity[identity] = entry
    merged = [by_identity[identity] for identity in order]
    if len(merged) > _DELEGATION_LEDGER_MAX_ENTRIES:
        merged = merged[-_DELEGATION_LEDGER_MAX_ENTRIES:]
    return merged


_SKILL_CONTEXT_MAX_ENTRIES = 8
_SKILL_DESCRIPTION_MAX_CHARS = 500


class SkillEntry(TypedDict):
    name: str
    path: str
    description: str
    loaded_at: int


def _normalize_skill_entry(entry: Mapping[str, object]) -> SkillEntry:
    """Drop legacy payload keys before storing skill_context back to state."""
    description = entry.get("description")
    loaded_at = entry.get("loaded_at")
    return {
        "name": str(entry.get("name") or ""),
        "path": str(entry["path"]),
        "description": " ".join(description.split())[:_SKILL_DESCRIPTION_MAX_CHARS] if isinstance(description, str) else "",
        "loaded_at": loaded_at if isinstance(loaded_at, int) else 0,
    }


def merge_skill_context(existing: list[SkillEntry] | None, new: list[SkillEntry] | None) -> list[SkillEntry]:
    """Reducer for the skill-context channel.

    - new None/empty -> preserve existing.
    - legacy entries are normalized to references; verbatim body keys are dropped.
    - dedup by ``path``; later reads refresh recency and replace the reference.
    - cap by keeping the most recently read entries. ``loaded_at`` is
      observational only because message indices reset after compaction.
    """
    normalized_existing = [_normalize_skill_entry(entry) for entry in existing or []]
    if not new:
        return normalized_existing

    by_path: dict[str, SkillEntry] = {}
    order: list[str] = []
    for entry in normalized_existing:
        path = entry["path"]
        if path not in by_path:
            order.append(path)
        by_path[path] = entry

    for entry in (_normalize_skill_entry(entry) for entry in new):
        path = entry["path"]
        if path in by_path:
            order.remove(path)
        order.append(path)
        by_path[path] = entry

    merged = [by_path[path] for path in order]
    if len(merged) > _SKILL_CONTEXT_MAX_ENTRIES:
        merged = merged[-_SKILL_CONTEXT_MAX_ENTRIES:]
    return merged


class ThreadState(AgentState):
    sandbox: SandboxStateField
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: Annotated[list | None, merge_todos]
    goal: Annotated[GoalState | None, merge_goal]
    uploaded_files: NotRequired[list[dict] | None]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    promoted: Annotated[PromotedTools | None, merge_promoted]
    delegations: Annotated[list[DelegationEntry], merge_delegations]
    skill_context: Annotated[list[SkillEntry], merge_skill_context]
    summary_text: NotRequired[str | None]
    memory_archive_receipt: NotRequired[MemoryArchiveReceipt | None]
    context_projection_snapshot: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    context_compaction_receipt: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    provider_request_profile: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]
    provider_request_measurement: NotRequired[Annotated[dict[str, Any] | None, PrivateStateAttr]]


def _normalize_messages(value: Any) -> list[AnyMessage]:
    values = value if isinstance(value, list) else [value]
    messages = [message_chunk_to_message(cast(BaseMessageChunk, message)) for message in convert_to_messages(values)]
    for message in messages:
        if message.id is None:
            message.id = str(uuid.uuid4())
    return messages


def _index_messages(
    messages: list[AnyMessage | None],
) -> tuple[dict[str, int], dict[str, list[int]]]:
    latest_position: dict[str, int] = {}
    positions_by_id: dict[str, list[int]] = {}
    for position, message in enumerate(messages):
        if message is None:
            continue
        message_id = cast(str, message.id)
        latest_position[message_id] = position
        positions_by_id.setdefault(message_id, []).append(position)
    return latest_position, positions_by_id


def _raise_null_write(has_messages: bool) -> None:
    received = "left" if has_messages else "right"
    raise ValueError(f"Must specify non-null arguments for both 'left' and 'right'. Only received: '{received}'.")


def merge_message_writes(
    state: list[AnyMessage],
    writes: Sequence[Any],
) -> list[AnyMessage]:
    """Fold DeltaChannel writes with add_messages-compatible semantics."""
    if not writes:
        return list(state)
    if writes[0] is None:
        _raise_null_write(bool(state))

    messages: list[AnyMessage | None] = _normalize_messages(state)
    latest_position, positions_by_id = _index_messages(messages)

    for write in writes:
        if write is None:
            _raise_null_write(bool(latest_position))
        normalized_write = _normalize_messages(write)
        remove_all_idx = None
        for position, message in enumerate(normalized_write):
            if isinstance(message, RemoveMessage) and message.id == REMOVE_ALL_MESSAGES:
                remove_all_idx = position

        if remove_all_idx is not None:
            messages = list(normalized_write[remove_all_idx + 1 :])
            latest_position, positions_by_id = _index_messages(messages)
            continue

        ids_to_remove: set[str] = set()
        for message in normalized_write:
            message_id = cast(str, message.id)
            existing_position = latest_position.get(message_id)
            if existing_position is not None:
                if isinstance(message, RemoveMessage):
                    ids_to_remove.add(message_id)
                else:
                    ids_to_remove.discard(message_id)
                    messages[existing_position] = message
                continue

            if isinstance(message, RemoveMessage):
                raise ValueError(f"Attempting to delete a message with an ID that doesn't exist ('{message_id}')")

            position = len(messages)
            messages.append(message)
            latest_position[message_id] = position
            positions_by_id[message_id] = [position]

        for message_id in ids_to_remove:
            for position in positions_by_id.pop(message_id):
                messages[position] = None
            del latest_position[message_id]

    return [message for message in messages if message is not None]


def delta_messages_field(
    snapshot_frequency: int = DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY,
) -> Any:
    """Return the messages annotation for a DeltaChannel cadence."""
    return Annotated[
        list[AnyMessage],
        DeltaChannel(
            merge_message_writes,
            snapshot_frequency=snapshot_frequency,
        ),
    ]


DELTA_MESSAGES_FIELD = delta_messages_field()


class DeltaThreadState(ThreadState):
    messages: DELTA_MESSAGES_FIELD


THREAD_STATE_REDUCER_FIELDS = frozenset(
    {
        "messages",
        "sandbox",
        "artifacts",
        "todos",
        "goal",
        "viewed_images",
        "promoted",
        "delegations",
        "skill_context",
    }
)


def get_thread_state_schema(
    mode: CheckpointChannelMode,
    snapshot_frequency: int | None = None,
) -> type:
    if mode != "delta":
        return ThreadState
    return _delta_thread_state_schema(_resolve_snapshot_frequency(snapshot_frequency))


@cache
def _delta_thread_state_schema(snapshot_frequency: int) -> type:
    if snapshot_frequency == DEFAULT_CHECKPOINT_SNAPSHOT_FREQUENCY:
        return DeltaThreadState
    annotations = get_type_hints(ThreadState, include_extras=True)
    annotations["messages"] = delta_messages_field(snapshot_frequency)
    return TypedDict(
        f"DeltaThreadState_f{snapshot_frequency}",
        annotations,
        total=getattr(ThreadState, "__total__", True),
    )


def adapt_state_schema_for_mode(
    schema: type,
    mode: CheckpointChannelMode,
    snapshot_frequency: int | None = None,
) -> type:
    if mode == "full":
        return schema
    return _adapt_state_schema_for_delta(
        schema,
        _resolve_snapshot_frequency(snapshot_frequency),
    )


@cache
def _adapt_state_schema_for_delta(
    schema: type,
    snapshot_frequency: int,
) -> type:
    annotations = get_type_hints(schema, include_extras=True)
    annotations["messages"] = delta_messages_field(snapshot_frequency)
    return TypedDict(
        f"Delta{schema.__module__.replace('.', '_')}_{schema.__name__}_f{snapshot_frequency}",
        annotations,
        total=getattr(schema, "__total__", True),
    )


def normalize_middleware_state_schemas(
    middleware: Sequence[Any],
    mode: CheckpointChannelMode,
    snapshot_frequency: int | None = None,
) -> list[Any]:
    if mode == "full":
        return list(middleware)
    resolved_frequency = _resolve_snapshot_frequency(snapshot_frequency)
    normalized = []
    for item in middleware:
        schema = getattr(item, "state_schema", None)
        if schema is None:
            normalized.append(item)
            continue
        adapted = copy.copy(item)
        adapted.state_schema = adapt_state_schema_for_mode(
            schema,
            mode,
            resolved_frequency,
        )
        normalized.append(adapted)
    return normalized
