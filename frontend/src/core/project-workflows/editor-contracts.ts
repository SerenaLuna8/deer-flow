import { z } from "zod";

import {
  canonicalWorkflowCursorSchema,
  safePreviewV1Schema,
  workflowActivationIdSchema,
  workflowAttemptSchema,
  workflowEventSafeErrorV1Schema,
  workflowIterationPathSchema,
  workflowRunStatusV1Schema,
} from "./transport";
import {
  edgeIdSchema,
  workflowNodeKindSchema,
  workflowPortIdSchema,
} from "./types";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const canonicalUuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const finiteNumberSchema = z.number().finite();
const nonnegativeSafeIntegerSchema = z
  .number()
  .int()
  .min(0)
  .max(MAX_SAFE_INTEGER);
const boundedSectionIdSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9._:-]+$/);

const positionSchema = z
  .object({ x: finiteNumberSchema, y: finiteNumberSchema })
  .strict();

const connectionEndpointSchema = z
  .object({
    node_id: canonicalUuidSchema,
    port_id: workflowPortIdSchema,
  })
  .strict();

const workflowEditorSelectionV1Schema = z
  .object({
    node_ids: z.array(canonicalUuidSchema).max(10_000),
    edge_ids: z.array(edgeIdSchema).max(20_000),
  })
  .strict()
  .superRefine((value, context) => {
    if (new Set(value.node_ids).size !== value.node_ids.length) {
      context.addIssue({
        code: "custom",
        message: "Editor node selection must be unique",
        path: ["node_ids"],
      });
    }
    if (new Set(value.edge_ids).size !== value.edge_ids.length) {
      context.addIssue({
        code: "custom",
        message: "Editor edge selection must be unique",
        path: ["edge_ids"],
      });
    }
  });

const workflowNodeDragInteractionV1Schema = z
  .object({
    kind: z.literal("node_drag"),
    node_ids: z.array(canonicalUuidSchema).min(1).max(10_000),
    transient_positions: z.record(canonicalUuidSchema, positionSchema),
  })
  .strict()
  .superRefine((value, context) => {
    const nodeIds = new Set(value.node_ids);
    const positionIds = new Set(Object.keys(value.transient_positions));
    if (
      nodeIds.size !== value.node_ids.length ||
      nodeIds.size !== positionIds.size ||
      [...nodeIds].some((nodeId) => !positionIds.has(nodeId))
    ) {
      context.addIssue({
        code: "custom",
        message:
          "Node drag positions must exactly match the unique dragged node identities",
        path: ["transient_positions"],
      });
    }
  });

export const workflowEditorInteractionV1Schema = z.union([
  z.object({ kind: z.literal("idle") }).strict(),
  workflowNodeDragInteractionV1Schema,
  z
    .object({
      kind: z.literal("connection"),
      source: connectionEndpointSchema,
      target: connectionEndpointSchema.nullable(),
    })
    .strict(),
]);

/**
 * Per-Workbench transient UI state. It is neither a Draft payload nor history.
 * React Flow measurements and renderer internals remain disposable adapter state.
 */
export const workflowEditorSessionV1Schema = z
  .object({
    schema_version: z.literal(1),
    viewport: z
      .object({
        x: finiteNumberSchema,
        y: finiteNumberSchema,
        zoom: finiteNumberSchema.positive(),
      })
      .strict(),
    selection: workflowEditorSelectionV1Schema,
    inspector: z
      .object({
        open: z.boolean(),
        node_id: canonicalUuidSchema.nullable(),
        tab: z.enum(["settings", "last_run"]),
        width_px: z.number().int().min(400).max(600),
        expanded_section_ids: z.array(boundedSectionIdSchema).max(256),
        scroll_top: finiteNumberSchema.nonnegative(),
      })
      .strict(),
    palette: z
      .object({
        open: z.boolean(),
        anchor: connectionEndpointSchema.nullable(),
      })
      .strict(),
    interaction: workflowEditorInteractionV1Schema,
  })
  .strict();

