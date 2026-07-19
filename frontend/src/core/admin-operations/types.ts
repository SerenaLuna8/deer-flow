import { z } from "zod";

import { auditItemSchema } from "@/core/project-governance/audit";

export const accountIdSchema = z.string().uuid();

const usageItemSchema = z.discriminatedUnion("dimension", [
  z
    .object({
      dimension: z.literal("members"),
      used: z.number().int().nonnegative(),
      reserved: z.number().int().nonnegative(),
    })
    .strict(),
  z
    .object({
      dimension: z.literal("storage_bytes"),
      used: z.number().int().nonnegative(),
      reserved: z.number().int().nonnegative(),
    })
    .strict(),
  z
    .object({
      dimension: z.literal("concurrent_runs"),
      used: z.number().int().nonnegative(),
      reserved: z.number().int().nonnegative(),
    })
    .strict(),
  z
    .object({
      dimension: z.literal("mcp_calls_daily"),
      used: z.number().int().nonnegative(),
      reserved: z.number().int().nonnegative(),
    })
    .strict(),
]);

const operationsReadinessSchema = z
  .object({
    status: z.enum(["ready", "degraded", "closed"]),
    database: z.string().min(1),
    schema: z.string().min(1),
    schema_state: z.enum(["ready", "unavailable"]),
    worker_fleet: z.string().min(1),
    scheduler: z.string().min(1),
    stream: z.string().min(1),
    recovery: z.string().min(1),
    quota: z.string().min(1),
    audit: z.string().min(1),
    role: z.enum(["gateway", "worker", "scheduler"]),
    worker_count: z.number().int().nonnegative(),
    worker_capacity: z.number().int().nonnegative(),
    worker_oldest_heartbeat_age_seconds: z
      .number()
      .int()
      .nonnegative()
      .nullable(),
    scheduler_ownership: z.string().min(1),
  })
  .strict();

const operationsCountsSchema = z
  .object({
    projects: z.number().int().nonnegative(),
    suspended_projects: z.number().int().nonnegative(),
    queued_jobs: z.number().int().nonnegative(),
    running_jobs: z.number().int().nonnegative(),
    dead_jobs: z.number().int().nonnegative(),
  })
  .strict();

const aggregateUsageSchema = z
  .array(usageItemSchema)
  .length(4)
  .superRefine((items, context) => {
    if (new Set(items.map((item) => item.dimension)).size !== 4) {
      context.addIssue({
        code: "custom",
        message: "Every aggregate usage dimension is required once",
      });
    }
  });

const channelProviderHealthSchema = z
  .object({
    provider: z.string().min(1),
    status: z.enum(["ready", "degraded", "unavailable"]),
    checked_at: z.string().datetime({ offset: true }),
    code: z.enum(["CHANNEL_READY", "CHANNEL_STOPPED", "CHANNEL_DISABLED"]),
  })
  .strict();

export const operationsOverviewSchema = z.discriminatedUnion("data_status", [
  z
    .object({
      readiness: operationsReadinessSchema.refine(
        (readiness) => readiness.status !== "closed",
        "Available aggregates require open readiness",
      ),
      data_status: z.literal("available"),
      counts: operationsCountsSchema,
      usage: aggregateUsageSchema,
      channel_providers: z.array(channelProviderHealthSchema),
    })
    .strict(),
  z
    .object({
      readiness: operationsReadinessSchema.refine(
        (readiness) => readiness.status === "closed",
        "Unavailable aggregates require closed readiness",
      ),
      data_status: z.literal("unavailable"),
      counts: z.null(),
      usage: z.null(),
      channel_providers: z.array(channelProviderHealthSchema),
    })
    .strict(),
]);

export const adminProjectSchema = z
  .object({
    project_id: z.string().uuid(),
    status: z.enum(["active", "pending_deletion"]),
    is_suspended: z.boolean(),
  })
  .strict();

export const adminProjectPageSchema = z
  .object({
    items: z.array(adminProjectSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

const publicErrorCodeSchema = z.string().regex(/^[A-Z][A-Z0-9_]{0,63}$/u);

export const adminJobSchema = z
  .object({
    job_id: z.string().uuid(),
    dead_job_id: z.string().uuid().nullable(),
    project_id: z.string().uuid(),
    job_type: z.enum(["private_run", "automation_run", "retention_purge"]),
    status: z.enum([
      "queued",
      "leased",
      "running",
      "retry_wait",
      "succeeded",
      "failed",
      "cancelled",
      "dead",
    ]),
    retry_safety: z.enum(["safe", "unknown", "unsafe"]),
    safe_to_requeue: z.boolean(),
    public_error_code: publicErrorCodeSchema.nullable(),
    predecessor_dead_job_id: z.string().uuid().nullable(),
  })
  .strict()
  .superRefine((item, context) => {
    const safe =
      item.status === "dead" &&
      item.dead_job_id !== null &&
      item.retry_safety === "safe" &&
      item.job_type === "retention_purge";
    if (item.safe_to_requeue && !safe) {
      context.addIssue({
        code: "custom",
        path: ["safe_to_requeue"],
        message: "Safe requeue eligibility is inconsistent",
      });
    }
  });

export const adminJobPageSchema = z
  .object({
    items: z.array(adminJobSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

export const adminAuditPageSchema = z
  .object({
    items: z.array(auditItemSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

export const projectFiltersSchema = z
  .object({
    status: z.enum(["active", "pending_deletion"]).optional(),
    suspended: z.boolean().optional(),
  })
  .strict();

export const jobFiltersSchema = z
  .object({
    project_id: z.string().uuid().optional(),
    status: z
      .enum([
        "queued",
        "leased",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "cancelled",
        "dead",
      ])
      .optional(),
    type: z
      .enum(["private_run", "automation_run", "retention_purge"])
      .optional(),
  })
  .strict();

export const safeRequeueInputSchema = z
  .object({
    project_id: z.string().uuid(),
    dead_job_id: z.string().uuid(),
    idempotency_key: z.string().regex(/^[0-9a-f]{64}$/u),
    max_attempts: z.number().int().min(1).max(20),
  })
  .strict();

export const safeRequeueResponseSchema = z
  .object({
    job_id: z.string().uuid(),
    project_id: z.string().uuid(),
    status: z.literal("queued"),
    retry_safety: z.literal("safe"),
    attempt_count: z.literal(0),
    predecessor_dead_job_id: z.string().uuid(),
  })
  .strict();

export const operationsServerErrorSchema = z
  .object({
    code: z.enum([
      "INVALID_STREAM_CURSOR",
      "RELIABILITY_NOT_FOUND",
      "RELIABILITY_CONFLICT",
      "RELIABILITY_INVALID",
      "DATABASE_UNAVAILABLE",
    ]),
    message: z.string().min(1),
    request_id: z.string().min(1),
  })
  .strict();

export type AdminAuditPage = z.infer<typeof adminAuditPageSchema>;
export type AdminJob = z.infer<typeof adminJobSchema>;
export type AdminJobFilters = z.input<typeof jobFiltersSchema>;
export type AdminJobPage = z.infer<typeof adminJobPageSchema>;
export type AdminProjectFilters = z.input<typeof projectFiltersSchema>;
export type AdminProjectPage = z.infer<typeof adminProjectPageSchema>;
export type OperationsOverviewData = z.infer<typeof operationsOverviewSchema>;
export type SafeRequeueInput = z.input<typeof safeRequeueInputSchema>;
export type SafeRequeueResponse = z.infer<typeof safeRequeueResponseSchema>;
