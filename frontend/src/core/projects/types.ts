import { z } from "zod";

export const PROJECT_ROLES = ["admin", "editor", "runner", "viewer"] as const;
export const INVITABLE_PROJECT_ROLES = ["editor", "runner", "viewer"] as const;

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
  "PROJECT_OR_MEMBER_NOT_FOUND",
  "PROJECT_MEMBERSHIP_FORBIDDEN",
  "PROJECT_LAST_ADMIN",
  "PROJECT_MEMBERSHIP_VERSION_CONFLICT",
  "PROJECT_INVITATION_CONFLICT",
  "PROJECT_INVITATION_INVALID",
  "PROJECT_DELETION_STATE_CONFLICT",
  "PROJECT_SLUG_CONFLICT",
  "PROJECT_VALIDATION_FAILED",
  "DATABASE_UNAVAILABLE",
  "AUTH_REQUIRED",
  "PROJECT_NETWORK_ERROR",
  "PROJECT_RESPONSE_INVALID",
  "PROJECT_ERROR_RESPONSE_INVALID",
] as const;

export const projectRoleSchema = z.enum(PROJECT_ROLES);
export const invitableProjectRoleSchema = z.enum(INVITABLE_PROJECT_ROLES);
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
    status: z.enum(["active", "pending_deletion"]),
    is_suspended: z.boolean(),
    membership_version: z.number().int().positive(),
    request_id: z.string().min(1),
    deletion_effective_at: z
      .string()
      .datetime({ offset: true })
      .nullable()
      .optional(),
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
    includeRecoverable: z.boolean().optional(),
  })
  .strict();

export const membershipStatusSchema = z.enum(["active", "left", "removed"]);

export const projectMembershipSchema = z
  .object({
    membership_id: projectIdSchema,
    user_id: projectIdSchema,
    account_email: z.string().email(),
    role: projectRoleSchema,
    status: membershipStatusSchema,
    version: z.number().int().positive(),
    joined_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const projectMembershipListSchema = z.array(projectMembershipSchema);

export const changeProjectMemberRoleSchema = z
  .object({
    role: projectRoleSchema,
    version: z.number().int().positive(),
  })
  .strict();

export const membershipVersionSchema = z
  .object({ version: z.number().int().positive() })
  .strict();

export const invitationStatusSchema = z.enum([
  "pending",
  "redeemed",
  "revoked",
  "expired",
]);

export const projectInvitationSchema = z
  .object({
    id: projectIdSchema,
    project_id: projectIdSchema,
    invited_email: z.string().email(),
    role: projectRoleSchema,
    status: invitationStatusSchema,
    expires_at: z.string().datetime({ offset: true }),
    version: z.number().int().positive(),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const projectInvitationListSchema = z.array(projectInvitationSchema);

export const createProjectInvitationSchema = z
  .object({
    email: z.string().trim().email(),
    role: invitableProjectRoleSchema,
  })
  .strict();

export const createdProjectInvitationSchema = projectInvitationSchema
  .extend({ invite_url_fragment: z.string().startsWith("/invite#token=") })
  .strict();

export const invitationClaimSchema = z
  .object({ token: z.string().min(1) })
  .strict();

export const invitationClaimResponseSchema = z
  .object({ message: z.literal("Invitation claim processed") })
  .strict();

export const redeemedProjectInvitationSchema = z
  .object({
    invitation_id: projectIdSchema,
    project_id: projectIdSchema,
    project_slug: z.string().min(1),
    membership_id: projectIdSchema,
    role: projectRoleSchema,
  })
  .strict();

export type ProjectRole = z.infer<typeof projectRoleSchema>;
export type InvitableProjectRole = z.infer<typeof invitableProjectRoleSchema>;
export type Capability = z.infer<typeof capabilitySchema>;
export type ProjectErrorCode = z.infer<typeof projectErrorCodeSchema>;
export type Project = z.infer<typeof projectSchema>;
export type ProjectPage = z.infer<typeof projectPageSchema>;
export type CreateProjectInput = z.input<typeof createProjectSchema>;
export type PatchProjectInput = z.input<typeof patchProjectSchema>;
export type PinProjectInput = z.input<typeof pinProjectSchema>;
export type ProjectFilters = z.input<typeof projectFiltersSchema>;
export type MembershipStatus = z.infer<typeof membershipStatusSchema>;
export type ProjectMembership = z.infer<typeof projectMembershipSchema>;
export type ChangeProjectMemberRoleInput = z.input<
  typeof changeProjectMemberRoleSchema
>;
export type InvitationStatus = z.infer<typeof invitationStatusSchema>;
export type ProjectInvitation = z.infer<typeof projectInvitationSchema>;
export type CreateProjectInvitationInput = z.input<
  typeof createProjectInvitationSchema
>;
export type CreatedProjectInvitation = z.infer<
  typeof createdProjectInvitationSchema
>;
export type RedeemedProjectInvitation = z.infer<
  typeof redeemedProjectInvitationSchema
>;
