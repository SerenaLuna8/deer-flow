import { z } from "zod";

export const adminSystemSettingsAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

export const systemSettingsSectionNameSchema = z.enum([
  "agent_runtime",
  "auth",
  "automations",
  "memory_document",
  "quotas",
]);
export const systemSettingsEffectScopeSchema = z.enum([
  "new_requests",
  "new_runs",
  "new_requests_and_runs",
  "new_memory_documents",
  "next_authoritative_check",
  "restart_required",
]);
export const systemSettingsPendingRoleSchema = z.enum([
  "gateway",
  "worker",
  "scheduler",
]);

const MAX_JSON_BYTES = 32_768;
const MAX_SAFE_INTEGER = Number.MAX_SAFE_INTEGER;
const secretKeyPattern =
  /(?:^|[_-])(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|authorization|cookie|secret|private[_-]?key|nonce|ciphertext|storage[_-]?locator)(?:$|[_-])/iu;
const secretValuePatterns = [
  /\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/iu,
  /\b(?:sk|pk|tp)-(?:proj-)?[A-Za-z0-9_-]{8,}/iu,
  /\bAKIA[0-9A-Z]{16}\b/u,
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/u,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/u,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/u,
] as const;
const httpUrlPattern = /https?:\/\/[^\s<>()]+/giu;
const logicalModelNameSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$/u);
const toolNameSchema = z
  .string()
  .trim()
  .min(1)
  .max(128)
  .regex(/^[A-Za-z_][A-Za-z0-9_.:-]*$/u)
  .refine(
    (value) =>
      !/(?:^|[._:-])(?:api[_-]?key|authorization|bearer|client[_-]?secret|password|passwd|secret|token)(?:$|[._:-])/iu.test(
        value,
      ) && !/^(?:sk|pk|tp)-(?:proj-)?[A-Za-z0-9_-]{8,}$/u.test(value),
    "Runtime names must not contain credential-like values",
  );
function containsSecretLikeMaterial(value: unknown): boolean {
  if (Array.isArray(value)) {
    return value.some(containsSecretLikeMaterial);
  }
  if (value !== null && typeof value === "object") {
    return Object.entries(value).some(
      ([key, item]) =>
        secretKeyPattern.test(key) || containsSecretLikeMaterial(item),
    );
  }
  if (typeof value !== "string") return false;
  if (
    Array.from(value).some((character) => {
      const codePoint = character.codePointAt(0);
      return (
        codePoint !== undefined &&
        codePoint < 32 &&
        character !== "\n" &&
        character !== "\t"
      );
    })
  )
    return true;
  if (secretValuePatterns.some((pattern) => pattern.test(value))) return true;
  for (const match of value.matchAll(httpUrlPattern)) {
    try {
      const url = new URL(match[0].replace(/[.,;:]$/u, ""));
      if (url.username || url.password || url.search || url.hash) return true;
    } catch {
      return true;
    }
  }
  return false;
}

function boundedJson<T extends z.ZodTypeAny>(
  schema: T,
): z.ZodEffects<T, z.output<T>, z.input<T>> {
  return schema.superRefine((value, context) => {
    if (containsSecretLikeMaterial(value)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "System settings must not contain secret-like material",
      });
    }
    if (
      new TextEncoder().encode(JSON.stringify(value)).byteLength >
      MAX_JSON_BYTES
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "System settings exceed the safe size limit",
      });
    }
  });
}

const boundedInteger = (minimum: number, maximum: number) =>
  z.number().int().min(minimum).max(Math.min(maximum, MAX_SAFE_INTEGER));
const ratioSchema = z.number().finite().min(0).max(1);

const tokenBudgetSchema = z
  .object({
    enabled: z.boolean(),
    max_tokens: boundedInteger(1_000, 2_000_000),
    max_input_tokens: boundedInteger(1, 2_000_000).nullable(),
    max_output_tokens: boundedInteger(1, 2_000_000).nullable(),
    warn_threshold: ratioSchema,
    hard_stop_threshold: ratioSchema,
  })
  .strict()
  .refine((value) => value.hard_stop_threshold >= value.warn_threshold, {
    path: ["hard_stop_threshold"],
    message: "Hard stop threshold cannot be below the warning threshold",
  });

const contextSizeSchema = z.discriminatedUnion("type", [
  z
    .object({
      type: z.literal("fraction"),
      value: z.number().finite().gt(0).max(1),
    })
    .strict(),
  z
    .object({
      type: z.literal("tokens"),
      value: boundedInteger(1, 2_000_000),
    })
    .strict(),
  z
    .object({
      type: z.literal("messages"),
      value: boundedInteger(1, 2_000_000),
    })
    .strict(),
]);

