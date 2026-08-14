import { z } from "zod";

const decimalStringSchema = z.string().regex(/^(?:0|[1-9][0-9]*)$/u);
const boundedIdSchema = z.string().min(1).max(256);
const toolCallIdSchema = z.string().min(1).max(128);
const timestampSchema = z.string().datetime({ offset: true });

export const executionApprovalStatusSchema = z.enum([
  "pending",
  "approved",
  "claimed",
  "finished",
  "launch_failed",
  "unknown",
  "denied",
  "expired",
  "cancelled",
]);

export type ExecutionApprovalStatus = z.infer<
  typeof executionApprovalStatusSchema
>;

const continuationRunSchema = z
  .object({
    run_id: boundedIdSchema,
    status: z.enum([
      "pending",
      "running",
      "error",
      "success",
      "timeout",
      "interrupted",
    ]),
  })
  .strict();

const executionDomainSchema = z
  .object({
    label: z.string().min(1).max(256),
    effective_user_label: z.string().min(1).max(256),
  })
  .strict();

const sourceAgentSchema = z
  .object({
    kind: z.enum(["lead", "subagent"]),
    label: z.string().min(1).max(256),
    path: z.array(z.string().min(1).max(256)).min(1).max(16),
  })
  .strict();

const commonProjectionShape = {
  approval_id: z.string().uuid(),
  source_run_id: boundedIdSchema,
  source_tool_call_id: toolCallIdSchema,
  version: decimalStringSchema,
  execution_domain: executionDomainSchema,
  command_preview: z.string().min(1).max(65_536),
  cwd_preview: z.string().min(1).max(4_096),
  timeout_seconds: z.number().int().positive().max(3_600),
  source_agent: sourceAgentSchema,
  risk_level: z.literal("host_execution"),
  warning_code: z.enum([
    "LOCAL_PROCESS_RUNS_ON_HOST",
    "HOST_EXECUTION_STATE_UNKNOWN",
  ]),
  continuation_run: continuationRunSchema.nullable(),
} as const;

const pendingProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("pending"),
    warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
    can_decide: z.boolean(),
    continuation_run: z.null(),
    decision_expires_at: timestampSchema,
    remaining_ttl_seconds: z.number().int().nonnegative().max(86_400),
  })
  .strict();

const approvedProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("approved"),
    warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
    can_decide: z.literal(false),
    // Admission of the continuation Run is a separate durable transition.
    // Keep the approved projection valid while that Run is still being linked.
    continuation_run: continuationRunSchema.nullable(),
    decision_at: timestampSchema,
    claim_expires_at: timestampSchema,
  })
  .strict();

const claimedProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("claimed"),
    warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
    can_decide: z.literal(false),
    continuation_run: continuationRunSchema,
    claimed_at: timestampSchema,
  })
  .strict();

const finishedProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("finished"),
    warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
    can_decide: z.literal(false),
    exit_code: z.number().int(),
    finished_at: timestampSchema,
    result_summary_code: z.string().min(1).max(128),
  })
  .strict();

const launchFailedProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("launch_failed"),
    warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
    can_decide: z.literal(false),
    finished_at: timestampSchema,
    reason_code: z.string().min(1).max(128),
  })
  .strict();

const unknownProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("unknown"),
    warning_code: z.literal("HOST_EXECUTION_STATE_UNKNOWN"),
    can_decide: z.literal(false),
    finished_at: timestampSchema,
  })
  .strict();

export const denialDeliveryStatusSchema = z.enum([
  "not_required",
  "pending",
  "admitted",
  "delivered",
  "failed",
]);

const deniedProjectionSchema = z
  .object({
    ...commonProjectionShape,
    status: z.literal("denied"),
    warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
    can_decide: z.literal(false),
    decision_at: timestampSchema,
    denial_delivery_status: denialDeliveryStatusSchema,
  })
  .strict();

function closedProjectionSchema(status: "expired" | "cancelled") {
  return z
    .object({
      ...commonProjectionShape,
      status: z.literal(status),
      warning_code: z.literal("LOCAL_PROCESS_RUNS_ON_HOST"),
      can_decide: z.literal(false),
      finished_at: timestampSchema,
      reason_code: z.string().min(1).max(128),
    })
    .strict();
}

export const executionApprovalProjectionSchema = z.discriminatedUnion(
  "status",
  [
    pendingProjectionSchema,
    approvedProjectionSchema,
    claimedProjectionSchema,
    finishedProjectionSchema,
    launchFailedProjectionSchema,
    unknownProjectionSchema,
    deniedProjectionSchema,
    closedProjectionSchema("expired"),
    closedProjectionSchema("cancelled"),
  ],
);

export type ExecutionApprovalProjection = z.infer<
  typeof executionApprovalProjectionSchema
>;

export const executionApprovalsActiveResponseSchema = z
  .object({
    schema_version: z.literal(1),
    server_time: timestampSchema,
    approval: executionApprovalProjectionSchema.nullable(),
  })
  .strict();

export type ExecutionApprovalsActiveResponse = z.infer<
  typeof executionApprovalsActiveResponseSchema
>;

const decisionInputCommon = {
  schema_version: z.literal(1),
  expected_version: decimalStringSchema,
  idempotency_key: z.string().uuid(),
} as const;

export const executionApprovalDecisionInputSchema = z.discriminatedUnion(
  "decision",
  [
    z
      .object({
        ...decisionInputCommon,
        decision: z.literal("allow_once"),
      })
      .strict(),
    z
      .object({
        ...decisionInputCommon,
        decision: z.literal("deny"),
      })
      .strict(),
  ],
);

export type ExecutionApprovalDecisionInput = z.infer<
  typeof executionApprovalDecisionInputSchema
>;

export type ExecutionApprovalDecision =
  ExecutionApprovalDecisionInput["decision"];

export function executionApprovalIsActive(
  approval: ExecutionApprovalProjection | null | undefined,
) {
  return (
    approval?.status === "pending" ||
    approval?.status === "approved" ||
    approval?.status === "claimed"
  );
}

export const executionApprovalBlocksSending = executionApprovalIsActive;

export function executionApprovalNeedsAdmissionRecovery(
  approval: ExecutionApprovalProjection | null | undefined,
) {
  return approval?.status === "approved" && approval.continuation_run === null;
}

export function executionApprovalContinuationRunId(
  approval: ExecutionApprovalProjection | null | undefined,
) {
  const continuation = approval?.continuation_run;
  if (!continuation) return null;
  return continuation.status === "pending" || continuation.status === "running"
    ? continuation.run_id
    : null;
}

function compareDecimalVersions(left: string, right: string) {
  if (left.length !== right.length) return left.length - right.length;
  return left.localeCompare(right);
}

export function selectNewerExecutionApprovalProjection(
  left: ExecutionApprovalProjection | null | undefined,
  right: ExecutionApprovalProjection | null | undefined,
) {
  if (!left) return right ?? null;
  if (!right) return left;
  if (left.approval_id !== right.approval_id) return right;
  return compareDecimalVersions(left.version, right.version) >= 0
    ? left
    : right;
}
