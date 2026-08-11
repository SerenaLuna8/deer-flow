"use client";

import { createContext, type ReactNode, useContext } from "react";

import type { WorkflowEditorFlushRegistry } from "@/core/project-workflows/editor/flush-registry";

const WorkflowWorkbenchFlushContext =
  createContext<WorkflowEditorFlushRegistry | null>(null);

export function WorkflowWorkbenchFlushProvider({
  children,
  registry,
}: {
  children: ReactNode;
  registry: WorkflowEditorFlushRegistry;
}) {
  return (
    <WorkflowWorkbenchFlushContext.Provider value={registry}>
      {children}
    </WorkflowWorkbenchFlushContext.Provider>
  );
}

export function useWorkflowWorkbenchFlushRegistry(): WorkflowEditorFlushRegistry {
  const registry = useContext(WorkflowWorkbenchFlushContext);
  if (registry === null) {
    throw new Error(
      "Workflow controlled editors require a per-Workbench flush provider",
    );
  }
  return registry;
}

/**
 * The only ordering primitive used by save, validate, and publish. The action
 * snapshot is read after every registered controlled editor has committed.
 */
export function flushWorkflowEditorBeforeAction<Result>(
  registry: WorkflowEditorFlushRegistry,
  readSnapshot: () => Result,
): Result {
  registry.flushAll();
  return readSnapshot();
}