const uniqueNameListSchema = <T extends z.ZodType<string>>(
  item: T,
  maximum: number,
  minimum = 0,
) =>
  z
    .array(item)
    .min(minimum)
    .max(maximum)
    .refine(
      (items) => new Set(items).size === items.length,
      "Runtime names must be unique",
    );

const toolOutputOverridesSchema = z
  .record(toolNameSchema, boundedInteger(0, 10_000_000))
  .superRefine((value, context) => {
    if (Object.keys(value).length > 64) {
      context.addIssue({
        code: z.ZodIssueCode.too_big,
        type: "array",
        maximum: 64,
        inclusive: true,
      });
    }
  });

const toolFrequencyOverrideSchema = z
  .object({
    warn: boundedInteger(1, 100_000),
    hard_limit: boundedInteger(1, 100_000),
  })
  .strict()
  .refine((value) => value.hard_limit >= value.warn, {
    path: ["hard_limit"],
    message: "Hard limit cannot be below the warning threshold",
  });
const toolFrequencyOverridesSchema = z
  .record(toolNameSchema, toolFrequencyOverrideSchema)
  .superRefine((value, context) => {
    if (Object.keys(value).length > 64) {
      context.addIssue({
        code: z.ZodIssueCode.too_big,
        type: "array",
        maximum: 64,
        inclusive: true,
      });
    }
  });

export const agentRuntimeSettingsValueSchema = boundedJson(
  z
    .object({
      token_usage: z.object({ enabled: z.boolean() }).strict(),
      token_budget: tokenBudgetSchema,
      max_recursion_limit: boundedInteger(1, 100_000),
      title: z
        .object({
          enabled: z.boolean(),
          max_words: boundedInteger(1, 20),
          max_chars: boundedInteger(10, 200),
          model_name: logicalModelNameSchema.nullable(),
        })
        .strict(),
      suggestions: z.object({ enabled: z.boolean() }).strict(),
      input_polish: z
        .object({
          enabled: z.boolean(),
          max_chars: boundedInteger(1, 100_000),
          model_name: logicalModelNameSchema.nullable(),
        })
        .strict(),
      summarization: z
        .object({
          enabled: z.boolean(),
          model_name: logicalModelNameSchema.nullable(),
          trigger: z.array(contextSizeSchema).max(8).nullable(),
          keep: contextSizeSchema,
          trim_tokens_to_summarize: boundedInteger(1, 2_000_000).nullable(),
          skill_file_read_tool_names: uniqueNameListSchema(
            toolNameSchema,
            32,
            1,
          ),
        })
        .strict(),
      memory: z
        .object({
          enabled: z.boolean(),
          model_name: logicalModelNameSchema.nullable(),
          dream_interval_minutes: boundedInteger(15, 1_440),
          max_injection_tokens: boundedInteger(100, 8_000),
          idle_seal_minutes: boundedInteger(0, 10_080).refine(
            (value) => value === 0 || value >= 30,
            { message: "idle_seal_minutes must be 0 or at least 30" },
          ),
          episode_retention_days: boundedInteger(0, 3_650).refine(
            (value) => value === 0 || value >= 30,
            { message: "episode_retention_days must be 0 or at least 30" },
          ),
        })
        .strict(),
      tool_search: z
        .object({
          enabled: z.boolean(),
          auto_promote_top_k: boundedInteger(1, 5),
        })
        .strict(),
      tool_output: z
        .object({
          enabled: z.boolean(),
          externalize_min_chars: boundedInteger(0, 10_000_000),
          preview_head_chars: boundedInteger(0, 10_000_000),
          preview_tail_chars: boundedInteger(0, 10_000_000),
          fallback_max_chars: boundedInteger(0, 10_000_000),
          fallback_head_chars: boundedInteger(0, 10_000_000),
          fallback_tail_chars: boundedInteger(0, 10_000_000),
          exempt_tools: uniqueNameListSchema(toolNameSchema, 64),
          tool_overrides: toolOutputOverridesSchema,
        })
        .strict(),
      loop_detection: z
        .object({
          enabled: z.boolean(),
          warn_threshold: boundedInteger(1, 100_000),
          hard_limit: boundedInteger(1, 100_000),
          window_size: boundedInteger(1, 100_000),
          max_tracked_threads: boundedInteger(1, 100_000),
          tool_freq_warn: boundedInteger(1, 100_000),
          tool_freq_hard_limit: boundedInteger(1, 100_000),
          tool_freq_overrides: toolFrequencyOverridesSchema,
        })
        .strict()
        .superRefine((value, context) => {
          if (value.hard_limit < value.warn_threshold) {
            context.addIssue({
              code: z.ZodIssueCode.custom,
              path: ["hard_limit"],
              message: "Hard limit cannot be below the warning threshold",
            });
          }
          if (value.tool_freq_hard_limit < value.tool_freq_warn) {
            context.addIssue({
              code: z.ZodIssueCode.custom,
              path: ["tool_freq_hard_limit"],
              message: "Hard limit cannot be below the warning threshold",
            });
          }
        }),
      read_before_write: z.object({ enabled: z.boolean() }).strict(),
      safety_finish_reason: z.object({ enabled: z.boolean() }).strict(),
      subagents: z
        .object({ max_total_per_run: boundedInteger(1, 50) })
        .strict(),
    })
    .strict(),
);

