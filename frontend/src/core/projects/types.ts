import { z } from "zod";

export const PROJECT_ROLES = ["admin", "editor", "runner", "viewer"] as const;

export const CAPABILITIES = [
  "project.read",
  "project.update",
  "project.enter",
  "project.pin",
  "project.members.manage",
  "shared_assets.read",
  "shared_assets.execute",
  "shared_assets.edit",
  "mcp.credentials.approve",
  "private_work.create",
  "private_work.read_own",
  "automation.manage_own",
  "project.audit.read",
  "project.usage.read",
  "project.lifecycle.manage",
] as const;

export const PROJECT_ERROR_CODES = [
  "PROJECT_NOT_FOUND",
  "PROJECT_FORBIDDEN",
  "PROJECT_SLUG_CONFLICT",
  "PROJECT_VALIDATION_FAILED",
  "DATABASE_UNAVAILABLE",
  "AUTH_REQUIRED",
  "PROJECT_NETWORK_ERROR",
  "PROJECT_RESPONSE_INVALID",
  "PROJECT_ERROR_RESPONSE_INVALID",
] as const;

export const projectRoleSchema = z.enum(PROJECT_ROLES);
export const capabilitySchema = z.enum(CAPABILITIES);
export const projectErrorCodeSchema = z.enum(PROJECT_ERROR_CODES);
export const projectIdSchema = z.string().uuid();

export const projectSchema = z
  .object({
    id: projectIdSchema,
    slug: z.string().min(1),
    display_name: z.string().min(1),
    description: z.string(),
    icon: z.string().min(1),
    role: projectRoleSchema,
    capabilities: z.array(capabilitySchema),
    is_pinned: z.boolean(),
    last_entered_at: z.string().datetime({ offset: true }).nullable(),
    member_count: z.number().int().nonnegative(),
    agent_count: z.number().int().nonnegative(),
    skill_count: z.number().int().nonnegative(),
    mcp_count: z.number().int().nonnegative(),
    status: z.literal("active"),
    is_suspended: z.boolean(),
    membership_version: z.number().int().positive(),
    request_id: z.string().min(1),
  })
  .strict();

export const projectPageSchema = z
  .object({
    items: z.array(projectSchema),
    next_cursor: z.string().min(1).nullable(),
  })
  .strict();

export const createProjectSchema = z
  .object({
    slug: z.string().min(1),
    display_name: z.string().min(1).max(120),
    description: z.string().max(500).optional(),
    icon: z.string().min(1).max(32).optional(),
  })
  .strict();

export const patchProjectSchema = z
  .object({
    display_name: z.string().min(1).max(120).optional(),
    description: z.string().max(500).optional(),
    icon: z.string().min(1).max(32).optional(),
  })
  .strict()
  .refine(
    (value) => Object.keys(value).length > 0,
    "Project changes are required",
  );

export const pinProjectSchema = z.object({ pinned: z.boolean() }).strict();

export const projectFiltersSchema = z
  .object({
    query: z.string().optional(),
    pinned: z.boolean().optional(),
    cursor: z.string().min(1).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  })
  .strict();

export type ProjectRole = z.infer<typeof projectRoleSchema>;
export type Capability = z.infer<typeof capabilitySchema>;
export type ProjectErrorCode = z.infer<typeof projectErrorCodeSchema>;
export type Project = z.infer<typeof projectSchema>;
export type ProjectPage = z.infer<typeof projectPageSchema>;
export type CreateProjectInput = z.input<typeof createProjectSchema>;
export type PatchProjectInput = z.input<typeof patchProjectSchema>;
export type PinProjectInput = z.input<typeof pinProjectSchema>;
export type ProjectFilters = z.input<typeof projectFiltersSchema>;
