import { z } from "zod";

export { projectWorkflowEntryEnabled } from "./navigation";

import { edgeIdSchema, workflowNodeKindSchema } from "./types";
import { utf8ByteBoundedString, utf8ByteLength } from "./validation";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const POSTGRES_INTEGER_MAX = 2_147_483_647;
const POSTGRES_BIGINT_MAX = "9223372036854775807";

const nonnegativeSafeIntegerSchema = z
  .number()
  .int()
  .min(0)
  .max(MAX_SAFE_INTEGER);

const positiveSafeIntegerSchema = z.number().int().min(1).max(MAX_SAFE_INTEGER);

const uuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const printableRequestIdSchema = z
  .string()
  .min(1)
  .max(512)
  .regex(/^[\x20-\x7e]+$/);
const safeIdentifierSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z0-9._:-]+$/);
const safeCodeSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Z][A-Z0-9_]*$/);
const sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/);

export const canonicalWorkflowCursorSchema = z
  .string()
  .regex(/^(0|[1-9][0-9]*)$/)
  .refine(
    (value) =>
      value.length < POSTGRES_BIGINT_MAX.length ||
      (value.length === POSTGRES_BIGINT_MAX.length &&
        value <= POSTGRES_BIGINT_MAX),
    "Workflow cursor exceeds PostgreSQL signed BIGINT range",
  );

export const workflowActivationIdSchema = safeIdentifierSchema;
const workflowDatabasePositiveIntegerSchema = z
  .number()
  .int()
  .min(1)
  .max(POSTGRES_INTEGER_MAX);

export const workflowIterationPathSchema = z
  .array(workflowDatabasePositiveIntegerSchema)
  .max(16);
export const workflowAttemptSchema = workflowDatabasePositiveIntegerSchema;

const workflowControlPlaneReadyV1Schema = z
  .object({
    status: z.literal("ready"),
    code: z.literal("WORKFLOW_CONTROL_PLANE_READY"),
    workflow_enabled: z.literal(true),
    schema_ready: z.literal(true),
    admission_ready: z.boolean(),
    request_id: printableRequestIdSchema,
  })
  .strict();

const workflowDisabledV1Schema = z
  .object({
    status: z.literal("ready"),
    code: z.literal("WORKFLOW_DISABLED"),
    workflow_enabled: z.literal(false),
    schema_ready: z.literal(true),
    admission_ready: z.literal(false),
    request_id: printableRequestIdSchema,
  })
  .strict();

const workflowSchemaUnavailableV1Schema = z
  .object({
    status: z.literal("unavailable"),
    code: z.literal("WORKFLOW_SCHEMA_UNAVAILABLE"),
    workflow_enabled: z.literal(false),
    schema_ready: z.literal(false),
    admission_ready: z.literal(false),
    request_id: printableRequestIdSchema,
  })
  .strict();

const workflowPolicyUnavailableV1Schema = z
  .object({
    status: z.literal("unavailable"),
    code: z.literal("WORKFLOW_POLICY_UNAVAILABLE"),
    workflow_enabled: z.literal(false),
    schema_ready: z.literal(true),
    admission_ready: z.literal(false),
    request_id: printableRequestIdSchema,
  })
  .strict();

export const workflowProjectReadinessV1Schema = z.discriminatedUnion("code", [
  workflowControlPlaneReadyV1Schema,
  workflowDisabledV1Schema,
  workflowSchemaUnavailableV1Schema,
  workflowPolicyUnavailableV1Schema,
]);

export const safePreviewV1Schema = z
  .object({
    format: z.enum(["text", "json", "summary"]),
    text: utf8ByteBoundedString(0, 65_536),
    truncated: z.boolean(),
    redacted: z.boolean(),
    original_byte_count: nonnegativeSafeIntegerSchema.nullable().optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.original_byte_count !== null &&
      value.original_byte_count !== undefined &&
      value.original_byte_count < utf8ByteLength(value.text)
    ) {
      context.addIssue({
        code: "custom",
        message:
          "preview original byte count cannot be below retained UTF-8 bytes",
        path: ["original_byte_count"],
      });
    }
  });

export const workflowValidationIssueV1Schema = z
  .object({
    severity: z.enum(["error", "warning"]),
    code: safeCodeSchema,
    message: utf8ByteBoundedString(1, 2_048),
    path: z.array(z.string()).max(64),
    node_id: uuidSchema.nullable().optional(),
    edge_id: edgeIdSchema.nullable().optional(),
    port_id: safeIdentifierSchema.nullable().optional(),
  })
  .strict();

