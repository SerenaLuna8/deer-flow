"use client";

import { workflowNodeKindSchema } from "@/core/project-workflows/types";

import type {
  WorkflowNodeConfigPanelProps,
  WorkflowNodeConfigPanelRegistry,
} from "./contracts";

export type WorkflowNodeConfigProps = WorkflowNodeConfigPanelProps & {
  registry: WorkflowNodeConfigPanelRegistry;
};

/** Select one closed first-batch panel without reflecting on renderer names. */
export function WorkflowNodeConfig({
  registry,
  ...props
}: WorkflowNodeConfigProps) {
  const parsed = workflowNodeKindSchema.safeParse(props.node.type);
  if (!parsed.success || props.node.type_version !== 1) {
    return (
      <p className="text-muted-foreground p-4 text-sm" role="status">
        该节点类型或版本不受当前编辑器支持，已保持只读。
      </p>
    );
  }

  const Panel = registry[parsed.data];
  return <Panel {...props} />;
}
