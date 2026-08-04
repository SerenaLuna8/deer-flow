import { z } from "zod";

export const PROJECT_CHANNEL_GROUP_BINDING_STATUSES = [
  "active",
  "disabled",
] as const;

export const projectChannelGroupBindingSchema = z
  .object({
    id: z.string().uuid(),
    provider: z.string().min(1),
    display_name: z.string().min(1),
    status: z.enum(PROJECT_CHANNEL_GROUP_BINDING_STATUSES),
    agent_asset_id: z.string().uuid(),
    agent_scope: z.enum(["project", "system"]),
    last_activity_at: z.string().datetime({ offset: true }).nullable(),
    revision: z.number().int().positive(),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const projectChannelGroupBindingsResponseSchema = z
  .object({
    bindings: z.array(projectChannelGroupBindingSchema),
  })
  .strict();

export const projectChannelGroupBindingChallengeSchema = z
  .object({
    provider: z.string().min(1),
    code: z.string().min(1),
    command: z.string().min(1),
    expires_at: z.string().datetime({ offset: true }),
    expires_in: z.number().int().nonnegative(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.command !== `/bind-project ${value.code}`) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["command"],
        message: "Binding command does not match its code",
      });
    }
  });

export const createProjectChannelGroupBindingChallengeInputSchema = z
  .object({
    provider: z.string().min(1),
    agentAssetId: z.string().uuid(),
    agentScope: z.enum(["project", "system"]),
  })
  .strict();

export const updateProjectChannelGroupBindingInputSchema = z
  .object({
    expectedRevision: z.number().int().positive(),
    enabled: z.boolean().optional(),
    agentAssetId: z.string().uuid().optional(),
    agentScope: z.enum(["project", "system"]).optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      (value.agentAssetId === undefined) !==
      (value.agentScope === undefined)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["agentAssetId"],
        message: "Agent asset and scope must be updated together",
      });
    }
    if (value.enabled === undefined && value.agentAssetId === undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "At least one binding field must be updated",
      });
    }
  });

export type ProjectChannelGroupBinding = z.infer<
  typeof projectChannelGroupBindingSchema
>;
export type ProjectChannelGroupBindingStatus =
  (typeof PROJECT_CHANNEL_GROUP_BINDING_STATUSES)[number];
export type ProjectChannelGroupBindingsResponse = z.infer<
  typeof projectChannelGroupBindingsResponseSchema
>;
export type ProjectChannelGroupBindingChallenge = z.infer<
  typeof projectChannelGroupBindingChallengeSchema
>;
export type CreateProjectChannelGroupBindingChallengeInput = z.infer<
  typeof createProjectChannelGroupBindingChallengeInputSchema
>;
export type UpdateProjectChannelGroupBindingInput = z.infer<
  typeof updateProjectChannelGroupBindingInputSchema
>;
