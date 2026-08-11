import { z } from "zod";

import {
  CanonicalJsonUtf8BudgetExceededError,
  serializeCanonicalJsonValueWithinUtf8Budget,
} from "@/core/project-workflows/canonical";
import { workflowEventSafeErrorV1Schema } from "@/core/project-workflows/transport";
import {
  jsonValueSchema,
  type JsonValue,
} from "@/core/project-workflows/types";
import {
  utf8ByteBoundedString,
  utf8ByteLength,
} from "@/core/project-workflows/validation";

const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const MAX_HTTP_SETTLED_BODY_BYTES = 2_097_152;
const MAX_HTTP_SETTLED_HEADER_BYTES = 65_536;
const MAX_HTTP_JSON_DEPTH = 64;
const MAX_HTTP_JSON_NODES = 65_536;

const nonnegativeSafeIntegerSchema = z
  .number()
  .int()
  .min(0)
  .max(MAX_SAFE_INTEGER);

const httpHeaderNameSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9!#$%&'*+.^_`|~-]+$/);

const retainedHttpJsonByteCount = (value: JsonValue): number => {
  let nodes = 0;
  const stack: Array<{ value: JsonValue; depth: number }> = [
    { value, depth: 1 },
  ];
  while (stack.length > 0) {
    const current = stack.pop()!;
    if (current.depth > MAX_HTTP_JSON_DEPTH) {
      throw new Error("settled HTTP JSON exceeds the maximum depth");
    }
    nodes += 1;
    if (nodes > MAX_HTTP_JSON_NODES) {
      throw new Error("settled HTTP JSON exceeds the maximum node count");
    }
    if (Array.isArray(current.value)) {
      for (const item of current.value) {
        stack.push({ value: item, depth: current.depth + 1 });
      }
    } else if (current.value !== null && typeof current.value === "object") {
      for (const [key, item] of Object.entries(current.value)) {
        nodes += 1;
        if (nodes > MAX_HTTP_JSON_NODES) {
          throw new Error("settled HTTP JSON exceeds the maximum node count");
        }
        if (utf8ByteLength(key) > MAX_HTTP_SETTLED_BODY_BYTES) {
          throw new Error("settled HTTP JSON key exceeds the byte limit");
        }
        stack.push({ value: item, depth: current.depth + 1 });
      }
    } else if (
      typeof current.value === "string" &&
      utf8ByteLength(current.value) > MAX_HTTP_SETTLED_BODY_BYTES
    ) {
      throw new Error("settled HTTP JSON string exceeds the byte limit");
    }
  }
  try {
    return serializeCanonicalJsonValueWithinUtf8Budget(
      value,
      MAX_HTTP_SETTLED_BODY_BYTES,
    ).utf8Bytes;
  } catch (error) {
    if (error instanceof CanonicalJsonUtf8BudgetExceededError) {
      throw new Error("settled HTTP JSON exceeds the persisted byte limit", {
        cause: error,
      });
    }
    throw error;
  }
};

export const workflowHttpHeaderV1Schema = z
  .object({
    name: httpHeaderNameSchema.refine(
      (value) =>
        ![
          "authorization",
          "proxy-authenticate",
          "proxy-authorization",
          "set-cookie",
          "www-authenticate",
          "location",
        ].includes(value),
      "sensitive and redirect response headers cannot be persisted",
    ),
    value: utf8ByteBoundedString(0, 4_096),
  })
  .strict();

const workflowHttpEmptyBodyV1Schema = z
  .object({ kind: z.literal("empty") })
  .strict();
const workflowHttpTextBodyV1Schema = z
  .object({
    kind: z.literal("text"),
    text: utf8ByteBoundedString(0, MAX_HTTP_SETTLED_BODY_BYTES),
  })
  .strict();
const workflowHttpJsonBodyV1Schema = z
  .object({ kind: z.literal("json"), value: jsonValueSchema })
  .strict()
  .superRefine((value, context) => {
    try {
      retainedHttpJsonByteCount(value.value);
    } catch (error) {
      context.addIssue({
        code: "custom",
        path: ["value"],
        message:
          error instanceof Error ? error.message : "invalid settled HTTP JSON",
      });
    }
  });

export const workflowHttpBodyV1Schema = z.union([
  workflowHttpEmptyBodyV1Schema,
  workflowHttpTextBodyV1Schema,
  workflowHttpJsonBodyV1Schema,
]);

export const workflowHttpObservedByteCountV1Schema = z
  .object({
    value: z.number().int().min(0).max(MAX_HTTP_SETTLED_BODY_BYTES),
    relation: z.enum(["exact", "at_least"]),
  })
  .strict();