export const authSettingsValueSchema = boundedJson(
  z.object({ allow_registration: z.boolean() }).strict(),
);

export const quotaSettingsValueSchema = boundedJson(
  z
    .object({
      default_member_limit: boundedInteger(1, MAX_SAFE_INTEGER),
      default_storage_bytes_limit: boundedInteger(0, MAX_SAFE_INTEGER),
      default_concurrent_run_limit: boundedInteger(1, MAX_SAFE_INTEGER),
      default_mcp_calls_daily_limit: boundedInteger(0, MAX_SAFE_INTEGER),
      warning_threshold: z.number().finite().gt(0).lt(1),
    })
    .strict(),
);

export const automationsSettingsValueSchema = boundedJson(
  z
    .object({
      enabled: z.boolean(),
      poll_interval_seconds: boundedInteger(1, 300),
      max_concurrent_runs: boundedInteger(1, 32),
      min_once_delay_seconds: boundedInteger(0, 86_400),
    })
    .strict(),
);

const memoryDocumentTitleSchema = z
  .string()
  .refine((value) => !/[\p{C}\p{Zl}\p{Zp}]/u.test(value), {
    message:
      "Memory document titles must not contain control or line-separator characters",
  })
  .transform((value) => value.trim())
  .pipe(
    z
      .string()
      .min(1)
      .refine((value) => Array.from(value).length <= 80, {
        message: "Memory document titles must not exceed 80 Unicode characters",
      })
      .refine((value) => !value.startsWith("#"), {
        message:
          "Memory document titles must not include a Markdown heading prefix",
      })
      .refine(
        (value) =>
          !/\[H:\d+\]/iu.test(value) &&
          !/\[(?:skip|correction|permanent|durable|ephemeral)\]/iu.test(value),
        {
          message:
            "Memory document titles must not contain Dream history markers",
        },
      ),
  );

export const memoryDocumentSettingsValueSchema = boundedJson(
  z
    .object({
      sections: z
        .array(memoryDocumentTitleSchema)
        .min(2)
        .max(8)
        .refine(
          (sections) => new Set(sections).size === sections.length,
          "Memory document titles must be unique",
        ),
    })
    .strict(),
);

const sectionMetadataFields = {
  revision: z.number().int().positive(),
  schema_version: z.number().int().positive(),
  effective_revision: z.number().int().positive(),
  updated_at: z.string().datetime({ offset: true }),
} as const;

const agentRuntimeSectionSchema = z
  .object({
    section: z.literal("agent_runtime"),
    ...sectionMetadataFields,
    value: agentRuntimeSettingsValueSchema,
    effect_scope: z.literal("new_requests_and_runs"),
  })
  .strict();
const authSectionSchema = z
  .object({
    section: z.literal("auth"),
    ...sectionMetadataFields,
    value: authSettingsValueSchema,
    effect_scope: z.literal("new_requests"),
  })
  .strict();
const memoryDocumentSectionSchema = z
  .object({
    section: z.literal("memory_document"),
    ...sectionMetadataFields,
    value: memoryDocumentSettingsValueSchema,
    effect_scope: z.literal("new_memory_documents"),
  })
  .strict();
const quotasSectionSchema = z
  .object({
    section: z.literal("quotas"),
    ...sectionMetadataFields,
    value: quotaSettingsValueSchema,
    effect_scope: z.literal("next_authoritative_check"),
  })
  .strict();
const automationsSectionSchema = z
  .object({
    section: z.literal("automations"),
    ...sectionMetadataFields,
    value: automationsSettingsValueSchema,
    effect_scope: z.literal("new_requests"),
  })
  .strict();

export const systemSettingsCatalogSchema = z
  .object({
    catalog_revision: z.number().int().positive(),
    sections: z
      .object({
        agent_runtime: agentRuntimeSectionSchema,
        auth: authSectionSchema,
        automations: automationsSectionSchema,
        memory_document: memoryDocumentSectionSchema,
        quotas: quotasSectionSchema,
      })
      .strict(),
  })
  .strict();

const pendingRolesSchema = z
  .array(systemSettingsPendingRoleSchema)
  .max(3)
  .refine((roles) => new Set(roles).size === roles.length);
