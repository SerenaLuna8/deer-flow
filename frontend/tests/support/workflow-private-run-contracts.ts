import { z } from "zod";

import {
  workflowCompilerSnapshotContractV1Schema,
  workflowSchemaCompatibilityCaseV1Schema,
} from "@/core/project-workflows/compatibility";
import {
  workflowOwnerPrivateRunV1Schema,
  workflowRunAdmissionRequestV1Schema,
  workflowRunAdmissionResponseV1Schema,
} from "@/core/project-workflows/run-contracts";
import { workflowRunStatusV1Schema } from "@/core/project-workflows/transport";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const positiveSafeIntegerSchema = z.number().int().min(1).max(MAX_SAFE_INTEGER);
const nonnegativeSafeIntegerSchema = z
  .number()
  .int()
  .min(0)
  .max(MAX_SAFE_INTEGER);
const uuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/);
const originTraceIdSchema = z
  .string()
  .min(1)
  .max(512)
  .regex(/^[\x20-\x7e]+$/);
const publicErrorCodeSchema = z
  .string()
  .min(1)
  .max(64)
  .regex(/^[A-Z][A-Z0-9_]*$/);
const canonicalUtcTimestampPattern =
  /^([0-9]{4})-([0-9]{2})-([0-9]{2})T([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]{0,5}[1-9]))?Z$/;

const isLeapYear = (year: number): boolean =>
  year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);

const isCanonicalUtcTimestamp = (value: string): boolean => {
  const match = canonicalUtcTimestampPattern.exec(value);
  if (match === null) return false;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  if (year < 1 || month < 1 || month > 12 || day < 1) return false;
  const daysInMonth = [
    31,
    isLeapYear(year) ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return day <= (daysInMonth[month - 1] ?? 0);
};

const canonicalUtcTimestampSchema = z
  .string()
  .refine(
    isCanonicalUtcTimestamp,
    "Workflow timestamps must be canonical RFC3339 UTC text ending in Z",
  );

const timestampSortKey = (value: string): string | null => {
  const match = canonicalUtcTimestampPattern.exec(value);
  if (match === null) return null;
  return `${value.slice(0, 19)}.${(match[7] ?? "").padEnd(6, "0")}`;
};

const isAtOrBefore = (left: string, right: string): boolean => {
  const leftKey = timestampSortKey(left);
  const rightKey = timestampSortKey(right);
  return leftKey !== null && rightKey !== null && leftKey <= rightKey;
};

const agentRunExecutionReferenceV1Schema = z
  .object({
    kind: z.literal("agent_run"),
    run_id: uuidSchema,
  })
  .strict();

const workflowRunExecutionReferenceV1Schema = z
  .object({
    kind: z.literal("workflow_run"),
    workflow_run_id: uuidSchema,
    workflow_epoch: positiveSafeIntegerSchema,
    required_worker_profile_digest: sha256HexSchema.nullable().optional(),
  })
  .strict();

export const workflowExecutionReferenceV1Schema = z.discriminatedUnion("kind", [
  agentRunExecutionReferenceV1Schema,
  workflowRunExecutionReferenceV1Schema,
]);

const workflowPrivateRunAuthorityV1Schema = z
  .object({
    schema_version: z.literal(1),
    run_id: uuidSchema,
    project_id: uuidSchema,
    owner_user_id: uuidSchema,
    workflow_id: uuidSchema,
    workflow_version_id: uuidSchema,
    status: workflowRunStatusV1Schema,
    execution_epoch: positiveSafeIntegerSchema,
    current_job_id: uuidSchema.nullable(),
    retry_of_run_id: uuidSchema.nullable(),
    origin_trace_id: originTraceIdSchema,
    required_worker_profile_digest: sha256HexSchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.retry_of_run_id === value.run_id) {
      context.addIssue({
        code: "custom",
        path: ["retry_of_run_id"],
        message: "a Workflow Run cannot retry itself",
      });
    }
    const active = value.status === "queued" || value.status === "running";
    if (active !== (value.current_job_id !== null)) {
      context.addIssue({
        code: "custom",
        path: ["current_job_id"],
        message:
          "current_job_id must exist only for an active first-wave Workflow Run",
      });
    }
  });

const workflowPrivateJobStatusV1Schema = z.enum([
  "queued",
  "leased",
  "running",
  "retry_wait",
  "succeeded",
  "failed",
  "cancelled",
  "dead",
]);

const workflowRunJobCauseV1Schema = z.enum(["initial", "resume"]);

