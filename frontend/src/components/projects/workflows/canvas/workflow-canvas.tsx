"use client";

import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  type Connection,
  type EdgeTypes,
  type NodeTypes,
  type ReactFlowInstance,
  type ReactFlowProps,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import "./workflow-canvas.css";

import {
  projectWorkflowFlow,
  workflowConnectionAllowed,
  type WorkflowCanvasDocumentInput,
  type WorkflowCanvasSpecInput,
  type WorkflowConnectionValidator,
  type WorkflowFlowEdge,
  type WorkflowFlowNode,
} from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  WorkflowCanvasInteractionContext,
  type WorkflowKeyboardPortActivation,
} from "@/components/projects/workflows/canvas/workflow-canvas-interaction";
import { WorkflowEdge } from "@/components/projects/workflows/edges/workflow-edge";
import { BASIC_WORKFLOW_NODE_CARDS } from "@/components/projects/workflows/nodes/basic-node-cards";
import { BRANCH_LOOP_WORKFLOW_NODE_CARDS } from "@/components/projects/workflows/nodes/branch-loop-node-cards";
import { HttpRequestWorkflowNode } from "@/components/projects/workflows/nodes/http-node-card";
import { PythonCodeWorkflowNode } from "@/components/projects/workflows/nodes/python-code-node-card";
import { WorkflowNodeCard } from "@/components/projects/workflows/nodes/workflow-node";
import type { NodeCatalogResponseV1 } from "@/core/project-workflows/catalog";
import type { WorkflowDraftPortLocale } from "@/core/project-workflows/editor/ports";
import type { WorkflowValidationIssueTarget } from "@/core/project-workflows/editor/validation";
import { cn } from "@/lib/utils";

/** Static registries prevent React Flow from remounting every renderer. */
export const WORKFLOW_NODE_TYPES = {
  start: BASIC_WORKFLOW_NODE_CARDS.start,
  llm: BASIC_WORKFLOW_NODE_CARDS.llm,
  condition: BRANCH_LOOP_WORKFLOW_NODE_CARDS.condition,
  transform: BASIC_WORKFLOW_NODE_CARDS.transform,
  variable_aggregate: BRANCH_LOOP_WORKFLOW_NODE_CARDS.variable_aggregate,
  loop: BRANCH_LOOP_WORKFLOW_NODE_CARDS.loop,
  http_request: HttpRequestWorkflowNode,
  python_code: PythonCodeWorkflowNode,
  end: BASIC_WORKFLOW_NODE_CARDS.end,
  unsupported: WorkflowNodeCard,
} as const satisfies NodeTypes;

export const WORKFLOW_EDGE_TYPES = {
  workflow: WorkflowEdge,
} as const satisfies EdgeTypes;

type WorkflowReactFlowProps = ReactFlowProps<
  WorkflowFlowNode,
  WorkflowFlowEdge
>;

export type WorkflowFocusInstance = {
  fitView: (options: {
    duration: number;
    maxZoom: number;
    nodes?: Array<{ id: string }>;
    padding: number;
  }) => Promise<boolean>;
  getEdges: () => Array<{ id: string; source: string; target: string }>;
  getInternalNode: (id: string) =>
    | {
        internals: { positionAbsolute: { x: number; y: number } };
        measured: { height?: number; width?: number };
      }
    | undefined;
  setCenter: (
    x: number,
    y: number,
    options: { duration: number; zoom: number },
  ) => Promise<boolean>;
};

export type WorkflowFocusRoot = {
  focus: () => void;
  querySelectorAll: (selector: string) => ArrayLike<unknown>;
};

type WorkflowFocusDataset = {
  workflowEdgeId?: string;
  workflowFocusKind?: string;
  workflowNodeId?: string;
  workflowPortId?: string;
};

type WorkflowFocusableElement = {
  dataset: WorkflowFocusDataset;
  focus: () => void;
};

const isWorkflowFocusableElement = (
  candidate: unknown,
): candidate is WorkflowFocusableElement => {
  if (candidate === null || typeof candidate !== "object") return false;
  const element = candidate as Partial<WorkflowFocusableElement>;
  return element.dataset !== undefined && typeof element.focus === "function";
};

