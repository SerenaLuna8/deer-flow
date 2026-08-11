import type { Connection, Edge, Node } from "@xyflow/react";

import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import {
  nodeCatalogResponseV1Schema,
  workflowNodeRegistryV1,
  type NodeCatalogResponseV1,
} from "@/core/project-workflows/catalog";
import {
  resolveDraftNodePorts,
  workflowDraftPortSignature,
  type WorkflowDraftPort,
  type WorkflowDraftPortLocale,
} from "@/core/project-workflows/editor/ports";
import type { WorkflowValidationIssueTarget } from "@/core/project-workflows/editor/validation";
import {
  controlTransitionSchema,
  workflowNodeKindSchema,
  workflowNodeScopeSchema,
  workflowNodeSpecSchema,
  type WorkflowNodeKind,
} from "@/core/project-workflows/types";

export type WorkflowCanvasNodeType = WorkflowNodeKind | "unsupported";
export type WorkflowNodeSupportState =
  | "supported"
  | "incomplete"
  | "unsupported";

export type WorkflowFlowPort = {
  id: string;
  label: string;
  kind: "control" | "data";
  cardinality: "one" | "many";
  direction: "input" | "output";
};

export type WorkflowFlowNodeData = Record<string, unknown> & {
  nodeId: string;
  nodeKind: WorkflowNodeKind | "unknown";
  originalType: string | null;
  title: string;
  supportState: WorkflowNodeSupportState;
  statusLabel: string;
  availabilityReason: string | null;
  readOnly: boolean;
  disabled: boolean;
  inputPorts: readonly WorkflowFlowPort[];
  outputPorts: readonly WorkflowFlowPort[];
  focusedPortId: string | null;
  portSignature: string;
};

export type WorkflowFlowNode = Node<
  WorkflowFlowNodeData,
  WorkflowCanvasNodeType
>;

export type WorkflowFlowEdgeData = Record<string, unknown> & {
  edgeId: string;
  routing: "bezier" | "smoothstep";
  readOnly: boolean;
  statusLabel: string;
};

export type WorkflowFlowEdge = Edge<WorkflowFlowEdgeData, "workflow">;

export type WorkflowFlowProjection = {
  nodes: WorkflowFlowNode[];
  edges: WorkflowFlowEdge[];
};

export type WorkflowCanvasSpecInput = WorkflowPersistedDocumentV1["spec"];
export type WorkflowCanvasDocumentInput = WorkflowPersistedDocumentV1["canvas"];

export type WorkflowFlowProjectionOptions = {
  catalog?: NodeCatalogResponseV1 | null;
  focusTarget?: WorkflowValidationIssueTarget | null;
  locale?: WorkflowDraftPortLocale;
  readOnly?: boolean;
};

export type WorkflowConnectionValidator = (connection: Connection) => boolean;

const canonicalUuidPattern =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const canonicalDefinitionByType = new Map(
  workflowNodeRegistryV1.map((definition) => [definition.type, definition]),
);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const safeNodeId = (value: unknown): string | null =>
  typeof value === "string" && canonicalUuidPattern.test(value) ? value : null;

const safeLabel = (value: unknown, fallback: string): string =>
  typeof value === "string" && value.length > 0 ? value : fallback;

const projectedPort = (port: WorkflowDraftPort): WorkflowFlowPort => ({
  id: port.id,
  label: port.label,
  kind: port.kind,
  cardinality: port.cardinality,
  direction: port.direction,
});

const readArray = (value: unknown): unknown[] =>
  Array.isArray(value) ? value : [];

const fallbackPosition = (index: number) => ({
  x: (index % 4) * 320,
  y: Math.floor(index / 4) * 220,
});

const nodeLayouts = (
  canvas: unknown,
): Map<string, { x: number; y: number }> => {
  const layouts = new Map<string, { x: number; y: number }>();
  const rawCanvas = isRecord(canvas) ? canvas : {};
  for (const layout of readArray(rawCanvas.node_layouts)) {
    if (!isRecord(layout)) continue;
    const nodeId = safeNodeId(layout.node_id);
    const position = isRecord(layout.position) ? layout.position : {};
    const x = position.x;
    const y = position.y;
    if (
      nodeId &&
      typeof x === "number" &&
      Number.isFinite(x) &&
      typeof y === "number" &&
      Number.isFinite(y)
    ) {
      layouts.set(nodeId, { x, y });
    }
  }
  return layouts;
};