const actualRetainedHttpBodyBytes = (
  body: z.infer<typeof workflowHttpBodyV1Schema>,
) => {
  if (body.kind === "empty") return 0;
  if (body.kind === "text") return utf8ByteLength(body.text);
  return retainedHttpJsonByteCount(body.value);
};

export const workflowHttpResponseV1Schema = z
  .object({
    status_code: z.number().int().min(100).max(599),
    headers: z.array(workflowHttpHeaderV1Schema).max(64),
    body: workflowHttpBodyV1Schema,
    duration_ms: nonnegativeSafeIntegerSchema,
    wire_byte_count: workflowHttpObservedByteCountV1Schema,
    decoded_byte_count: workflowHttpObservedByteCountV1Schema,
    retained_body_byte_count: z
      .number()
      .int()
      .min(0)
      .max(MAX_HTTP_SETTLED_BODY_BYTES),
  })
  .strict()
  .superRefine((value, context) => {
    const names = value.headers.map((header) => header.name);
    if (new Set(names).size !== names.length) {
      context.addIssue({
        code: "custom",
        path: ["headers"],
        message: "settled HTTP response headers must be unique",
      });
    }
    const headerBytes = value.headers.reduce(
      (total, header) =>
        total + utf8ByteLength(header.name) + utf8ByteLength(header.value),
      0,
    );
    if (headerBytes > MAX_HTTP_SETTLED_HEADER_BYTES) {
      context.addIssue({
        code: "custom",
        path: ["headers"],
        message:
          "settled HTTP response headers exceed the persisted byte limit",
      });
    }
    if (
      value.wire_byte_count.relation !== "exact" ||
      value.decoded_byte_count.relation !== "exact"
    ) {
      context.addIssue({
        code: "custom",
        message:
          "settled HTTP responses require exact wire and decoded byte counts",
      });
    }
    try {
      if (
        value.retained_body_byte_count !==
        actualRetainedHttpBodyBytes(value.body)
      ) {
        context.addIssue({
          code: "custom",
          path: ["retained_body_byte_count"],
          message:
            "retained body byte count must match canonical persisted response material",
        });
      }
    } catch (error) {
      context.addIssue({
        code: "custom",
        path: ["body"],
        message:
          error instanceof Error ? error.message : "invalid retained HTTP body",
      });
    }
  });

const workflowHttpSuccessOutcomeV1Schema = z
  .object({
    kind: z.literal("success"),
    response: workflowHttpResponseV1Schema,
  })
  .strict();
const workflowHttpErrorOutcomeV1Schema = z
  .object({
    kind: z.literal("http_error"),
    response: workflowHttpResponseV1Schema,
  })
  .strict();
const workflowHttpResponseInvalidOutcomeV1Schema = z
  .object({
    kind: z.literal("response_invalid"),
    status_code: z.number().int().min(100).max(599),
    duration_ms: nonnegativeSafeIntegerSchema,
    wire_byte_count: workflowHttpObservedByteCountV1Schema,
    decoded_byte_count: workflowHttpObservedByteCountV1Schema,
    error: workflowEventSafeErrorV1Schema,
  })
  .strict()
  .superRefine((value, context) => {
    if (
      ![
        "WORKFLOW_HTTP_RESPONSE_LIMIT",
        "WORKFLOW_HTTP_RESPONSE_INVALID",
      ].includes(value.error.code)
    ) {
      context.addIssue({
        code: "custom",
        path: ["error", "code"],
        message:
          "response_invalid requires a stable HTTP response validation error",
      });
    }
    if (
      value.error.code === "WORKFLOW_HTTP_RESPONSE_LIMIT" &&
      value.wire_byte_count.relation !== "at_least" &&
      value.decoded_byte_count.relation !== "at_least"
    ) {
      context.addIssue({
        code: "custom",
        message:
          "response-limit outcomes require at least one capped byte observation",
      });
    }
  });

export const workflowHttpSettledOutcomeV1Schema = z.union([
  workflowHttpSuccessOutcomeV1Schema,
  workflowHttpErrorOutcomeV1Schema,
  workflowHttpResponseInvalidOutcomeV1Schema,
]);

export type WorkflowHttpObservedByteCountV1 = z.infer<
  typeof workflowHttpObservedByteCountV1Schema
>;
export type WorkflowHttpResponseV1 = z.infer<
  typeof workflowHttpResponseV1Schema
>;
export type WorkflowHttpSettledOutcomeV1 = z.infer<
  typeof workflowHttpSettledOutcomeV1Schema
>;
