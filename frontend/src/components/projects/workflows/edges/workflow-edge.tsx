"use client";

import {
  BaseEdge,
  getBezierPath,
  getSmoothStepPath,
  type EdgeProps,
} from "@xyflow/react";
import { memo } from "react";

import type { WorkflowFlowEdge } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";

export const WorkflowEdge = memo(function WorkflowEdge({
  data,
  id,
  markerEnd,
  sourcePosition,
  sourceX,
  sourceY,
  style,
  targetPosition,
  targetX,
  targetY,
}: EdgeProps<WorkflowFlowEdge>) {
  const pathOptions = {
    sourcePosition,
    sourceX,
    sourceY,
    targetPosition,
    targetX,
    targetY,
  };
  const [path] =
    data?.routing === "smoothstep"
      ? getSmoothStepPath(pathOptions)
      : getBezierPath(pathOptions);

  return (
    <g
      aria-label={data?.statusLabel ?? `工作流连接：${id}`}
      data-workflow-edge-id={data?.edgeId ?? id}
      data-workflow-focus-kind="edge"
      focusable="true"
      role="img"
      tabIndex={-1}
    >
      <BaseEdge id={id} markerEnd={markerEnd} path={path} style={style} />
    </g>
  );
});