export const workflowErrorCodes = [
  "WORKFLOW_NOT_FOUND",
  "WORKFLOW_DRAFT_CONFLICT",
  "WORKFLOW_DRAFT_INVALID",
  "WORKFLOW_VERSION_NOT_EXECUTABLE",
  "WORKFLOW_NODE_TYPE_UNAVAILABLE",
  "WORKFLOW_DEPENDENCY_STALE",
  "WORKFLOW_INPUT_INVALID",
  "WORKFLOW_RUN_CONFLICT",
  "WORKFLOW_RUN_NOT_RESUMABLE",
  "WORKFLOW_RUN_RETRY_FORBIDDEN",
  "WORKFLOW_WAIT_CONFLICT",
  "WORKFLOW_WAIT_EXPIRED",
  "WORKFLOW_OUTPUT_INVALID",
  "WORKFLOW_COMPILER_UNAVAILABLE",
  "WORKFLOW_RUNTIME_POLICY_UNAVAILABLE",
  "WORKFLOW_RUNTIME_PROFILE_PENDING",
  "WORKFLOW_CODE_INVALID",
  "WORKFLOW_CODE_SYNTAX_ERROR",
  "WORKFLOW_CODE_SANDBOX_UNAVAILABLE",
  "WORKFLOW_CODE_SANDBOX_CLEANUP_FAILED",
  "WORKFLOW_CODE_INFRASTRUCTURE_ERROR",
  "WORKFLOW_CODE_TIMEOUT",
  "WORKFLOW_CODE_RESOURCE_EXHAUSTED",
  "WORKFLOW_CODE_OUTPUT_LIMIT",
  "WORKFLOW_CODE_OUTPUT_INVALID",
  "WORKFLOW_CODE_RUNTIME_ERROR",
  "WORKFLOW_VARIABLE_AGGREGATE_NO_VALUE",
  "WORKFLOW_VARIABLE_AGGREGATE_AMBIGUOUS",
  "WORKFLOW_LOOP_LIMIT_EXCEEDED",
  "WORKFLOW_HTTP_UNAVAILABLE",
  "WORKFLOW_HTTP_ENDPOINT_FORBIDDEN",
  "WORKFLOW_HTTP_REQUEST_INVALID",
  "WORKFLOW_HTTP_TIMEOUT",
  "WORKFLOW_HTTP_RESPONSE_LIMIT",
  "WORKFLOW_HTTP_RESPONSE_INVALID",
  "WORKFLOW_HTTP_TRANSPORT_ERROR",
  "SIDE_EFFECT_STATE_UNKNOWN",
] as const;

export const workflowErrorCodeSchema = z.enum(workflowErrorCodes);

export const workflowRunStatusesV1 = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
  "side_effect_unknown",
] as const;

export const workflowRunStatusV1Schema = z.enum(workflowRunStatusesV1);

export const workflowEventSafeErrorV1Schema = z
  .object({
    code: workflowErrorCodeSchema,
    safe_message: utf8ByteBoundedString(1, 2_048),
    line: positiveSafeIntegerSchema.nullable().optional(),
    column: positiveSafeIntegerSchema.nullable().optional(),
  })
  .strict();

export const workflowEventUsageV1Schema = z
  .object({
    model_calls: nonnegativeSafeIntegerSchema.nullable().optional(),
    input_tokens: nonnegativeSafeIntegerSchema.nullable().optional(),
    output_tokens: nonnegativeSafeIntegerSchema.nullable().optional(),
  })
  .strict();

export const workflowEmptyEventPayloadV1Schema = z.object({}).strict();

export const workflowNodeLifecycleEventPayloadV1Schema = z
  .object({
    node_type: workflowNodeKindSchema,
  })
  .strict();

export const workflowNodeDeltaEventPayloadV1Schema = z
  .object({
    node_type: z.literal("llm"),
    text: utf8ByteBoundedString(0, 16_384),
    truncated: z.boolean(),
  })
  .strict();

export const workflowNodeLogEventPayloadV1Schema = z
  .object({
    node_type: z.literal("python_code"),
    stream: z.enum(["stdout", "stderr"]),
    text: utf8ByteBoundedString(0, 65_536),
    truncated: z.boolean(),
  })
  .strict();

export const workflowNodeCompletedEventPayloadV1Schema = z
  .object({
    node_type: workflowNodeLifecycleEventPayloadV1Schema.shape.node_type,
    duration_ms: nonnegativeSafeIntegerSchema,
    output_preview: safePreviewV1Schema.nullable().optional(),
    usage: workflowEventUsageV1Schema.nullable().optional(),
    branch_port_id: safeIdentifierSchema.nullable().optional(),
    retry_count: nonnegativeSafeIntegerSchema.nullable().optional(),
    truncated: z.boolean().nullable().optional(),
  })
  .strict();