const mutationBaseFields = {
  catalog_revision: z.number().int().positive(),
  stored_revision: z.number().int().positive(),
  effective_revision: z.number().int().positive(),
  effective_at: z.string().datetime({ offset: true }),
  pending_roles: pendingRolesSchema,
} as const;

function mutationResponseSchema<
  Section extends
    | "agent_runtime"
    | "auth"
    | "automations"
    | "memory_document"
    | "quotas",
  Value extends z.ZodTypeAny,
  Effect extends
    | "new_requests_and_runs"
    | "new_memory_documents"
    | "new_requests"
    | "next_authoritative_check",
>(section: Section, value: Value, effectScope: Effect) {
  return z
    .object({
      ...mutationBaseFields,
      section: z.literal(section),
      effect_scope: z.literal(effectScope),
      policy: z
        .object({
          revision: z.number().int().positive(),
          schema_version: z.number().int().positive(),
          value,
        })
        .strict(),
    })
    .strict();
}

export const systemSettingsMutationResponseSchema = z.discriminatedUnion(
  "section",
  [
    mutationResponseSchema(
      "agent_runtime",
      agentRuntimeSettingsValueSchema,
      "new_requests_and_runs",
    ),
    mutationResponseSchema("auth", authSettingsValueSchema, "new_requests"),
    mutationResponseSchema(
      "automations",
      automationsSettingsValueSchema,
      "new_requests",
    ),
    mutationResponseSchema(
      "memory_document",
      memoryDocumentSettingsValueSchema,
      "new_memory_documents",
    ),
    mutationResponseSchema(
      "quotas",
      quotaSettingsValueSchema,
      "next_authoritative_check",
    ),
  ],
);

export const replaceAgentRuntimeSettingsInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    value: agentRuntimeSettingsValueSchema,
  })
  .strict();
export const replaceAuthSettingsInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    value: authSettingsValueSchema,
  })
  .strict();
export const replaceMemoryDocumentSettingsInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    value: memoryDocumentSettingsValueSchema,
  })
  .strict();
export const replaceQuotaSettingsInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    value: quotaSettingsValueSchema,
  })
  .strict();
export const replaceAutomationsSettingsInputSchema = z
  .object({
    expected_revision: z.number().int().positive(),
    value: automationsSettingsValueSchema,
  })
  .strict();

export type AgentRuntimeSettingsValue = z.infer<
  typeof agentRuntimeSettingsValueSchema
>;
export type AuthSettingsValue = z.infer<typeof authSettingsValueSchema>;
export type MemoryDocumentSettingsValue = z.infer<
  typeof memoryDocumentSettingsValueSchema
>;
export type QuotaSettingsValue = z.infer<typeof quotaSettingsValueSchema>;
export type AutomationsSettingsValue = z.infer<
  typeof automationsSettingsValueSchema
>;
export type SystemSettingsCatalog = z.infer<typeof systemSettingsCatalogSchema>;
export type SystemSettingsMutationResponse = z.infer<
  typeof systemSettingsMutationResponseSchema
>;
export type SystemSettingsSectionName = z.infer<
  typeof systemSettingsSectionNameSchema
>;
export type SystemSettingsEffectScope = z.infer<
  typeof systemSettingsEffectScopeSchema
>;
export type ReplaceAgentRuntimeSettingsInput = z.infer<
  typeof replaceAgentRuntimeSettingsInputSchema
>;
export type ReplaceAuthSettingsInput = z.infer<
  typeof replaceAuthSettingsInputSchema
>;
export type ReplaceMemoryDocumentSettingsInput = z.infer<
  typeof replaceMemoryDocumentSettingsInputSchema
>;
export type ReplaceQuotaSettingsInput = z.infer<
  typeof replaceQuotaSettingsInputSchema
>;
export type ReplaceAutomationsSettingsInput = z.infer<
  typeof replaceAutomationsSettingsInputSchema
>;

export type SystemSettingsSectionValueMap = {
  agent_runtime: AgentRuntimeSettingsValue;
  auth: AuthSettingsValue;
  automations: AutomationsSettingsValue;
  memory_document: MemoryDocumentSettingsValue;
  quotas: QuotaSettingsValue;
};

export function validateAgentRuntimeModelReferences(
  value: unknown,
  activeModelNames: readonly string[],
): AgentRuntimeSettingsValue {
  const parsed = agentRuntimeSettingsValueSchema.parse(value);
  const allowed = new Set(
    activeModelNames.map((name) => logicalModelNameSchema.parse(name)),
  );
  const referenced = [
    parsed.title.model_name,
    parsed.input_polish.model_name,
    parsed.summarization.model_name,
    parsed.memory.model_name,
  ].filter((name): name is string => name !== null);
  if (referenced.some((name) => !allowed.has(name))) {
    throw new Error(
      "Every model setting must reference a current active model",
    );
  }
  return parsed;
}
