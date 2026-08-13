import { z } from "zod";

import { auditItemSchema } from "@/core/project-governance/audit";

export const accountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

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
    slug: z
      .string()
      .min(3)
      .max(63)
      .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/u),
    display_name: z.string().min(1).max(120),
    status: z.enum(["active", "pending_deletion"]),
    is_suspended: z.boolean(),
    state_version: z.number().int().positive(),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
    deletion_effective_at: z.string().datetime({ offset: true }).nullable(),
  })
  .strict();

export const adminProjectPageSchema = z
  .object({
    items: z.array(adminProjectSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

const publicErrorCodeSchema = z.string().regex(/^[A-Z][A-Z0-9_]{0,63}$/u);

export const ADMIN_JOB_TYPES = [
  "private_run",
  "automation_run",
  "retention_purge",
  "mcp_discovery",
  "memory_dream",
  "memory_dream_prepare",
  "memory_seal",
] as const;

export const ADMIN_OPERATIONS_PAGE_SIZES = [10, 20, 50, 100] as const;
export const DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE = 20;
export const ADMIN_JOB_PAGE_SIZES = ADMIN_OPERATIONS_PAGE_SIZES;
export const DEFAULT_ADMIN_JOB_PAGE_SIZE = DEFAULT_ADMIN_OPERATIONS_PAGE_SIZE;

export const adminJobTypeSchema = z.enum(ADMIN_JOB_TYPES);
export const adminOperationsPageSizeSchema = z.union([
  z.literal(10),
  z.literal(20),
  z.literal(50),
  z.literal(100),
]);
export const adminJobPageSizeSchema = adminOperationsPageSizeSchema;

export const adminJobSchema = z
  .object({
    job_id: z.string().uuid(),
    dead_job_id: z.string().uuid().nullable(),
    project_id: z.string().uuid(),
    project_slug: z
      .string()
      .min(3)
      .max(63)
      .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/u),
    project_display_name: z.string().min(1).max(120),
    job_type: adminJobTypeSchema,
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

export type AdminAuditItem = z.infer<typeof auditItemSchema> & {
  actor_user_id: string | null;
  actor_email: string | null;
  project_id: string | null;
  project_slug: string | null;
  project_display_name: string | null;
};

export const adminAuditItemSchema = z
  .object({
    actor_user_id: z.string().uuid().nullable(),
    actor_email: z.string().email().max(320).nullable(),
    project_id: z.string().uuid().nullable(),
    project_slug: z
      .string()
      .min(3)
      .max(63)
      .regex(/^[a-z0-9]+(?:-[a-z0-9]+)*$/u)
      .nullable(),
    project_display_name: z.string().min(1).max(120).nullable(),
  })
  .passthrough()
  .superRefine((item, context) => {
    const {
      actor_user_id: _actorUserId,
      actor_email: _actorEmail,
      project_id: _projectId,
      project_slug: _projectSlug,
      project_display_name: _projectDisplayName,
      ...base
    } = item;
    const baseResult = auditItemSchema.safeParse(base);
    if (!baseResult.success) {
      for (const issue of baseResult.error.issues) {
        context.addIssue({
          code: "custom",
          path: issue.path,
          message: issue.message,
        });
      }
    }
    if (item.actor_email !== null && item.actor_user_id === null) {
      context.addIssue({
        code: "custom",
        path: ["actor_email"],
        message: "actor_email requires actor_user_id",
      });
    }
    const hasProject = item.project_id !== null;
    if (
      hasProject !== (item.project_slug !== null) ||
      hasProject !== (item.project_display_name !== null)
    ) {
      context.addIssue({
        code: "custom",
        path: ["project_id"],
        message: "Project fields must be present together",
      });
    }
  })
  .transform((item): AdminAuditItem => {
    const {
      actor_user_id,
      actor_email,
      project_id,
      project_slug,
      project_display_name,
      ...base
    } = item;
    return {
      ...auditItemSchema.parse(base),
      actor_user_id,
      actor_email,
      project_id,
      project_slug,
      project_display_name,
    };
  }) as unknown as z.ZodType<AdminAuditItem>;

export const adminAuditPageSchema = z
  .object({
    items: z.array(adminAuditItemSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

export const projectFiltersSchema = z
  .object({
    query: z.string().min(1).max(120).optional(),
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
    type: adminJobTypeSchema.optional(),
  })
  .strict();

export const ADMIN_AUDIT_PLATFORM_FILTER = "__platform__";

export const auditFiltersSchema = z
  .object({
    project_id: z.string().uuid().optional(),
    platform_only: z.literal(true).optional(),
  })
  .strict()
  .superRefine((filters, context) => {
    if (filters.platform_only && filters.project_id) {
      context.addIssue({
        code: "custom",
        path: ["platform_only"],
        message: "platform_only cannot combine with project_id",
      });
    }
  });

export const adminProjectsPageSizeSchema = z.number().int().min(1).max(100);

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

export type AdminAuditFilters = z.input<typeof auditFiltersSchema>;
export type AdminAuditPage = z.infer<typeof adminAuditPageSchema>;
export type AdminJob = z.infer<typeof adminJobSchema>;
export type AdminJobFilters = z.input<typeof jobFiltersSchema>;
export type AdminJobPage = z.infer<typeof adminJobPageSchema>;
export type AdminJobPageSize = z.infer<typeof adminJobPageSizeSchema>;
export type AdminOperationsPageSize = z.infer<
  typeof adminOperationsPageSizeSchema
>;
export type AdminProjectFilters = z.input<typeof projectFiltersSchema>;
export type AdminProjectPage = z.infer<typeof adminProjectPageSchema>;
export type OperationsOverviewData = z.infer<typeof operationsOverviewSchema>;
export type SafeRequeueInput = z.input<typeof safeRequeueInputSchema>;
export type SafeRequeueResponse = z.infer<typeof safeRequeueResponseSchema>;
