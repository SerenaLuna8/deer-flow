import { z } from "zod";

// Local auth-disabled mode uses the canonical synthetic account solely as
// client cache identity; privacy authority is always derived by the server.
export const privacyAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);
export const privacyProjectIdSchema = z.string().uuid();

export const privacyCaseSchema = z
  .object({
    project_id: privacyProjectIdSchema,
    project_slug: z.string().min(1),
    project_display_name: z.string().min(1),
    project_icon: z.string().min(1),
    membership_status: z.enum(["left", "removed"]),
    retention_kind: z.enum(["former_owner", "project"]),
    deletion_deadline: z.string().datetime({ offset: true }),
    early_delete_requested: z.boolean(),
  })
  .strict();

export const privacyCaseListSchema = z.array(privacyCaseSchema);

export const privacyEarlyDeleteResponseSchema = z
  .object({
    project_id: privacyProjectIdSchema,
    job_id: z.string().uuid(),
    status: z.enum(["queued", "leased", "running", "retry_wait"]),
  })
  .strict();

export type PrivacyCase = z.infer<typeof privacyCaseSchema>;
export type PrivacyEarlyDeleteResponse = z.infer<
  typeof privacyEarlyDeleteResponseSchema
>;