const edgeLayouts = (canvas: unknown): Map<string, "bezier" | "smoothstep"> => {
  const layouts = new Map<string, "bezier" | "smoothstep">();
  const rawCanvas = isRecord(canvas) ? canvas : {};
  for (const layout of readArray(rawCanvas.edge_layouts)) {
    if (!isRecord(layout)) continue;
    if (
      typeof layout.edge_id === "string" &&
      (layout.routing === "bezier" || layout.routing === "smoothstep")
    ) {
      layouts.set(layout.edge_id, layout.routing);
    }
  }
  return layouts;
};

const catalogAvailability = (
  catalog: NodeCatalogResponseV1 | null | undefined,
): Map<WorkflowNodeKind, { enabled: boolean; reason: string | null }> => {
  const parsed = nodeCatalogResponseV1Schema.safeParse(catalog);
  if (!parsed.success) return new Map();
  return new Map(
    parsed.data.entries.map((entry) => [
      entry.definition.type,
      entry.availability.state === "enabled"
        ? { enabled: true, reason: null }
        : {
            enabled: false,
            reason: entry.availability.reason_code,
          },
    ]),
  );
};

const semanticParentId = (
  rawNode: Record<string, unknown>,
  rawNodesById: ReadonlyMap<string, Record<string, unknown>>,
): string | null => {
  const scope = workflowNodeScopeSchema.safeParse(rawNode.scope);
  if (!scope.success || scope.data.kind === "root") return null;
  const parent = rawNodesById.get(scope.data.loop_node_id);
  if (parent?.type !== "loop") return null;
  const parentScope = workflowNodeScopeSchema.safeParse(parent.scope);
  if (!parentScope.success || parentScope.data.kind !== "root") return null;
  return scope.data.loop_node_id;
};

const statusLabel = (
  supportState: WorkflowNodeSupportState,
  disabled: boolean,
): string => {
  if (supportState === "incomplete") return "状态：定义不完整（只读）";
  if (supportState === "unsupported") return "状态：不支持（只读）";
  if (disabled) return "状态：不可用（只读）";
  return "状态：可用";
};

/**
 * Pure, disposable React Flow projection. It deliberately selects only card,
 * handle, position, and transition fields; authored configs and runtime state
 * never enter React Flow data and React Flow output is never a save payload.
 */
