import { z } from "zod";

import {
  agentModelSettingsSchema,
  assetScopeSchema,
  currentVersionAssetSummarySchema,
} from "@/core/shared-assets/types";

const uuidSchema = z.string().uuid();
const exactModelUuidSchema = uuidSchema.refine(
  (value) => value === value.toLowerCase(),
  "Model UUID must use its canonical lowercase form",
);
const timestampSchema = z.string().datetime({ offset: true });
export const AGENT_BUILDER_SLUG_MIN_LENGTH = 3;
export const AGENT_BUILDER_SLUG_MAX_LENGTH = 63;
export const AGENT_BUILDER_SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/u;

export const agentBuilderSlugSchema = z
  .string()
  .trim()
  .min(AGENT_BUILDER_SLUG_MIN_LENGTH)
  .max(AGENT_BUILDER_SLUG_MAX_LENGTH)
  .regex(AGENT_BUILDER_SLUG_PATTERN);

export const agentBuilderStatusSchema = z.enum([
  "interviewing",
  "generating",
  "awaiting_clarification",
  "proposal_ready",
  "committing",
  "completed",
  "failed",
  "cancelled",
]);

export const agentBuilderProgressStatusSchema = z.enum([
  "pending",
  "running",
  "completed",
  "failed",
]);

export const agentBuilderProgressItemSchema = z
  .object({
    id: z.string().trim().min(1),
    label: z.string().trim().min(1),
    status: agentBuilderProgressStatusSchema,
  })
  .strict();

export const agentBuilderMessageSchema = z
  .object({
    id: z.string().trim().min(1),
    role: z.enum(["user", "assistant"]),
    content: z.string(),
    created_at: timestampSchema,
    operation_id: uuidSchema.nullable().default(null),
  })
  .strict();

export const agentBuilderConflictFieldSchema = z.enum([
  "agents_instructions",
  "soul",
  "identity",
  "user_context",
]);

export const agentBuilderConflictSchema = z
  .object({
    code: z.string().trim().min(1),
    fields: z.array(agentBuilderConflictFieldSchema),
    message: z.string().trim().min(1),
    severity: z.enum(["warning", "error"]),
  })
  .strict();

export const agentBuilderBlueprintSchema = z
  .object({
    description: z.string(),
    model_ref: z.string().trim().min(1),
    tool_groups: z.array(z.string().trim().min(1)),
    skill_refs: z.array(
      z.object({ scope: assetScopeSchema, asset_id: uuidSchema }).strict(),
    ),
    mcp_version_ids: z.array(uuidSchema),
    agents_instructions: z.string(),
    soul: z.string(),
    identity: z.string(),
    user_context: z.string(),
    model_settings: agentModelSettingsSchema.optional(),
  })
  .strict();

const humanInputOptionSchema = z
  .object({
    id: z.string().trim().min(1),
    label: z.string().trim().min(1),
    value: z.string(),
  })
  .strict();

const humanInputRequestSchema = z
  .object({
    version: z.literal(1),
    kind: z.literal("human_input_request"),
    source: z.string().trim().min(1),
    request_id: z.string().trim().min(1),
    tool_call_id: z.string().trim().min(1).optional(),
    clarification_type: z.string().trim().min(1).optional(),
    title: z.string().optional(),
    question: z.string().trim().min(1),
    context: z.string().nullable().optional(),
    input_mode: z.enum(["free_text", "single_choice", "choice_with_other"]),
    options: z.array(humanInputOptionSchema).optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.input_mode !== "free_text" &&
      (!value.options || value.options.length === 0)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Choice input requires at least one option",
        path: ["options"],
      });
    }
  });

