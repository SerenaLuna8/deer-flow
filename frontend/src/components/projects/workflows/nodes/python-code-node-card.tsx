"use client";

import type { NodeProps } from "@xyflow/react";
import { memo } from "react";

import type {
  WorkflowFlowNode,
  WorkflowFlowNodeData,
} from "@/components/projects/workflows/canvas/workflow-canvas-adapter";

import { WorkflowNodeCard } from "./workflow-node";

const safeProjectedData = (
  data: WorkflowFlowNodeData,
): WorkflowFlowNodeData => ({
  nodeId: data.nodeId,
  nodeKind: data.nodeKind,
  originalType: data.originalType,
  title: data.title,
  supportState: data.supportState,
  statusLabel: data.statusLabel,
  availabilityReason: data.availabilityReason,
  readOnly: data.readOnly,
  disabled: data.disabled,
  inputPorts: data.inputPorts,
  outputPorts: data.outputPorts,
  focusedPortId: data.focusedPortId,
  portSignature: data.portSignature,
});

export const PythonCodeWorkflowNode = memo(function PythonCodeWorkflowNode(
  props: NodeProps<WorkflowFlowNode>,
) {
  return (
    <div data-python-code-node-card="true">
      <div
        aria-label="Python runtime contract"
        className="bg-muted/50 border-border flex items-center justify-between gap-3 border-x border-t px-4 py-2 text-xs"
      >
        <span className="font-medium">Python 3.12</span>
        <span className="text-muted-foreground">
          隔离 Sandbox · availability：
          {props.data.disabled ? "disabled" : "enabled"}
        </span>
      </div>
      <WorkflowNodeCard {...props} data={safeProjectedData(props.data)} />
    </div>
  );
});

export const PYTHON_CODE_WORKFLOW_NODE_CARDS = Object.freeze({
  python_code: PythonCodeWorkflowNode,
});