export const workflowPrivateJobV1Schema = z
  .object({
    schema_version: z.literal(1),
    job_id: uuidSchema,
    job_type: z.literal("workflow_run"),
    project_id: uuidSchema,
    owner_user_id: uuidSchema,
    status: workflowPrivateJobStatusV1Schema,
    cause: workflowRunJobCauseV1Schema,
    attempt_count: nonnegativeSafeIntegerSchema,
    max_attempts: z.number().int().min(1).max(20),
    origin_trace_id: originTraceIdSchema,
    execution_reference: workflowRunExecutionReferenceV1Schema,
    created_at: canonicalUtcTimestampSchema,
    started_at: canonicalUtcTimestampSchema.nullable(),
    completed_at: canonicalUtcTimestampSchema.nullable(),
    public_error_code: publicErrorCodeSchema.nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.attempt_count > value.max_attempts) {
      context.addIssue({
        code: "custom",
        path: ["attempt_count"],
        message: "attempt_count cannot exceed max_attempts",
      });
    }
    if (
      value.status === "queued" &&
      (value.attempt_count !== 0 ||
        value.started_at !== null ||
        value.completed_at !== null ||
        value.public_error_code !== null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["attempt_count"],
        message:
          "queued Workflow Jobs require attempt zero and no attempt outcome",
      });
    }
    const cancelledBeforeStart =
      value.status === "cancelled" &&
      value.attempt_count === 0 &&
      value.started_at === null;
    if (
      value.status !== "queued" &&
      !cancelledBeforeStart &&
      (value.attempt_count < 1 || value.started_at === null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["started_at"],
        message: "non-queued Workflow Jobs require a started attempt",
      });
    }
    const terminal = ["succeeded", "failed", "cancelled", "dead"].includes(
      value.status,
    );
    if (terminal !== (value.completed_at !== null)) {
      context.addIssue({
        code: "custom",
        path: ["completed_at"],
        message: "completed_at must match terminal Workflow Job state",
      });
    }
    const errorRequired = ["retry_wait", "failed", "dead"].includes(
      value.status,
    );
    if (errorRequired !== (value.public_error_code !== null)) {
      context.addIssue({
        code: "custom",
        path: ["public_error_code"],
        message:
          "retrying, failed, and dead Workflow Jobs require a public error code",
      });
    }
    if (
      value.started_at !== null &&
      !isAtOrBefore(value.created_at, value.started_at)
    ) {
      context.addIssue({
        code: "custom",
        path: ["started_at"],
        message: "Workflow Job created_at cannot follow started_at",
      });
    }
    if (value.completed_at !== null) {
      if (value.started_at === null) {
        if (
          !cancelledBeforeStart ||
          !isAtOrBefore(value.created_at, value.completed_at)
        ) {
          context.addIssue({
            code: "custom",
            path: ["completed_at"],
            message:
              "cancel-before-start completion cannot precede Workflow Job creation",
          });
        }
      } else if (!isAtOrBefore(value.started_at, value.completed_at)) {
        context.addIssue({
          code: "custom",
          path: ["completed_at"],
          message: "Workflow Job started_at cannot follow completed_at",
        });
      }
    }
    const epoch = value.execution_reference.workflow_epoch;
    if (
      (value.cause === "initial" && epoch !== 1) ||
      (value.cause === "resume" && epoch < 2)
    ) {
      context.addIssue({
        code: "custom",
        path: ["cause"],
        message: "Workflow Job cause must match its execution epoch",
      });
    }
  });

const workflowRunJobEpochMappingV1Schema = z
  .object({
    schema_version: z.literal(1),
    workflow_run_id: uuidSchema,
    execution_epoch: positiveSafeIntegerSchema,
    job_id: uuidSchema,
    cause: workflowRunJobCauseV1Schema,
    created_at: canonicalUtcTimestampSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.cause === "initial" && value.execution_epoch !== 1) {
      context.addIssue({
        code: "custom",
        path: ["execution_epoch"],
        message: "initial Workflow Job mapping must be epoch 1",
      });
    }
    if (value.cause === "resume" && value.execution_epoch < 2) {
      context.addIssue({
        code: "custom",
        path: ["execution_epoch"],
        message: "resume Workflow Job mapping must use a later epoch",
      });
    }
  });

export const workflowRunJobAuthorityV1Schema = z
  .object({
    schema_version: z.literal(1),
    run: workflowPrivateRunAuthorityV1Schema,
    job: workflowPrivateJobV1Schema,
    mapping: workflowRunJobEpochMappingV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    const { job, mapping, run } = value;
    const reference = job.execution_reference;
    const exact =
      run.run_id === reference.workflow_run_id &&
      run.run_id === mapping.workflow_run_id &&
      run.current_job_id === job.job_id &&
      job.job_id === mapping.job_id &&
      run.execution_epoch === reference.workflow_epoch &&
      run.execution_epoch === mapping.execution_epoch &&
      job.cause === mapping.cause &&
      run.project_id === job.project_id &&
      run.owner_user_id === job.owner_user_id &&
      run.origin_trace_id === job.origin_trace_id &&
      run.required_worker_profile_digest ===
        (reference.required_worker_profile_digest ?? null) &&
      job.created_at === mapping.created_at;
    if (!exact) {
      context.addIssue({
        code: "custom",
        message:
          "Workflow Run, current Job, execution epoch, trace, scope, and profile must match exactly",
      });
    }
  });

export const workflowRunContractFixtureV1Schema = z
  .object({
    schema_version: z.literal(1),
    admission_request: workflowRunAdmissionRequestV1Schema,
    admission_response: workflowRunAdmissionResponseV1Schema,
    owner_private_run: workflowOwnerPrivateRunV1Schema,
    cancelled_before_start_run: workflowOwnerPrivateRunV1Schema,
    cancelled_before_start_job: workflowPrivateJobV1Schema,
    execution_references: z.array(workflowExecutionReferenceV1Schema).length(2),
    authority_bundles: z.array(workflowRunJobAuthorityV1Schema).min(2).max(8),
    compatibility_cases: z
      .array(workflowSchemaCompatibilityCaseV1Schema)
      .min(3)
      .max(16),
    compiler_snapshot_contract: workflowCompilerSnapshotContractV1Schema,
  })
  .strict();
