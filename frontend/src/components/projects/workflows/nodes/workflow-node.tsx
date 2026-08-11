"use client";

import { Handle, Position, useUpdateNodeInternals } from "@xyflow/react";
import type { NodeProps } from "@xyflow/react";
import { memo, useEffect } from "react";

import type {
  WorkflowFlowNode,
  WorkflowFlowPort,
} from "@/components/projects/workflows/canvas/workflow-canvas-adapter";
import {
  useWorkflowCanvasInteraction,
  type WorkflowKeyboardPortActivation,
} from "@/components/projects/workflows/canvas/workflow-canvas-interaction";
import { cn } from "@/lib/utils";

const portOffset = (index: number, total: number): string =>
  `${((index + 1) / (total + 1)) * 100}%`;

type WorkflowPortHandleProps = {
  connectable: boolean;
  focused: boolean;
  nodeId: string;
  onKeyboardPortActivate?: WorkflowKeyboardPortActivation;
  port: WorkflowFlowPort;
  index: number;
  total: number;
};

type WorkflowPortKeyboardEvent = {
  key: string;
  preventDefault: () => void;
  stopPropagation: () => void;
};

export function handleWorkflowPortKeyDown(
  event: WorkflowPortKeyboardEvent,
  nodeId: string,
  port: WorkflowFlowPort,
  enabled: boolean,
  onActivate: WorkflowKeyboardPortActivation | undefined,
): boolean {
  if (
    !enabled ||
    onActivate === undefined ||
    (event.key !== "Enter" && event.key !== " ")
  ) {
    return false;
  }
  event.preventDefault();
  event.stopPropagation();
  onActivate(nodeId, port);
  return true;
}

const WorkflowPortHandle = memo(function WorkflowPortHandle({
  connectable,
  focused,
  nodeId,
  onKeyboardPortActivate,
  port,
  index,
  total,
}: WorkflowPortHandleProps) {
  const input = port.direction === "input";
  const directionLabel = input ? "输入端口" : "输出端口";
  return (
    <Handle
      aria-current={focused ? "true" : undefined}
      aria-disabled={!connectable}
      aria-label={`${directionLabel}：${port.id}`}
      className={cn(focused && "ring-ring ring-2 ring-offset-2")}
      data-focus-target={focused ? "true" : undefined}
      data-workflow-focus-kind="port"
      data-workflow-node-id={nodeId}
      data-workflow-port-id={port.id}
      id={port.id}
      isConnectable={connectable}
      onKeyDown={(event) => {
        handleWorkflowPortKeyDown(
          event,
          nodeId,
          port,
          connectable,
          onKeyboardPortActivate,
        );
      }}
      position={input ? Position.Left : Position.Right}
      role="button"
      style={{ top: portOffset(index, total) }}
      tabIndex={0}
      title={`${directionLabel}：${port.label}`}
      type={input ? "target" : "source"}
    />
  );
});

export const WorkflowNodeCard = memo(function WorkflowNodeCard({
  data,
  id,
  isConnectable,
  selected,
}: NodeProps<WorkflowFlowNode>) {
  const updateNodeInternals = useUpdateNodeInternals();
  const { onKeyboardPortActivate } = useWorkflowCanvasInteraction();

  useEffect(() => {
    updateNodeInternals(id);
  }, [data.portSignature, id, updateNodeInternals]);

  const connectable = isConnectable && !data.readOnly && !data.disabled;

  return (
    <article
      aria-label={`工作流节点：${data.title}`}
      className={cn(
        "bg-card text-card-foreground relative min-w-64 rounded-xl border shadow-sm",
        selected && "ring-ring ring-2 ring-offset-2",
        data.disabled && "border-dashed opacity-80",
      )}
      data-node-kind={data.nodeKind}
      data-support-state={data.supportState}
      data-workflow-focus-kind="node"
      data-workflow-node-id={id}
      tabIndex={-1}
    >
      {data.inputPorts.map((port, index) => (
        <WorkflowPortHandle
          connectable={connectable}
          focused={data.focusedPortId === port.id}
          index={index}
          key={`input:${port.id}`}
          nodeId={id}
          onKeyboardPortActivate={onKeyboardPortActivate}
          port={port}
          total={data.inputPorts.length}
        />
      ))}
      {data.outputPorts.map((port, index) => (
        <WorkflowPortHandle
          connectable={connectable}
          focused={data.focusedPortId === port.id}
          index={index}
          key={`output:${port.id}`}
          nodeId={id}
          onKeyboardPortActivate={onKeyboardPortActivate}
          port={port}
          total={data.outputPorts.length}
        />
      ))}

      <header className="border-b px-4 py-3">
        <p className="text-muted-foreground text-xs font-medium tracking-wide uppercase">
          {data.nodeKind === "unknown" ? "Unsupported" : data.nodeKind}
        </p>
        <h3 className="mt-1 text-sm font-semibold">{data.title}</h3>
      </header>

      <div className="space-y-3 px-4 py-3">
        <p
          className={cn(
            "text-xs font-medium",
            data.disabled ? "text-muted-foreground" : "text-foreground",
          )}
          role="status"
        >
          {data.statusLabel}
        </p>
        <div className="text-muted-foreground grid grid-cols-2 gap-3 text-[11px]">
          <div>
            <p className="text-foreground font-medium">输入</p>
            {data.inputPorts.length === 0 ? (
              <p>无</p>
            ) : (
              <ul aria-label="输入端口列表">
                {data.inputPorts.map((port) => (
                  <li key={port.id}>输入端口：{port.id}</li>
                ))}
              </ul>
            )}
          </div>
          <div className="text-right">
            <p className="text-foreground font-medium">输出</p>
            {data.outputPorts.length === 0 ? (
              <p>无</p>
            ) : (
              <ul aria-label="输出端口列表">
                {data.outputPorts.map((port) => (
                  <li key={port.id}>输出端口：{port.id}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </div>
    </article>
  );
});

export const UnsupportedWorkflowNode = WorkflowNodeCard;