export function projectWorkflowFlow(
  spec: unknown,
  canvas: unknown,
  options: WorkflowFlowProjectionOptions = {},
): WorkflowFlowProjection {
  const rawSpec: Record<string, unknown> = isRecord(spec) ? spec : {};
  const rawNodes = readArray(rawSpec.nodes).filter(isRecord);
  const rawNodesById = new Map<string, Record<string, unknown>>();
  rawNodes.forEach((node) => {
    const nodeId = safeNodeId(node.id);
    if (nodeId && !rawNodesById.has(nodeId)) rawNodesById.set(nodeId, node);
  });

  const positions = nodeLayouts(canvas);
  const availability = catalogAvailability(options.catalog);
  const locale = options.locale ?? "zh-CN";
  const draftDocument = {
    spec: { ...rawSpec, nodes: rawNodes },
    canvas: isRecord(canvas) ? canvas : { schema_version: 1 },
  } as unknown as WorkflowPersistedDocumentV1;

  const nodes: WorkflowFlowNode[] = [];
  rawNodes.forEach((rawNode, index) => {
    const nodeId = safeNodeId(rawNode.id);
    if (!nodeId || nodes.some((node) => node.id === nodeId)) return;
    const kindResult = workflowNodeKindSchema.safeParse(rawNode.type);
    const knownKind = kindResult.success ? kindResult.data : null;
    const completeNode = workflowNodeSpecSchema.safeParse(rawNode).success;
    const supportState: WorkflowNodeSupportState =
      knownKind === null
        ? "unsupported"
        : rawNode.type_version === null || rawNode.type_version === undefined
          ? "incomplete"
          : rawNode.type_version !== 1
            ? "unsupported"
            : completeNode
              ? "supported"
              : "incomplete";
    const entryAvailability = knownKind
      ? availability.get(knownKind)
      : undefined;
    const unavailable = entryAvailability?.enabled !== true;
    const disabled = supportState !== "supported" || unavailable;
    const readOnly = options.readOnly === true || disabled;
    const canonicalTitle = knownKind
      ? canonicalDefinitionByType.get(knownKind)?.title_i18n[locale]
      : undefined;
    const title = safeLabel(
      rawNode.custom_label,
      canonicalTitle ?? "不支持的节点",
    );
    const parentId = semanticParentId(rawNode, rawNodesById);
    const resolved = knownKind
      ? resolveDraftNodePorts(draftDocument, nodeId, locale)
      : { inputPorts: [], outputPorts: [] };
    const inputPorts = resolved.inputPorts.map(projectedPort);
    const outputPorts = resolved.outputPorts.map(projectedPort);
    const data: WorkflowFlowNodeData = {
      nodeId,
      nodeKind: knownKind ?? "unknown",
      originalType: typeof rawNode.type === "string" ? rawNode.type : null,
      title,
      supportState,
      statusLabel: statusLabel(supportState, disabled),
      availabilityReason:
        entryAvailability?.reason ??
        (knownKind && !entryAvailability
          ? "WORKFLOW_CATALOG_UNAVAILABLE"
          : null),
      readOnly,
      disabled,
      inputPorts,
      outputPorts,
      focusedPortId:
        options.focusTarget?.kind === "port" &&
        options.focusTarget.node_id === nodeId
          ? options.focusTarget.port_id
          : null,
      portSignature: workflowDraftPortSignature(resolved),
    };
    nodes.push({
      id: nodeId,
      type: supportState === "supported" ? knownKind! : "unsupported",
      position: positions.get(nodeId) ?? fallbackPosition(index),
      data,
      ...(parentId ? { parentId, extent: "parent" as const } : {}),
      ariaLabel: `工作流节点：${title}`,
      ariaRole: "group",
      connectable: !readOnly,
      deletable: !readOnly,
      draggable: !readOnly,
      selectable: true,
      focusable: true,
      selected:
        (options.focusTarget?.kind === "node" &&
          options.focusTarget.node_id === nodeId) ||
        (options.focusTarget?.kind === "port" &&
          options.focusTarget.node_id === nodeId),
    });
  });

  nodes.sort((left, right) => {
    if (left.parentId === right.id) return 1;
    if (right.parentId === left.id) return -1;
    if (left.parentId && !right.parentId) return 1;
    if (!left.parentId && right.parentId) return -1;
    return 0;
  });

  const edgeRouting = edgeLayouts(canvas);
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const transitions = readArray(rawSpec.transitions);
  const edges: WorkflowFlowEdge[] = [];
  for (const candidate of transitions) {
    const parsed = controlTransitionSchema.safeParse(candidate);
    if (!parsed.success || edges.some((edge) => edge.id === parsed.data.id)) {
      continue;
    }
    const source = nodeById.get(parsed.data.source.node_id);
    const target = nodeById.get(parsed.data.target.node_id);
    if (!source || !target) continue;
    const readOnly =
      options.readOnly === true || source.data.readOnly || target.data.readOnly;
    edges.push({
      id: parsed.data.id,
      type: "workflow",
      source: parsed.data.source.node_id,
      sourceHandle: parsed.data.source.port_id,
      target: parsed.data.target.node_id,
      targetHandle: parsed.data.target.port_id,
      animated: false,
      deletable: !readOnly,
      selectable: true,
      focusable: true,
      selected:
        (options.focusTarget?.kind === "edge" &&
          options.focusTarget.edge_id === parsed.data.id) ||
        (options.focusTarget?.kind === "port" &&
          options.focusTarget.edge_id === parsed.data.id),
      ariaLabel: `工作流连接：${parsed.data.id}`,
      data: {
        edgeId: parsed.data.id,
        routing: edgeRouting.get(parsed.data.id) ?? "bezier",
        readOnly,
        statusLabel: readOnly ? "状态：只读连接" : "状态：可编辑连接",
      },
    });
  }

  return { nodes, edges };
}

export function workflowConnectionAllowed(
  connection: Connection,
  readOnly: boolean,
  validator: WorkflowConnectionValidator | undefined,
): boolean {
  if (readOnly || validator === undefined) return false;
  return validator(connection) === true;
}
