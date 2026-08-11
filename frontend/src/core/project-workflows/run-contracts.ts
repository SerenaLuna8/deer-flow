import { z } from "zod";

import {
  CanonicalJsonUtf8BudgetExceededError,
  serializeCanonicalJsonValueWithinUtf8Budget,
} from "./canonical";
import {
  safePreviewV1Schema,
  workflowEventSafeErrorV1Schema,
  workflowRunStatusV1Schema,
} from "./transport";
import type { JsonValue } from "./types";
import { containsOnlyUnicodeScalars } from "./validation";

export const WORKFLOW_RUN_INPUT_MAX_DEPTH = 64;
export const WORKFLOW_RUN_INPUT_MAX_NODES = 65_536;
export const WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES = 2_097_152;

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const positiveSafeIntegerSchema = z.number().int().min(1).max(MAX_SAFE_INTEGER);
const uuidSchema = z
  .string()
  .regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
const inputIdSchema = z.string().regex(/^[A-Za-z][A-Za-z0-9_.:-]{0,127}$/);
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
  const fraction = (match[7] ?? "").padEnd(6, "0");
  return `${value.slice(0, 19)}.${fraction}`;
};

const isAtOrBefore = (left: string, right: string): boolean => {
  const leftKey = timestampSortKey(left);
  const rightKey = timestampSortKey(right);
  return leftKey !== null && rightKey !== null && leftKey <= rightKey;
};

const addInputIssue = (
  context: z.RefinementCtx,
  message: string,
  path: Array<string | number> = [],
): void => {
  context.addIssue({ code: "custom", message, path });
};

const validateWorkflowInputs = (
  value: Record<string, unknown>,
  context: z.RefinementCtx,
): void => {
  if (Object.keys(value).length > 256) {
    addInputIssue(context, "Workflow inputs contain too many fields");
    return;
  }

  let valid = true;
  let nodes = 0;
  const stack: Array<{
    value: unknown;
    depth: number;
    path: Array<string | number>;
  }> = [{ value, depth: 0, path: [] }];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current.depth > WORKFLOW_RUN_INPUT_MAX_DEPTH) {
      addInputIssue(
        context,
        "Workflow inputs exceed the maximum JSON nesting depth",
        current.path,
      );
      valid = false;
      break;
    }
    nodes += 1;
    if (nodes > WORKFLOW_RUN_INPUT_MAX_NODES) {
      addInputIssue(
        context,
        "Workflow inputs exceed the maximum JSON node count",
      );
      valid = false;
      break;
    }

    const nested = current.value;
    if (nested === null || typeof nested === "boolean") continue;
    if (typeof nested === "string") {
      if (!containsOnlyUnicodeScalars(nested)) {
        addInputIssue(
          context,
          "Workflow inputs must contain only Unicode scalar values",
          current.path,
        );
        valid = false;
      }
      continue;
    }
    if (typeof nested === "number") {
      if (
        !Number.isFinite(nested) ||
        (Number.isInteger(nested) && !Number.isSafeInteger(nested))
      ) {
        addInputIssue(
          context,
          "Workflow input number is not canonical cross-runtime JSON",
          current.path,
        );
        valid = false;
      }
      continue;
    }
    if (Array.isArray(nested)) {
      for (let index = 0; index < nested.length; index += 1) {
        stack.push({
          value: nested[index],
          depth: current.depth + 1,
          path: [...current.path, index],
        });
      }
      continue;
    }
    if (typeof nested === "object") {
      const prototype = Object.getPrototypeOf(nested);
      if (prototype !== Object.prototype && prototype !== null) {
        addInputIssue(
          context,
          "Workflow inputs must contain only JSON values",
          current.path,
        );
        valid = false;
        continue;
      }
      for (const [key, child] of Object.entries(nested)) {
        if (!containsOnlyUnicodeScalars(key)) {
          addInputIssue(
            context,
            "Workflow inputs must contain only Unicode scalar values",
            [...current.path, key],
          );
          valid = false;
        }
        stack.push({
          value: child,
          depth: current.depth + 1,
          path: [...current.path, key],
        });
      }
      continue;
    }
    addInputIssue(
      context,
      "Workflow inputs must contain only JSON values",
      current.path,
    );
    valid = false;
  }

  if (!valid) return;
  try {
    serializeCanonicalJsonValueWithinUtf8Budget(
      value as JsonValue,
      WORKFLOW_RUN_INPUT_MAX_CANONICAL_BYTES,
    );
  } catch (error) {
    if (error instanceof CanonicalJsonUtf8BudgetExceededError) {
      addInputIssue(
        context,
        "Workflow inputs exceed the maximum canonical UTF-8 byte count",
      );
      return;
    }
    addInputIssue(context, "Workflow inputs must be portable canonical JSON");
  }
};

