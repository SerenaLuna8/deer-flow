"""Observable middleware-chain semantics.

This module does not build or reorder middleware.  It projects an already
assembled chain into the hook dispatch orders that LangChain will exercise so
order-sensitive behavior can be reviewed and pinned independently from the
builder implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from itertools import pairwise

from langchain.agents.middleware import AgentMiddleware


class MiddlewareHook(StrEnum):
    BEFORE_AGENT = "before_agent"
    BEFORE_MODEL = "before_model"
    WRAP_MODEL_CALL = "wrap_model_call"
    WRAP_TOOL_CALL = "wrap_tool_call"
    AFTER_MODEL = "after_model"
    AFTER_AGENT = "after_agent"


class MiddlewareDispatchDirection(StrEnum):
    FORWARD = "forward"
    OUTER_FIRST = "outer_first"
    REVERSE = "reverse"


class MiddlewarePhase(IntEnum):
    UNTRUSTED_CONTENT = 100
    THREAD_INFRA = 200
    TRANSCRIPT_REPAIR = 300
    TOOL_CALL_BOUNDARY = 400
    PRIVATE_CONTEXT = 500
    COMPACTION = 600
    PLANNING = 700
    RESPONSE_RECOVERY = 800
    ACCOUNTING = 900
    REQUEST_SHAPING = 1000
    TOOL_CALL_ARBITRATION = 1100
    CUSTOM = 1150
    RESPONSE_GATE = 1200
    INTERRUPT_TAIL = 1300
    FINAL_PROVIDER_REQUEST = 1400


@dataclass(frozen=True, slots=True)
class MiddlewareLayerMetadata:
    layer_id: str
    phase: MiddlewarePhase
    slot: int
    why: str


@dataclass(frozen=True, slots=True)
class MiddlewareDispatchConstraint:
    name: str
    hook: MiddlewareHook
    first: str
    then: str
    why: str


_HOOK_METHODS: dict[MiddlewareHook, tuple[str, str]] = {
    MiddlewareHook.BEFORE_AGENT: ("before_agent", "abefore_agent"),
    MiddlewareHook.BEFORE_MODEL: ("before_model", "abefore_model"),
    MiddlewareHook.WRAP_MODEL_CALL: ("wrap_model_call", "awrap_model_call"),
    MiddlewareHook.WRAP_TOOL_CALL: ("wrap_tool_call", "awrap_tool_call"),
    MiddlewareHook.AFTER_MODEL: ("after_model", "aafter_model"),
    MiddlewareHook.AFTER_AGENT: ("after_agent", "aafter_agent"),
}

_HOOK_DIRECTIONS: dict[MiddlewareHook, MiddlewareDispatchDirection] = {
    MiddlewareHook.BEFORE_AGENT: MiddlewareDispatchDirection.FORWARD,
    MiddlewareHook.BEFORE_MODEL: MiddlewareDispatchDirection.FORWARD,
    MiddlewareHook.WRAP_MODEL_CALL: MiddlewareDispatchDirection.OUTER_FIRST,
    MiddlewareHook.WRAP_TOOL_CALL: MiddlewareDispatchDirection.OUTER_FIRST,
    MiddlewareHook.AFTER_MODEL: MiddlewareDispatchDirection.REVERSE,
    MiddlewareHook.AFTER_AGENT: MiddlewareDispatchDirection.REVERSE,
}

_LAYER_METADATA_ATTRIBUTE = "_deerflow_middleware_layer"


@dataclass(frozen=True, slots=True)
class MiddlewareLayerDescription:
    registration_index: int
    middleware_name: str
    hooks: tuple[MiddlewareHook, ...]
    metadata: MiddlewareLayerMetadata | None = None


@dataclass(frozen=True, slots=True)
class MiddlewareChainDescription:
    layers: tuple[MiddlewareLayerDescription, ...]

    def dispatch_order(
        self,
        hook: MiddlewareHook,
    ) -> tuple[str, ...]:
        names = tuple(layer.middleware_name for layer in self.layers if hook in layer.hooks)
        if _HOOK_DIRECTIONS[hook] is MiddlewareDispatchDirection.REVERSE:
            return tuple(reversed(names))
        return names

    def render(self) -> str:
        return "\n".join(
            f"{layer.registration_index:02d} "
            f"{layer.middleware_name} "
            f"[{', '.join(hook.value for hook in layer.hooks) or '-'}]" + ("" if layer.metadata is None else (f" {layer.metadata.phase.name}:{layer.metadata.slot} {layer.metadata.layer_id}"))
            for layer in self.layers
        )


def middleware_dispatch_direction(
    hook: MiddlewareHook,
) -> MiddlewareDispatchDirection:
    return _HOOK_DIRECTIONS[hook]


def _overrides_hook_method(
    middleware: AgentMiddleware,
    method_name: str,
) -> bool:
    implementation = getattr(type(middleware), method_name, None)
    base_implementation = getattr(AgentMiddleware, method_name, None)
    return implementation is not None and implementation is not base_implementation


def middleware_hooks(
    middleware: AgentMiddleware,
) -> tuple[MiddlewareHook, ...]:
    return tuple(hook for hook, method_names in _HOOK_METHODS.items() if any(_overrides_hook_method(middleware, method_name) for method_name in method_names))


def assign_middleware_layer[MiddlewareT: AgentMiddleware](
    middleware: MiddlewareT,
    *,
    layer_id: str,
    phase: MiddlewarePhase,
    slot: int,
    why: str,
) -> MiddlewareT:
    """Attach stable assembly metadata without wrapping the middleware."""

    if not layer_id or slot < 0 or not why:
        raise ValueError("middleware layer metadata must be complete")
    setattr(
        middleware,
        _LAYER_METADATA_ATTRIBUTE,
        MiddlewareLayerMetadata(
            layer_id=layer_id,
            phase=phase,
            slot=slot,
            why=why,
        ),
    )
    return middleware


def middleware_layer_metadata(
    middleware: AgentMiddleware,
) -> MiddlewareLayerMetadata | None:
    metadata = getattr(middleware, _LAYER_METADATA_ATTRIBUTE, None)
    return metadata if isinstance(metadata, MiddlewareLayerMetadata) else None


def describe_middleware_chain(
    middlewares: Sequence[AgentMiddleware],
) -> MiddlewareChainDescription:
    return MiddlewareChainDescription(
        layers=tuple(
            MiddlewareLayerDescription(
                registration_index=index,
                middleware_name=type(middleware).__name__,
                hooks=middleware_hooks(middleware),
                metadata=middleware_layer_metadata(middleware),
            )
            for index, middleware in enumerate(middlewares)
        )
    )


def middleware_dispatch_order(
    middlewares: Sequence[AgentMiddleware],
    hook: MiddlewareHook,
) -> tuple[str, ...]:
    return describe_middleware_chain(middlewares).dispatch_order(hook)


def validate_middleware_phase_ladder(
    middlewares: Sequence[AgentMiddleware],
    *,
    require_tagged: bool = True,
) -> None:
    """Validate the literal registration order; never sort or repair it."""

    tagged: list[tuple[int, MiddlewareLayerMetadata]] = []
    for index, middleware in enumerate(middlewares):
        metadata = middleware_layer_metadata(middleware)
        if metadata is None:
            if require_tagged:
                raise RuntimeError(f"Middleware phase ladder contains an untagged layer: {type(middleware).__name__} at registration index {index}")
            continue
        tagged.append((index, metadata))

    layer_ids = [metadata.layer_id for _, metadata in tagged]
    if len(layer_ids) != len(set(layer_ids)):
        raise RuntimeError("Middleware phase ladder contains duplicate layer ids")

    for (
        (left_index, left),
        (right_index, right),
    ) in pairwise(tagged):
        if (left.phase.value, left.slot) >= (
            right.phase.value,
            right.slot,
        ):
            raise RuntimeError(f"Middleware phase ladder is not monotonic: {left.layer_id} ({left.phase.name}:{left.slot}, index {left_index}) must precede {right.layer_id} ({right.phase.name}:{right.slot}, index {right_index})")


def validate_middleware_dispatch_constraints(
    middlewares: Sequence[AgentMiddleware],
    constraints: Sequence[MiddlewareDispatchConstraint],
) -> None:
    """Validate intent in runtime hook order without changing registration."""

    positions = {metadata.layer_id: index for index, middleware in enumerate(middlewares) if (metadata := middleware_layer_metadata(middleware)) is not None}
    for constraint in constraints:
        first_position = positions.get(constraint.first)
        then_position = positions.get(constraint.then)
        if first_position is None or then_position is None:
            continue
        direction = middleware_dispatch_direction(constraint.hook)
        valid = first_position > then_position if direction is MiddlewareDispatchDirection.REVERSE else first_position < then_position
        if not valid:
            raise RuntimeError(f"Middleware dispatch constraint '{constraint.name}' violated for {constraint.hook.value}: {constraint.first} must run before {constraint.then}. {constraint.why}")


__all__ = [
    "MiddlewareDispatchConstraint",
    "MiddlewareChainDescription",
    "MiddlewareDispatchDirection",
    "MiddlewareHook",
    "MiddlewareLayerDescription",
    "MiddlewareLayerMetadata",
    "MiddlewarePhase",
    "assign_middleware_layer",
    "describe_middleware_chain",
    "middleware_dispatch_direction",
    "middleware_dispatch_order",
    "middleware_hooks",
    "middleware_layer_metadata",
    "validate_middleware_dispatch_constraints",
    "validate_middleware_phase_ladder",
]