const matchesWorkflowFocusTarget = (
  dataset: WorkflowFocusDataset,
  target: Exclude<WorkflowValidationIssueTarget, { kind: "document" }>,
): boolean => {
  if (target.kind === "node") {
    return (
      dataset.workflowFocusKind === "node" &&
      dataset.workflowNodeId === target.node_id
    );
  }
  if (target.kind === "edge") {
    return (
      dataset.workflowFocusKind === "edge" &&
      dataset.workflowEdgeId === target.edge_id
    );
  }
  return (
    dataset.workflowFocusKind === "port" &&
    dataset.workflowNodeId === target.node_id &&
    dataset.workflowPortId === target.port_id
  );
};

/** Focus only elements owned by this Canvas instance; target values never enter a selector. */
export function focusWorkflowTargetElement(
  root: WorkflowFocusRoot,
  target: WorkflowValidationIssueTarget,
): boolean {
  if (target.kind === "document") {
    root.focus();
    return true;
  }

  for (const candidate of Array.from(
    root.querySelectorAll("[data-workflow-focus-kind]"),
  )) {
    if (
      isWorkflowFocusableElement(candidate) &&
      matchesWorkflowFocusTarget(candidate.dataset, target)
    ) {
      candidate.focus();
      return true;
    }
  }
  return false;
}

export async function focusWorkflowValidationTarget(
  instance: WorkflowFocusInstance,
  target: WorkflowValidationIssueTarget | null,
): Promise<void> {
  if (target === null) return;
  if (target.kind === "document") {
    await instance.fitView({ duration: 240, maxZoom: 1.2, padding: 0.2 });
    return;
  }
  if (target.kind === "edge") {
    const edge = instance
      .getEdges()
      .find((candidate) => candidate.id === target.edge_id);
    if (edge) {
      await instance.fitView({
        duration: 240,
        maxZoom: 1.2,
        nodes: [{ id: edge.source }, { id: edge.target }],
        padding: 0.35,
      });
      return;
    }
  }
  const nodeId = target.node_id;
  if (nodeId === undefined) return;
  const node = instance.getInternalNode(nodeId);
  if (node === undefined) {
    await instance.fitView({
      duration: 240,
      maxZoom: 1.2,
      nodes: [{ id: nodeId }],
      padding: 0.45,
    });
    return;
  }
  const { x, y } = node.internals.positionAbsolute;
  await instance.setCenter(
    x + (node.measured.width ?? 0) / 2,
    y + (node.measured.height ?? 0) / 2,
    { duration: 240, zoom: 1.2 },
  );
}

export async function focusWorkflowValidationTargetInCanvas(
  instance: WorkflowFocusInstance,
  root: WorkflowFocusRoot,
  target: WorkflowValidationIssueTarget | null,
  isCurrent: () => boolean = () => true,
): Promise<boolean> {
  if (target === null) return false;
  try {
    await focusWorkflowValidationTarget(instance, target);
  } catch {
    return false;
  }
  return isCurrent() && focusWorkflowTargetElement(root, target);
}

export type WorkflowCanvasProps = {
  canvas: WorkflowCanvasDocumentInput;
  catalog?: NodeCatalogResponseV1 | null;
  children?: ReactNode;
  className?: string;
  focusTarget?: WorkflowValidationIssueTarget | null;
  locale?: WorkflowDraftPortLocale;
  readOnly?: boolean;
  spec: WorkflowCanvasSpecInput;
  isValidConnection?: WorkflowConnectionValidator;
  onConnect?: (connection: Connection) => void;
  onEdgesDelete?: WorkflowReactFlowProps["onEdgesDelete"];
  onInit?: WorkflowReactFlowProps["onInit"];
  onKeyboardPortActivate?: WorkflowKeyboardPortActivation;
  onNodeClick?: WorkflowReactFlowProps["onNodeClick"];
  onNodeDrag?: WorkflowReactFlowProps["onNodeDrag"];
  onNodeDragStart?: WorkflowReactFlowProps["onNodeDragStart"];
  onNodeDragStop?: WorkflowReactFlowProps["onNodeDragStop"];
  onNodesDelete?: WorkflowReactFlowProps["onNodesDelete"];
};

