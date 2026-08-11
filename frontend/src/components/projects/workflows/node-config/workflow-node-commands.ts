import type { WorkflowPersistedDocumentV1 } from "@/core/project-workflows/boundaries";
import type { WorkflowEditorCommand } from "@/core/project-workflows/editor/store";
import {
  edgeIdSchema,
  workflowNodeKindSchema,
  workflowPortIdSchema,
  type WorkflowNodeKind,
} from "@/core/project-workflows/types";

type NextStepCandidate = {
  nodeType: WorkflowNodeKind;
  targetPortId: string;
  title: string;
};

type JsonObject = Record<string, unknown>;

const canonicalUuid =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const asObject = (value: unknown): JsonObject | null =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonObject)
    : null;

const finitePosition = (value: { x: number; y: number }): boolean =>
  Number.isFinite(value.x) && Number.isFinite(value.y);

const sourcePosition = (
  document: WorkflowPersistedDocumentV1,
  nodeId: string,
): { x: number; y: number } => {
  const position = document.canvas.node_layouts?.find(
    (layout) => layout.node_id === nodeId,
  )?.position;
  return position &&
    typeof position.x === "number" &&
    typeof position.y === "number" &&
    finitePosition({ x: position.x, y: position.y })
    ? { x: position.x, y: position.y }
    : { x: 0, y: 0 };
};

const newNode = (
  nodeId: string,
  nodeType: WorkflowNodeKind,
  scope: { kind: "root" } | { kind: "loop_body"; loop_node_id: string },
) => ({
  id: nodeId,
  type: nodeType,
  type_version: 1 as const,
  scope,
  config: {},
});

export function createWorkflowConnectCommand({
  connection,
  edgeId,
}: {
  connection: {
    source: string | null;
    sourceHandle: string | null;
    target: string | null;
    targetHandle: string | null;
  };
  edgeId: string;
}): Extract<WorkflowEditorCommand, { type: "connect" }> | null {
  if (
    connection.source === null ||
    connection.target === null ||
    connection.sourceHandle === null ||
    connection.targetHandle === null ||
    !canonicalUuid.test(connection.source) ||
    !canonicalUuid.test(connection.target) ||
    !workflowPortIdSchema.safeParse(connection.sourceHandle).success ||
    !workflowPortIdSchema.safeParse(connection.targetHandle).success ||
    !edgeIdSchema.safeParse(edgeId).success
  ) {
    return null;
  }
  return {
    type: "connect",
    transition: {
      id: edgeId,
      source: {
        node_id: connection.source,
        port_id: connection.sourceHandle,
      },
      target: {
        node_id: connection.target,
        port_id: connection.targetHandle,
      },
    },
    routing: "smoothstep",
  };
}

export function createWorkflowPaletteNodeCommand({
  nodeId,
  nodeType,
  position,
}: {
  nodeId: string;
  nodeType: WorkflowNodeKind;
  position: { x: number; y: number };
}): Extract<WorkflowEditorCommand, { type: "add_node" }> | null {
  if (
    !canonicalUuid.test(nodeId) ||
    !workflowNodeKindSchema.safeParse(nodeType).success ||
    !finitePosition(position)
  ) {
    return null;
  }
  return {
    type: "add_node",
    node: newNode(nodeId, nodeType, { kind: "root" }),
    layout: { node_id: nodeId, position: { ...position } },
  };
}

export function createWorkflowNextStepCommand({
  candidate,
  document,
  nextId,
  sourceNodeId,
  sourcePortId,
}: {
  candidate: NextStepCandidate;
  document: WorkflowPersistedDocumentV1;
  nextId: () => string;
  sourceNodeId: string;
  sourcePortId: string;
}): Extract<
  WorkflowEditorCommand,
  { type: "add_loop_body_entry" | "add_next_step" }
> | null {
  const source = document.spec.nodes?.find((node) => node.id === sourceNodeId);
  if (!source || source.type === "end" || candidate.nodeType === "start") {
    return null;
  }
  const sourceScope = asObject(source.scope);
  const loopId =
    sourceScope?.kind === "loop_body" &&
    typeof sourceScope.loop_node_id === "string"
      ? sourceScope.loop_node_id
      : null;
  const entersLoop = source.type === "loop" && sourcePortId === "body";
  const bodyLoopId = entersLoop ? source.id : loopId;
  if (
    bodyLoopId !== null &&
    (candidate.nodeType === "end" || candidate.nodeType === "loop")
  ) {
    return null;
  }
  if (sourcePortId === "body" && !entersLoop) return null;

  const nodeId = nextId();
  if (!canonicalUuid.test(nodeId)) return null;
  const base = sourcePosition(document, sourceNodeId);
  const scope = bodyLoopId
    ? ({ kind: "loop_body", loop_node_id: bodyLoopId } as const)
    : ({ kind: "root" } as const);
  const position = entersLoop
    ? { x: 40, y: 80 }
    : { x: base.x + 320, y: base.y };
  const node = newNode(nodeId, candidate.nodeType, scope);
  const layout = {
    node_id: nodeId,
    position,
    ...(bodyLoopId ? { parent_node_id: bodyLoopId } : {}),
  };

  if (entersLoop) {
    return {
      type: "add_loop_body_entry",
      loop_node_id: sourceNodeId,
      node,
      layout,
      set_as_exit: true,
    };
  }

  const edgeId = nextId();
  if (!edgeIdSchema.safeParse(edgeId).success) return null;
  return {
    type: "add_next_step",
    source: { node_id: sourceNodeId, port_id: sourcePortId },
    node,
    layout,
    transition: {
      id: edgeId,
      target_port_id: candidate.targetPortId,
      routing: "smoothstep",
    },
  };
}
