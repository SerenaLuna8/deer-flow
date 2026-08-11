"use client";

import type { NodeProps } from "@xyflow/react";
import { memo } from "react";

import type {
  WorkflowFlowNode,
  WorkflowFlowNodeData,
} from "@/components/projects/workflows/canvas/workflow-canvas-adapter";

import { WorkflowNodeCard } from "./workflow-node";

type BranchLoopNodeKind = "condition" | "variable_aggregate" | "loop";

const SAFE_SUMMARIES: Readonly<Record<BranchLoopNodeKind, string>> = {
  condition: "条件出口 · 有序 IF / ELIF + ELSE",
  variable_aggregate: "聚合输出 · 互斥分支与 MISSING 语义",
  loop: "循环输出 · 有界 do_until · body / done",
};

function safeProjectedData(data: WorkflowFlowNodeData): WorkflowFlowNodeData {
  return {
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
  };
}

function BranchLoopWorkflowNodeCard({
  kind,
  props,
}: {
  kind: BranchLoopNodeKind;
  props: NodeProps<WorkflowFlowNode>;
}) {
  return (
    <div data-branch-loop-node-card={kind}>
      <p className="sr-only" role="status">
        {SAFE_SUMMARIES[kind]}
      </p>
      <WorkflowNodeCard {...props} data={safeProjectedData(props.data)} />
    </div>
  );
}

export const ConditionWorkflowNode = memo(function ConditionWorkflowNode(
  props: NodeProps<WorkflowFlowNode>,
) {
  return <BranchLoopWorkflowNodeCard kind="condition" props={props} />;
});

export const VariableAggregateWorkflowNode = memo(
  function VariableAggregateWorkflowNode(props: NodeProps<WorkflowFlowNode>) {
    return (
      <BranchLoopWorkflowNodeCard kind="variable_aggregate" props={props} />
    );
  },
);

export const LoopWorkflowNode = memo(function LoopWorkflowNode(
  props: NodeProps<WorkflowFlowNode>,
) {
  return <BranchLoopWorkflowNodeCard kind="loop" props={props} />;
});

export const BRANCH_LOOP_WORKFLOW_NODE_CARDS = Object.freeze({
  condition: ConditionWorkflowNode,
  variable_aggregate: VariableAggregateWorkflowNode,
  loop: LoopWorkflowNode,
});
