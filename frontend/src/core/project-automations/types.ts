import { z } from "zod";

export const automationStatusSchema = z.enum([
  "enabled",
  "paused",
  "completed",
  "failed",
  "cancelled",
]);

export const automationRunStatusSchema = z.enum([
  "queued",
  "launching",
  "running",
  "success",
  "failed",
  "skipped",
  "interrupted",
  "cancelled",
  "rejected",
]);

export const automationSchema = z
  .object({
    id: z.string().min(1),
    thread_id: z.string().uuid().nullable(),
    context_mode: z.enum(["fresh_thread_per_run", "reuse_thread"]),
    agent_asset_id: z.string().uuid(),
    agent_scope: z.enum(["project", "system"]),
    title: z.string().min(1),
    prompt: z.string(),
    schedule_type: z.enum(["once", "cron"]),
    schedule_spec: z.record(z.string(), z.unknown()),
    timezone: z.string().min(1),
    status: automationStatusSchema,
    next_run_at: z.string().datetime({ offset: true }).nullable(),
    last_run_at: z.string().datetime({ offset: true }).nullable(),
    last_outcome: z.string().nullable(),
    last_error_code: z.string().nullable(),
    run_count: z.number().int().nonnegative(),
    version: z.number().int().positive(),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const automationListSchema = z
  .object({ items: z.array(automationSchema) })
  .strict();

export const automationRunSchema = z
  .object({
    id: z.string().min(1),
    automation_id: z.string().min(1),
    automation_version: z.number().int().positive(),
    scheduled_for: z.string().datetime({ offset: true }),
    trigger: z.enum(["scheduled", "manual"]),
    status: automationRunStatusSchema,
    thread_id: z.string().nullable(),
    run_id: z.string().nullable(),
    error_code: z.string().nullable(),
    started_at: z.string().datetime({ offset: true }).nullable(),
    finished_at: z.string().datetime({ offset: true }).nullable(),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const automationRunListSchema = z
  .object({ items: z.array(automationRunSchema) })
  .strict();

export const automationReadinessSchema = z
  .object({
    status: z.enum(["ready", "unavailable"]),
    code: z.string().min(1),
    scheduler_enabled: z.boolean(),
    scheduler_status: z.enum([
      "disabled",
      "stopped",
      "running",
      "ownership_lost",
    ]),
    project_private_work_ready: z.boolean(),
    schema_ready: z.boolean(),
    request_id: z.string().min(1),
  })
  .strict();

export const createAutomationInputSchema = z
  .object({
    title: z.string().min(1).max(255),
    prompt: z.string().min(1),
    context_mode: z.enum(["fresh_thread_per_run", "reuse_thread"]),
    thread_id: z.string().uuid().nullable().optional(),
    agent_asset_id: z.string().uuid(),
    agent_scope: z.enum(["project", "system"]),
    schedule_type: z.enum(["once", "cron"]),
    schedule_spec: z.record(z.string(), z.unknown()),
    timezone: z.string().min(1).max(64),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.context_mode === "fresh_thread_per_run" &&
      value.thread_id != null
    ) {
      context.addIssue({
        code: "custom",
        message: "fresh_thread_per_run does not accept thread_id",
        path: ["thread_id"],
      });
    }
    if (value.context_mode === "reuse_thread" && !value.thread_id) {
      context.addIssue({
        code: "custom",
        message: "reuse_thread requires thread_id",
        path: ["thread_id"],
      });
    }
  });

export const updateAutomationInputSchema = z
  .object({
    expected_version: z.number().int().positive(),
    title: z.string().min(1).max(255).optional(),
    prompt: z.string().min(1).optional(),
    schedule_spec: z.record(z.string(), z.unknown()).optional(),
    timezone: z.string().min(1).max(64).optional(),
  })
  .strict()
  .refine(
    (value) =>
      value.title !== undefined ||
      value.prompt !== undefined ||
      value.schedule_spec !== undefined ||
      value.timezone !== undefined,
    "Automation changes are required",
  );

export const automationVersionInputSchema = z
  .object({ expected_version: z.number().int().positive() })
  .strict();

export const automationDeleteSchema = z
  .object({ id: z.string().min(1), deleted: z.boolean() })
  .strict();

export const automationListFiltersSchema = z
  .object({
    limit: z.number().int().min(1).max(100).default(50),
    offset: z.number().int().nonnegative().default(0),
  })
  .strict();

export const automationIdSchema = z.string().min(1);
export const automationIdempotencyKeySchema = z.string().uuid();

export type Automation = z.infer<typeof automationSchema>;
export type AutomationList = z.infer<typeof automationListSchema>;
export type AutomationRun = z.infer<typeof automationRunSchema>;
export type AutomationRunList = z.infer<typeof automationRunListSchema>;
export type AutomationReadiness = z.infer<typeof automationReadinessSchema>;
export type CreateAutomationInput = z.input<typeof createAutomationInputSchema>;
export type UpdateAutomationInput = z.input<typeof updateAutomationInputSchema>;
export type AutomationListFilters = z.input<typeof automationListFiltersSchema>;
export type AutomationDelete = z.infer<typeof automationDeleteSchema>;