export const agentBuilderSessionSchema = z
  .object({
    id: uuidSchema,
    project_id: uuidSchema,
    owner_user_id: z.string().trim().min(1),
    thread_id: uuidSchema,
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
    status: agentBuilderStatusSchema,
    revision: z.number().int().positive(),
    blueprint: agentBuilderBlueprintSchema.nullable(),
    blueprint_checksum: z.string().trim().min(1).nullable(),
    assumptions: z.array(z.string().trim().min(1)).default([]),
    conflicts: z.array(agentBuilderConflictSchema).default([]),
    messages: z.array(agentBuilderMessageSchema),
    active_clarification: humanInputRequestSchema.nullable(),
    active_clarifications: z.array(humanInputRequestSchema),
    progress: z.array(agentBuilderProgressItemSchema),
    error_code: z.string().trim().min(1).nullable(),
    error_message: z.string().trim().min(1).nullable(),
    created_agent_id: uuidSchema.nullable(),
    generation_preference: z
      .object({
        model_ref: z.union([z.literal("default"), exactModelUuidSchema]),
        mode: z.enum(["flash", "thinking", "pro", "ultra"]),
      })
      .strict()
      .nullable()
      .default(null),
    created_at: timestampSchema,
    updated_at: timestampSchema,
  })
  .strict();

export const agentBuilderSessionSummarySchema = z
  .object({
    id: uuidSchema,
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
    status: agentBuilderStatusSchema,
    revision: z.number().int().positive(),
    updated_at: timestampSchema,
  })
  .strict();

export const agentBuilderActivityKindSchema = z.enum([
  "turn_accepted",
  "attempt_started",
  "reasoning",
  "candidate_generated",
  "validation_started",
  "validation_passed",
  "validation_failed",
  "repair_started",
  "turn_terminal",
  "commit_accepted",
  "commit_validation_started",
  "commit_validation_passed",
  "commit_persistence_started",
  "commit_persistence_completed",
  "commit_terminal",
]);

export const agentBuilderActivitySchema = z
  .object({
    seq: z.string().regex(/^(?:0|[1-9][0-9]*)$/u),
    operation_id: uuidSchema,
    kind: agentBuilderActivityKindSchema,
    attempt: z.union([z.literal(1), z.literal(2)]).nullable(),
    payload: z
      .object({
        text: z.string().nullable().optional(),
        status: z
          .enum(["completed", "failed", "stopped", "cancelled"])
          .nullable()
          .optional(),
        duration_ms: z.number().int().nonnegative().nullable().optional(),
        error_code: z.string().trim().min(1).nullable().optional(),
      })
      .strict(),
    created_at: timestampSchema,
  })
  .strict();

export const agentBuilderActivityListResponseSchema = z
  .object({
    data: z.array(agentBuilderActivitySchema).max(2000),
    request_id: z.string().trim().min(1),
  })
  .strict();

export const agentBuilderSessionResponseSchema = z
  .object({
    data: agentBuilderSessionSchema,
    request_id: z.string().trim().min(1),
  })
  .strict();

export const agentBuilderSessionListResponseSchema = z
  .object({
    data: z.array(agentBuilderSessionSummarySchema).max(100),
    next_cursor: z.string().min(1).max(256).nullable().default(null),
    request_id: z.string().trim().min(1),
  })
  .strict();

export const agentBuilderSessionListInputSchema = z
  .object({
    limit: z.number().int().min(1).max(100).optional(),
    cursor: z.string().min(1).max(256).optional(),
  })
  .strict();

export const createAgentBuilderSessionInputSchema = z
  .object({
    slug: agentBuilderSlugSchema,
    display_name: z.string().trim().min(1),
    idempotency_key: z.string().trim().min(1),
  })
  .strict();

export const agentBuilderMessageTurnInputSchema = z
  .object({
    kind: z.literal("message"),
    message: z.string().trim().min(1),
  })
  .strict();

export const agentBuilderClarificationTurnInputSchema = z
  .object({
    kind: z.literal("clarification"),
    response: z
      .object({
        version: z.literal(1),
        kind: z.literal("human_input_response"),
        source: z.string().trim().min(1),
        request_id: z.string().trim().min(1),
        response_kind: z.enum(["option", "text"]),
        option_id: z.string().trim().min(1).optional(),
        value: z.string().trim().min(1),
      })
      .strict(),
  })
  .strict();

