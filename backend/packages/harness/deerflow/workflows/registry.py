"""Compiler/executor metadata layered over the shared G01 Node Registry.

Type/version, bilingual titles, retry semantics, config schemas, and fixed or
derived instance ports come directly from ``catalog_contracts``.  G12 adds
only compiler-facing exact lookup and the external executor-port marker; it
does not create another Node Catalog authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol

from deerflow.workflows.catalog_contracts import (
    FIRST_BATCH_NODE_REGISTRY_V1,
    NodeTypeDefinition,
    PortDefinition,
    _resolve_workflow_node_instance_ports_for_node_v1,
)
from deerflow.workflows.contracts import JsonValue, WorkflowNodeSpec, WorkflowSpecV1

ExecutorPort = Literal["llm", "code", "http"]


@dataclass(frozen=True, slots=True)
class WorkflowNodeDefinition:
    shared: NodeTypeDefinition
    executor_port: ExecutorPort | None

    @property
    def type(self) -> str:
        return self.shared.type

    @property
    def version(self) -> int:
        return self.shared.version

    @property
    def title_zh_cn(self) -> str:
        return self.shared.title_i18n.zh_cn

    @property
    def title_en_us(self) -> str:
        return self.shared.title_i18n.en_us


@dataclass(frozen=True, slots=True)
class WorkflowNodeExecutionContext:
    """Opaque runtime coordinates supplied by a lease-authorized Worker."""

    workflow_run_id: str
    node_id: str
    activation_id: str
    attempt: int


class WorkflowNodeExecutor(Protocol):
    """The common later-runtime node executor boundary."""

    async def execute(
        self,
        *,
        inputs: Mapping[str, JsonValue],
        context: WorkflowNodeExecutionContext,
    ) -> Mapping[str, JsonValue]: ...


class WorkflowLlmExecutionPort(Protocol):
    """Injected exact-model execution; no model catalog lookup is permitted."""

    async def execute_llm(self, *, request: object, context: WorkflowNodeExecutionContext) -> Mapping[str, JsonValue]: ...


class WorkflowCodeExecutionPort(Protocol):
    """Injected isolated-code execution; no provider/config lookup is permitted."""

    async def execute_code(self, *, request: object, context: WorkflowNodeExecutionContext) -> Mapping[str, JsonValue]: ...


class WorkflowHttpExecutionPort(Protocol):
    """Injected controlled-egress execution; no ambient HTTP client is permitted."""

    async def execute_http(self, *, request: object, context: WorkflowNodeExecutionContext) -> Mapping[str, JsonValue]: ...


class WorkflowNodeRegistryError(LookupError):
    """An authored type/version has no exact installed compiler definition."""


class WorkflowNodeRegistry:
    """Immutable exact type/version lookup over shared definitions."""

    def __init__(self, definitions: Sequence[WorkflowNodeDefinition]) -> None:
        entries: dict[tuple[str, int], WorkflowNodeDefinition] = {}
        for definition in definitions:
            key = (definition.type, definition.version)
            if key in entries:
                raise ValueError("duplicate Workflow runtime registry entry")
            entries[key] = definition
        self._entries = MappingProxyType(entries)
        self._ordered_keys = tuple(entries)

    def keys(self) -> tuple[tuple[str, int], ...]:
        return self._ordered_keys

    def require(self, node_type: object, version: object) -> WorkflowNodeDefinition:
        if not isinstance(node_type, str):
            raise WorkflowNodeRegistryError("WORKFLOW_NODE_TYPE_UNAVAILABLE: node type must be a string")
        if type(version) is not int:
            raise WorkflowNodeRegistryError("WORKFLOW_NODE_VERSION_UNAVAILABLE: node version must be an integer")
        definition = self._entries.get((node_type, version))
        if definition is not None:
            return definition
        if any(registered_type == node_type for registered_type, _registered_version in self._entries):
            raise WorkflowNodeRegistryError("WORKFLOW_NODE_VERSION_UNAVAILABLE: exact node contract is not installed")
        raise WorkflowNodeRegistryError("WORKFLOW_NODE_TYPE_UNAVAILABLE: node type is not installed")


_EXECUTOR_PORTS: dict[str, ExecutorPort] = {
    "llm": "llm",
    "http_request": "http",
    "python_code": "code",
}

FIRST_BATCH_RUNTIME_REGISTRY = WorkflowNodeRegistry(
    tuple(
        WorkflowNodeDefinition(
            shared=definition,
            executor_port=_EXECUTOR_PORTS.get(definition.type),
        )
        for definition in FIRST_BATCH_NODE_REGISTRY_V1
    )
)


def resolve_node_ports(
    spec: WorkflowSpecV1,
    node: WorkflowNodeSpec,
) -> tuple[tuple[PortDefinition, ...], tuple[PortDefinition, ...]]:
    """Resolve ports through the single G01 instance-port authority."""

    FIRST_BATCH_RUNTIME_REGISTRY.require(node.type, node.type_version)
    resolved = _resolve_workflow_node_instance_ports_for_node_v1(spec, node)
    return tuple(resolved.input_ports), tuple(resolved.output_ports)


__all__ = [
    "FIRST_BATCH_RUNTIME_REGISTRY",
    "WorkflowCodeExecutionPort",
    "WorkflowHttpExecutionPort",
    "WorkflowLlmExecutionPort",
    "WorkflowNodeDefinition",
    "WorkflowNodeExecutionContext",
    "WorkflowNodeExecutor",
    "WorkflowNodeRegistry",
    "WorkflowNodeRegistryError",
    "resolve_node_ports",
]
