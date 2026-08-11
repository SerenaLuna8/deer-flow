"use client";

import { createContext, useContext } from "react";

import type { WorkflowFlowPort } from "@/components/projects/workflows/canvas/workflow-canvas-adapter";

export type WorkflowKeyboardPortActivation = (
  nodeId: string,
  port: WorkflowFlowPort,
) => void;

export const WorkflowCanvasInteractionContext = createContext<{
  onKeyboardPortActivate?: WorkflowKeyboardPortActivation;
}>({});

export const useWorkflowCanvasInteraction = () =>
  useContext(WorkflowCanvasInteractionContext);