export const workflowNodeFailedEventPayloadV1Schema = z
  .object({
    node_type: workflowNodeLifecycleEventPayloadV1Schema.shape.node_type,
    duration_ms: nonnegativeSafeIntegerSchema,
    error: workflowEventSafeErrorV1Schema,
    retry_count: nonnegativeSafeIntegerSchema.nullable().optional(),
  })
  .strict();

export const workflowRunCompletedEventPayloadV1Schema = z
  .object({
    duration_ms: nonnegativeSafeIntegerSchema,
    output_preview: safePreviewV1Schema.nullable().optional(),
  })
  .strict();

export const workflowRunFailedEventPayloadV1Schema = z
  .object({
    duration_ms: nonnegativeSafeIntegerSchema,
    error: workflowEventSafeErrorV1Schema,
  })
  .strict();

export const workflowRunCancelledEventPayloadV1Schema = z
  .object({
    duration_ms: nonnegativeSafeIntegerSchema.nullable().optional(),
  })
  .strict();

export const workflowRunSideEffectUnknownEventPayloadV1Schema = z
  .object({
    code: z.literal("SIDE_EFFECT_STATE_UNKNOWN"),
    safe_message: utf8ByteBoundedString(1, 2_048),
  })
  .strict();

export const workflowEventPayloadV1Schema = z.union([
  workflowEmptyEventPayloadV1Schema,
  workflowNodeLifecycleEventPayloadV1Schema,
  workflowNodeDeltaEventPayloadV1Schema,
  workflowNodeLogEventPayloadV1Schema,
  workflowNodeCompletedEventPayloadV1Schema,
  workflowNodeFailedEventPayloadV1Schema,
  workflowRunCompletedEventPayloadV1Schema,
  workflowRunFailedEventPayloadV1Schema,
  workflowRunCancelledEventPayloadV1Schema,
  workflowRunSideEffectUnknownEventPayloadV1Schema,
]);

export const workflowEventTypesV1 = [
  "workflow.run.started",
  "workflow.node.queued",
  "workflow.node.started",
  "workflow.node.delta",
  "workflow.node.log",
  "workflow.node.completed",
  "workflow.node.failed",
  "workflow.run.completed",
  "workflow.run.failed",
  "workflow.run.cancelled",
  "workflow.run.side_effect_unknown",
] as const;

export const workflowEventTypeV1Schema = z.enum(workflowEventTypesV1);

const workflowEventBaseShape = {
  schema_version: z.literal(1),
  run_id: uuidSchema,
  workflow_version_id: uuidSchema,
  seq: canonicalWorkflowCursorSchema,
  occurred_at: z.string().datetime({ offset: true }),
};

const workflowNodeEventIdentityShape = {
  node_id: uuidSchema,
  activation_id: workflowActivationIdSchema,
  scope_path_hash: sha256HexSchema,
  iteration_path: workflowIterationPathSchema,
  attempt: workflowAttemptSchema,
};

const workflowRunEventIdentityShape = {
  node_id: z.null().optional(),
  activation_id: z.null().optional(),
  scope_path_hash: z.null().optional(),
  iteration_path: z.array(z.never()).max(0),
  attempt: z.null().optional(),
};

const workflowNodeEventEnvelope = <
  const EventType extends string,
  PayloadSchema extends z.ZodTypeAny,
>(
  type: EventType,
  payload: PayloadSchema,
) =>
  z
    .object({
      ...workflowEventBaseShape,
      ...workflowNodeEventIdentityShape,
      type: z.literal(type),
      payload,
    })
    .strict();

const workflowRunEventEnvelope = <
  const EventType extends string,
  PayloadSchema extends z.ZodTypeAny,
>(
  type: EventType,
  payload: PayloadSchema,
) =>
  z
    .object({
      ...workflowEventBaseShape,
      ...workflowRunEventIdentityShape,
      type: z.literal(type),
      payload,
    })
    .strict();