export const workflowNodeRuntimeAttemptV1Schema = z
  .object({
    node_id: canonicalUuidSchema,
    node_type: workflowNodeKindSchema,
    activation_id: workflowActivationIdSchema,
    iteration_path: workflowIterationPathSchema,
    attempt: workflowAttemptSchema,
    status: z.enum([
      "queued",
      "provisioning",
      "running",
      "collecting",
      "cleanup_pending",
      "succeeded",
      "failed",
      "timed_out",
      "cancelled",
    ]),
    output_preview: safePreviewV1Schema.nullable(),
    error: workflowEventSafeErrorV1Schema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    const failed = value.status === "failed" || value.status === "timed_out";
    if (failed !== (value.error !== null)) {
      context.addIssue({
        code: "custom",
        message: "Node runtime failure status and safe error must agree",
        path: ["error"],
      });
    }
    if (value.status !== "succeeded" && value.output_preview !== null) {
      context.addIssue({
        code: "custom",
        message: "Only a succeeded node may retain an output preview",
        path: ["output_preview"],
      });
    }
  });

const workflowRuntimeProgressV1Schema = z
  .object({
    completed_nodes: nonnegativeSafeIntegerSchema,
    active_nodes: nonnegativeSafeIntegerSchema,
    total_nodes: nonnegativeSafeIntegerSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.completed_nodes + value.active_nodes > value.total_nodes) {
      context.addIssue({
        code: "custom",
        message: "Workflow runtime progress exceeds the total node count",
        path: ["total_nodes"],
      });
    }
  });

/**
 * Safe event-fold projection. It owns no authored input, Code source, HTTP
 * request material, Credential value, raw log, traceback, or infrastructure ID.
 */
export const workflowRuntimeProjectionV1Schema = z
  .object({
    schema_version: z.literal(1),
    scope: z
      .object({
        account_id: canonicalUuidSchema,
        project_id: canonicalUuidSchema,
        workflow_id: canonicalUuidSchema,
        run_id: canonicalUuidSchema,
        workflow_version_id: canonicalUuidSchema,
      })
      .strict(),
    cursor: canonicalWorkflowCursorSchema,
    run_status: workflowRunStatusV1Schema,
    progress: workflowRuntimeProgressV1Schema.nullable(),
    node_attempts: z.array(workflowNodeRuntimeAttemptV1Schema).max(100_000),
    output_preview: safePreviewV1Schema.nullable(),
    error: workflowEventSafeErrorV1Schema.nullable(),
    // Human Input is outside the first batch. A later schema version must add
    // its closed safe projection rather than accepting an open future object.
    wait: z.null(),
  })
  .strict()
  .superRefine((value, context) => {
    const identities = value.node_attempts.map((attempt) =>
      JSON.stringify([
        attempt.node_id,
        attempt.activation_id,
        attempt.iteration_path,
        attempt.attempt,
      ]),
    );
    if (new Set(identities).size !== identities.length) {
      context.addIssue({
        code: "custom",
        message: "Workflow runtime attempt coordinates must be unique",
        path: ["node_attempts"],
      });
    }

    const failed =
      value.run_status === "failed" ||
      value.run_status === "side_effect_unknown";
    if (failed !== (value.error !== null)) {
      context.addIssue({
        code: "custom",
        message: "Workflow runtime failure status and safe error must agree",
        path: ["error"],
      });
    }
    if (value.run_status !== "succeeded" && value.output_preview !== null) {
      context.addIssue({
        code: "custom",
        message: "Only a succeeded Workflow Run may retain an output preview",
        path: ["output_preview"],
      });
    }
  });

export type WorkflowEditorInteractionV1 = z.infer<
  typeof workflowEditorInteractionV1Schema
>;
export type WorkflowEditorSessionV1 = z.infer<
  typeof workflowEditorSessionV1Schema
>;
export type WorkflowNodeRuntimeAttemptV1 = z.infer<
  typeof workflowNodeRuntimeAttemptV1Schema
>;
export type WorkflowRuntimeProjectionV1 = z.infer<
  typeof workflowRuntimeProjectionV1Schema
>;
