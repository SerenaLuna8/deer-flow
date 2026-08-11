"use client";

import type { NodeProps } from "@xyflow/react";

import type {
  WorkflowFlowNode,
  WorkflowFlowNodeData,
} from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import { WorkflowNodeCard } from "@/components/projects/workflows/nodes/workflow-node";

const HTTP_METHODS = new Set(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]);

const safeMethod = (value: unknown): string =>
  typeof value === "string" && HTTP_METHODS.has(value) ? value : "HTTP";

const policyLabel = (value: unknown): string => {
  if (value === "approved") return "Endpoint policy 已批准";
  if (value === "unavailable") return "Endpoint policy 不可用";
  return "Endpoint policy 状态未知";
};

const slotLabel = (value: unknown): string => {
  if (value === "declared") return "Slot 声明就绪";
  if (value === "missing") return "Slot 声明缺失";
  if (value === "not_required") return "无需 Credential slot";
  return "Slot 状态未知";
};

export function HttpRequestWorkflowNode(props: NodeProps<WorkflowFlowNode>) {
  const safeData: WorkflowFlowNodeData = {
    nodeId: props.data.nodeId,
    nodeKind: props.data.nodeKind,
    originalType: props.data.originalType,
    title: props.data.title,
    supportState: props.data.supportState,
    statusLabel: props.data.statusLabel,
    availabilityReason: props.data.availabilityReason,
    readOnly: props.data.readOnly,
    disabled: props.data.disabled,
    inputPorts: props.data.inputPorts,
    outputPorts: props.data.outputPorts,
    focusedPortId: props.data.focusedPortId,
    portSignature: props.data.portSignature,
  };
  return (
    <div
      aria-label="HTTP 请求节点卡片"
      className="space-y-1"
      data-http-node-card="true"
    >
      <div className="border-border bg-card text-muted-foreground grid grid-cols-3 gap-2 rounded-t-xl border border-b-0 px-4 py-2 text-[10px]">
        <span className="text-foreground font-semibold">
          {safeMethod(props.data.httpMethod)}
        </span>
        <span>{policyLabel(props.data.httpPolicyState)}</span>
        <span>{slotLabel(props.data.httpCredentialSlotState)}</span>
      </div>
      <WorkflowNodeCard {...props} data={safeData} />
    </div>
  );
}