export function WorkflowCanvas({
  canvas,
  catalog,
  children,
  className,
  focusTarget = null,
  isValidConnection,
  locale = "zh-CN",
  onConnect,
  onEdgesDelete,
  onInit,
  onKeyboardPortActivate,
  onNodeClick,
  onNodeDrag,
  onNodeDragStart,
  onNodeDragStop,
  onNodesDelete,
  readOnly = false,
  spec,
}: WorkflowCanvasProps) {
  const canvasRootRef = useRef<HTMLElement | null>(null);
  const [flowInstance, setFlowInstance] = useState<ReactFlowInstance<
    WorkflowFlowNode,
    WorkflowFlowEdge
  > | null>(null);
  const projection = useMemo(
    () =>
      projectWorkflowFlow(spec, canvas, {
        catalog,
        focusTarget,
        locale,
        readOnly,
      }),
    [canvas, catalog, focusTarget, locale, readOnly, spec],
  );
  const interaction = useMemo(
    () => ({ onKeyboardPortActivate }),
    [onKeyboardPortActivate],
  );

  const initialize = useCallback<NonNullable<WorkflowReactFlowProps["onInit"]>>(
    (instance) => {
      setFlowInstance(instance);
      onInit?.(instance);
    },
    [onInit],
  );

  useEffect(() => {
    const root = canvasRootRef.current;
    if (flowInstance === null || focusTarget === null || root === null) return;

    let current = true;
    void focusWorkflowValidationTargetInCanvas(
      flowInstance,
      root,
      focusTarget,
      () => current,
    );
    return () => {
      current = false;
    };
  }, [flowInstance, focusTarget]);

  const validateConnection = useCallback(
    (candidate: Connection | WorkflowFlowEdge) =>
      !("id" in candidate) &&
      workflowConnectionAllowed(candidate, readOnly, isValidConnection),
    [isValidConnection, readOnly],
  );

  const connect = useCallback(
    (connection: Connection) => {
      if (!readOnly) onConnect?.(connection);
    },
    [onConnect, readOnly],
  );

  const deleteNodes = useCallback<
    NonNullable<WorkflowReactFlowProps["onNodesDelete"]>
  >(
    (nodes) => {
      if (!readOnly) onNodesDelete?.(nodes);
    },
    [onNodesDelete, readOnly],
  );

  const deleteEdges = useCallback<
    NonNullable<WorkflowReactFlowProps["onEdgesDelete"]>
  >(
    (edges) => {
      if (!readOnly) onEdgesDelete?.(edges);
    },
    [onEdgesDelete, readOnly],
  );

  const nodeDragStart = useCallback<
    NonNullable<WorkflowReactFlowProps["onNodeDragStart"]>
  >(
    (event, node, nodes) => {
      if (!readOnly) onNodeDragStart?.(event, node, nodes);
    },
    [onNodeDragStart, readOnly],
  );

  const nodeDrag = useCallback<
    NonNullable<WorkflowReactFlowProps["onNodeDrag"]>
  >(
    (event, node, nodes) => {
      if (!readOnly) onNodeDrag?.(event, node, nodes);
    },
    [onNodeDrag, readOnly],
  );

  const nodeDragStop = useCallback<
    NonNullable<WorkflowReactFlowProps["onNodeDragStop"]>
  >(
    (event, node, nodes) => {
      if (!readOnly) onNodeDragStop?.(event, node, nodes);
    },
    [onNodeDragStop, readOnly],
  );

  return (
    <section
      aria-label="工作流画布"
      className={cn("relative h-full min-h-96 w-full", className)}
      data-workflow-focus-kind="document"
      ref={canvasRootRef}
      tabIndex={-1}
    >
      <WorkflowCanvasInteractionContext.Provider value={interaction}>
        <ReactFlow<WorkflowFlowNode, WorkflowFlowEdge>
          deleteKeyCode={readOnly ? null : ["Backspace", "Delete"]}
          edges={projection.edges}
          edgeTypes={WORKFLOW_EDGE_TYPES}
          elementsSelectable
          fitView
          isValidConnection={validateConnection}
          nodes={projection.nodes}
          nodesConnectable={!readOnly}
          nodesDraggable={!readOnly}
          nodesFocusable
          nodeTypes={WORKFLOW_NODE_TYPES}
          onConnect={connect}
          onEdgesDelete={deleteEdges}
          onInit={initialize}
          onNodeClick={onNodeClick}
          onNodeDrag={nodeDrag}
          onNodeDragStart={nodeDragStart}
          onNodeDragStop={nodeDragStop}
          onNodesDelete={deleteNodes}
          panOnScroll
          selectionOnDrag={!readOnly}
          zoomOnDoubleClick={false}
        >
          <Background color="var(--border)" gap={24} size={1} />
          <MiniMap<WorkflowFlowNode>
            ariaLabel="工作流缩略图"
            pannable
            zoomable
          />
          <Controls aria-label="工作流画布控制" showInteractive={false} />
          {children}
        </ReactFlow>
      </WorkflowCanvasInteractionContext.Provider>
    </section>
  );
}