export const agentBuilderBlueprintTurnInputSchema = z
  .object({
    kind: z.literal("blueprint_update"),
    blueprint: agentBuilderBlueprintSchema,
  })
  .strict();

export const agentBuilderTurnInputSchema = z
  .object({
    input: z.discriminatedUnion("kind", [
      agentBuilderMessageTurnInputSchema,
      agentBuilderClarificationTurnInputSchema,
      agentBuilderBlueprintTurnInputSchema,
    ]),
    generation_model_ref: z
      .union([z.literal("default"), exactModelUuidSchema])
      .optional(),
    generation_mode: z.enum(["flash", "thinking", "pro", "ultra"]).optional(),
    thinking_enabled: z.boolean().optional(),
    reasoning_effort: z
      .enum(["none", "low", "medium", "high"])
      .nullable()
      .optional(),
    expected_revision: z.number().int().positive(),
    idempotency_key: z.string().trim().min(1),
  })
  .strict()
  .superRefine((value, context) => {
    const executionFields = [
      value.generation_mode,
      value.thinking_enabled,
      value.reasoning_effort,
    ];
    if (
      executionFields.some((field) => field !== undefined) &&
      (value.generation_model_ref === undefined ||
        executionFields.some((field) => field === undefined))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Generation profile fields must be provided together",
      });
    }
  });

export const agentBuilderGenerationPreferenceInputSchema = z
  .object({
    generation_model_ref: z.union([z.literal("default"), exactModelUuidSchema]),
    generation_mode: z.enum(["flash", "thinking", "pro", "ultra"]),
    thinking_enabled: z.boolean(),
    reasoning_effort: z.enum(["none", "low", "medium", "high"]).nullable(),
  })
  .strict();

export const commitAgentBuilderSessionInputSchema = z
  .object({
    slug: agentBuilderSlugSchema.optional(),
    expected_revision: z.number().int().positive(),
    expected_blueprint_checksum: z.string().trim().min(1),
    idempotency_key: z.string().trim().min(1),
  })
  .strict();

export const cancelAgentBuilderSessionInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    idempotency_key: z.string().trim().min(1),
  })
  .strict();

export const agentBuilderCommitResponseSchema = z
  .object({
    data: z
      .object({
        session: agentBuilderSessionSchema,
        agent: currentVersionAssetSummarySchema,
      })
      .strict(),
    request_id: z.string().trim().min(1),
  })
  .strict();

export type AgentBuilderStatus = z.infer<typeof agentBuilderStatusSchema>;
export type AgentBuilderProgressItem = z.infer<
  typeof agentBuilderProgressItemSchema
>;
export type AgentBuilderMessage = z.infer<typeof agentBuilderMessageSchema>;
export type AgentBuilderActivity = z.infer<typeof agentBuilderActivitySchema>;
export type AgentBuilderConflictField = z.infer<
  typeof agentBuilderConflictFieldSchema
>;
export type AgentBuilderConflict = z.infer<typeof agentBuilderConflictSchema>;
export type AgentBuilderBlueprint = z.infer<typeof agentBuilderBlueprintSchema>;
export type AgentBuilderSession = z.infer<typeof agentBuilderSessionSchema>;
export type AgentBuilderSessionSummary = z.infer<
  typeof agentBuilderSessionSummarySchema
>;
export type AgentBuilderSessionListInput = z.input<
  typeof agentBuilderSessionListInputSchema
>;
export type AgentBuilderSessionListResponse = z.output<
  typeof agentBuilderSessionListResponseSchema
>;
export type CreateAgentBuilderSessionInput = z.input<
  typeof createAgentBuilderSessionInputSchema
>;
export type AgentBuilderTurnInput = z.input<typeof agentBuilderTurnInputSchema>;
export type AgentBuilderGenerationPreferenceInput = z.input<
  typeof agentBuilderGenerationPreferenceInputSchema
>;
export type CommitAgentBuilderSessionInput = z.input<
  typeof commitAgentBuilderSessionInputSchema
>;
export type CancelAgentBuilderSessionInput = z.input<
  typeof cancelAgentBuilderSessionInputSchema
>;
