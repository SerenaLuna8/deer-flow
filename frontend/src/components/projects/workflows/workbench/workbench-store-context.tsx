"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useSyncExternalStore,
} from "react";

import type {
  WorkflowEditorCommand,
  WorkflowEditorHistory,
  WorkflowEditorState,
  WorkflowEditorStore,
} from "@/core/project-workflows/editor/store";

export type WorkflowWorkbenchAddNextStepCommand = Extract<
  WorkflowEditorCommand,
  { type: "add_loop_body_entry" | "add_next_step" }
>;

export type WorkflowWorkbenchCommand = WorkflowEditorCommand;

export type WorkflowWorkbenchHistory = WorkflowEditorHistory;

/**
 * The shell deliberately sees only the state required to present editor chrome.
 * The editor-core store remains the authority for commands, validation, history,
 * and persisted projections.
 */
export type WorkflowWorkbenchStoreSnapshot = WorkflowEditorState;

export type WorkflowWorkbenchStorePort = Pick<
  WorkflowEditorStore,
  "dispatch" | "getState" | "redo" | "setEditorSession" | "subscribe" | "undo"
> &
  Partial<
    Pick<
      WorkflowEditorStore,
      | "beginNodeDrag"
      | "cancelNodeDrag"
      | "commitNodeDrag"
      | "updateNodeDragPosition"
    >
  >;

const WorkflowWorkbenchStoreContext =
  createContext<WorkflowWorkbenchStorePort | null>(null);

export function WorkflowWorkbenchStoreProvider({
  children,
  store,
}: {
  children: ReactNode;
  store: WorkflowWorkbenchStorePort;
}) {
  return (
    <WorkflowWorkbenchStoreContext.Provider value={store}>
      {children}
    </WorkflowWorkbenchStoreContext.Provider>
  );
}

export function useWorkflowWorkbenchStore(): WorkflowWorkbenchStorePort {
  const store = useContext(WorkflowWorkbenchStoreContext);
  if (!store) {
    throw new Error(
      "Workflow Workbench components require a per-instance store provider",
    );
  }
  return store;
}

export function useWorkflowWorkbenchSnapshot(): WorkflowWorkbenchStoreSnapshot {
  const store = useWorkflowWorkbenchStore();
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

export function updateWorkflowEditorSession(
  store: WorkflowWorkbenchStorePort,
  update: (
    session: WorkflowWorkbenchStoreSnapshot["editorSession"],
  ) => WorkflowWorkbenchStoreSnapshot["editorSession"],
): void {
  const previous = store.getState().editorSession;
  const next = update(previous);
  if (next !== previous) store.setEditorSession(next);
}