const workflowInputsSchema = z
  .record(inputIdSchema, z.unknown())
  .superRefine(validateWorkflowInputs)
  .transform((value) => value as Record<string, JsonValue>);

export const workflowRunAdmissionRequestV1Schema = z
  .object({
    workflow_version_id: uuidSchema.nullable(),
    inputs: workflowInputsSchema,
  })
  .strict();

const relativeWorkflowStreamUrlSchema = z
  .string()
  .min(1)
  .max(512)
  .regex(
    /^\/api\/projects\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/workflow-runs\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\/stream$/,
  );

export const workflowRunAdmissionResponseV1Schema = z
  .object({
    schema_version: z.literal(1),
    run_id: uuidSchema,
    status: z.literal("queued"),
    stream_url: relativeWorkflowStreamUrlSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (!value.stream_url.includes(`/workflow-runs/${value.run_id}/stream`)) {
      context.addIssue({
        code: "custom",
        path: ["stream_url"],
        message: "stream URL must identify the admitted Workflow Run",
      });
    }
  });

export const workflowOwnerPrivateRunV1Schema = z
  .object({
    schema_version: z.literal(1),
    run_id: uuidSchema,
    workflow_id: uuidSchema,
    workflow_version_id: uuidSchema,
    status: workflowRunStatusV1Schema,
    execution_epoch: positiveSafeIntegerSchema,
    retry_of_run_id: uuidSchema.nullable(),
    created_at: canonicalUtcTimestampSchema,
    started_at: canonicalUtcTimestampSchema.nullable(),
    completed_at: canonicalUtcTimestampSchema.nullable(),
    input_preview: safePreviewV1Schema.nullable(),
    output_preview: safePreviewV1Schema.nullable(),
    error: workflowEventSafeErrorV1Schema.nullable(),
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
    const terminalAfterStart = [
      "succeeded",
      "failed",
      "side_effect_unknown",
    ].includes(value.status);
    if (
      value.status === "queued" &&
      (value.started_at !== null || value.completed_at !== null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["started_at"],
        message:
          "a queued Workflow Run cannot have timestamps beyond created_at",
      });
    }
    if (
      value.status === "running" &&
      (value.started_at === null || value.completed_at !== null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["started_at"],
        message: "a running Workflow Run requires only started_at",
      });
    }
    if (
      terminalAfterStart &&
      (value.started_at === null || value.completed_at === null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["completed_at"],
        message: "terminal Workflow Runs require started_at and completed_at",
      });
    }
    if (value.status === "cancelled" && value.completed_at === null) {
      context.addIssue({
        code: "custom",
        path: ["completed_at"],
        message: "cancelled Workflow Runs require completed_at",
      });
    }
    const errorRequired =
      value.status === "failed" || value.status === "side_effect_unknown";
    if (errorRequired !== (value.error !== null)) {
      context.addIssue({
        code: "custom",
        path: ["error"],
        message:
          "failed and side-effect-unknown Workflow Runs require a safe error projection",
      });
    }
    if (
      value.started_at !== null &&
      !isAtOrBefore(value.created_at, value.started_at)
    ) {
      context.addIssue({
        code: "custom",
        path: ["started_at"],
        message: "Workflow Run created_at cannot follow started_at",
      });
    }
    if (value.completed_at !== null) {
      if (value.started_at === null) {
        if (
          value.status !== "cancelled" ||
          !isAtOrBefore(value.created_at, value.completed_at)
        ) {
          context.addIssue({
            code: "custom",
            path: ["completed_at"],
            message:
              "cancel-before-start completion cannot precede Workflow Run creation",
          });
        }
      } else if (!isAtOrBefore(value.started_at, value.completed_at)) {
        context.addIssue({
          code: "custom",
          path: ["completed_at"],
          message: "Workflow Run started_at cannot follow completed_at",
        });
      }
    }
  });

export type WorkflowRunAdmissionRequestV1 = z.infer<
  typeof workflowRunAdmissionRequestV1Schema
>;
export type WorkflowRunAdmissionResponseV1 = z.infer<
  typeof workflowRunAdmissionResponseV1Schema
>;
export type WorkflowOwnerPrivateRunV1 = z.infer<
  typeof workflowOwnerPrivateRunV1Schema
>;
