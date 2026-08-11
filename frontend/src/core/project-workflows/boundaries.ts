import { z } from "zod";

import {
  workflowDraftCanvasV1Schema,
  workflowDraftSpecV1Schema,
} from "./definition-contracts";
import {
  workflowEditorSessionV1Schema,
  workflowRuntimeProjectionV1Schema,
} from "./editor-contracts";

/** The Draft save authority: partial authored semantics plus persisted layout. */
export const workflowPersistedDocumentV1Schema = z
  .object({
    spec: workflowDraftSpecV1Schema,
    canvas: workflowDraftCanvasV1Schema,
  })
  .strict();

/**
 * Four deliberately named layers. Only `authored` and `persisted_layout` may
 * cross the Draft save boundary; transient and runtime projections never do.
 */
export const workflowWorkbenchLayersV1Schema = z
  .object({
    authored: workflowDraftSpecV1Schema,
    persisted_layout: workflowDraftCanvasV1Schema,
    transient: workflowEditorSessionV1Schema,
    runtime: workflowRuntimeProjectionV1Schema.nullable(),
  })
  .strict();

export const WORKFLOW_LAYER_BOUNDARIES_V1 = Object.freeze({
  authored: "spec",
  persisted: "canvas",
  transient: "editor_session",
  runtime: "runtime_projection",
} as const);

export type WorkflowPersistedDocumentV1 = z.infer<
  typeof workflowPersistedDocumentV1Schema
>;
export type WorkflowWorkbenchLayersV1 = z.infer<
  typeof workflowWorkbenchLayersV1Schema
>;

export const projectWorkflowPersistedDocumentV1 = (
  layers: WorkflowWorkbenchLayersV1,
): WorkflowPersistedDocumentV1 =>
  workflowPersistedDocumentV1Schema.parse({
    spec: layers.authored,
    canvas: layers.persisted_layout,
  });
