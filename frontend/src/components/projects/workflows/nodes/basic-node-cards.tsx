"use client";

import type { NodeProps } from "@xyflow/react";
import { memo } from "react";

import type { WorkflowFlowNode } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import { WorkflowNodeCard } from "@/components/projects/workflows/nodes/workflow-node";

type BasicWorkflowNodeKind = "start" | "llm" | "transform" | "end";

const BASIC_NODE_SUMMARY: Record<BasicWorkflowNodeKind, string> = {
  start: "输入声明",
  llm: "模型调用 · 固定无工具",
  transform: "受限模板",
  end: "输出映射 · 无下一步",
};

function basicWorkflowNode(kind: BasicWorkflowNodeKind, displayName: string) {
  return memo(function BasicWorkflowNode(props: NodeProps<WorkflowFlowNode>) {
    return (
      <div aria-label={`${displayName}节点卡片`} data-basic-node-card={kind}>
        <p className="sr-only">{BASIC_NODE_SUMMARY[kind]}</p>
        <WorkflowNodeCard {...props} />
      </div>
    );
  });
}

export const StartWorkflowNode = basicWorkflowNode("start", "开始");
export const LlmWorkflowNode = basicWorkflowNode("llm", "大模型");
export const TransformWorkflowNode = basicWorkflowNode("transform", "模板转换");
export const EndWorkflowNode = basicWorkflowNode("end", "结束");

export const BASIC_WORKFLOW_NODE_CARDS = Object.freeze({
  start: StartWorkflowNode,
  llm: LlmWorkflowNode,
  transform: TransformWorkflowNode,
  end: EndWorkflowNode,
});