export const workflowEventEnvelopeV1Schema = z.discriminatedUnion("type", [
  workflowRunEventEnvelope(
    "workflow.run.started",
    workflowEmptyEventPayloadV1Schema,
  ),
  workflowNodeEventEnvelope(
    "workflow.node.queued",
    workflowNodeLifecycleEventPayloadV1Schema,
  ),
  workflowNodeEventEnvelope(
    "workflow.node.started",
    workflowNodeLifecycleEventPayloadV1Schema,
  ),
  workflowNodeEventEnvelope(
    "workflow.node.delta",
    workflowNodeDeltaEventPayloadV1Schema,
  ),
  workflowNodeEventEnvelope(
    "workflow.node.log",
    workflowNodeLogEventPayloadV1Schema,
  ),
  workflowNodeEventEnvelope(
    "workflow.node.completed",
    workflowNodeCompletedEventPayloadV1Schema,
  ),
  workflowNodeEventEnvelope(
    "workflow.node.failed",
    workflowNodeFailedEventPayloadV1Schema,
  ),
  workflowRunEventEnvelope(
    "workflow.run.completed",
    workflowRunCompletedEventPayloadV1Schema,
  ),
  workflowRunEventEnvelope(
    "workflow.run.failed",
    workflowRunFailedEventPayloadV1Schema,
  ),
  workflowRunEventEnvelope(
    "workflow.run.cancelled",
    workflowRunCancelledEventPayloadV1Schema,
  ),
  workflowRunEventEnvelope(
    "workflow.run.side_effect_unknown",
    workflowRunSideEffectUnknownEventPayloadV1Schema,
  ),
]);

export const workflowNodeLastRunV1Schema = z
  .object({
    run_id: uuidSchema,
    node_id: uuidSchema,
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
    started_at: z.string().datetime({ offset: true }).nullable().optional(),
    duration_ms: nonnegativeSafeIntegerSchema.nullable().optional(),
    input_preview: safePreviewV1Schema.nullable().optional(),
    output_preview: safePreviewV1Schema.nullable().optional(),
    error: workflowEventSafeErrorV1Schema.nullable().optional(),
    usage: workflowEventUsageV1Schema.nullable().optional(),
    branch_port_id: safeIdentifierSchema.nullable().optional(),
    retry_count: nonnegativeSafeIntegerSchema.nullable().optional(),
    truncated: z.boolean().nullable().optional(),
  })
  .strict();

export type WorkflowProjectReadinessV1 = z.infer<
  typeof workflowProjectReadinessV1Schema
>;

export type SafePreviewV1 = z.infer<typeof safePreviewV1Schema>;
export type WorkflowValidationIssueV1 = z.infer<
  typeof workflowValidationIssueV1Schema
>;
export type WorkflowErrorCode = z.infer<typeof workflowErrorCodeSchema>;
export type WorkflowRunStatusV1 = z.infer<typeof workflowRunStatusV1Schema>;
export type WorkflowEventSafeErrorV1 = z.infer<
  typeof workflowEventSafeErrorV1Schema
>;
export type WorkflowEventUsageV1 = z.infer<typeof workflowEventUsageV1Schema>;
export type WorkflowEmptyEventPayloadV1 = z.infer<
  typeof workflowEmptyEventPayloadV1Schema
>;
export type WorkflowNodeLifecycleEventPayloadV1 = z.infer<
  typeof workflowNodeLifecycleEventPayloadV1Schema
>;
export type WorkflowNodeDeltaEventPayloadV1 = z.infer<
  typeof workflowNodeDeltaEventPayloadV1Schema
>;
export type WorkflowNodeLogEventPayloadV1 = z.infer<
  typeof workflowNodeLogEventPayloadV1Schema
>;
export type WorkflowNodeCompletedEventPayloadV1 = z.infer<
  typeof workflowNodeCompletedEventPayloadV1Schema
>;
export type WorkflowNodeFailedEventPayloadV1 = z.infer<
  typeof workflowNodeFailedEventPayloadV1Schema
>;
export type WorkflowRunCompletedEventPayloadV1 = z.infer<
  typeof workflowRunCompletedEventPayloadV1Schema
>;
export type WorkflowRunFailedEventPayloadV1 = z.infer<
  typeof workflowRunFailedEventPayloadV1Schema
>;
export type WorkflowRunCancelledEventPayloadV1 = z.infer<
  typeof workflowRunCancelledEventPayloadV1Schema
>;
export type WorkflowRunSideEffectUnknownEventPayloadV1 = z.infer<
  typeof workflowRunSideEffectUnknownEventPayloadV1Schema
>;
export type WorkflowEventPayloadV1 = z.infer<
  typeof workflowEventPayloadV1Schema
>;
export type WorkflowEventTypeV1 = z.infer<typeof workflowEventTypeV1Schema>;
export type WorkflowEventEnvelopeV1 = z.infer<
  typeof workflowEventEnvelopeV1Schema
>;
export type WorkflowNodeLastRunV1 = z.infer<typeof workflowNodeLastRunV1Schema>;
