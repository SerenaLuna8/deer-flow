import { z } from "zod";

export const adminModelAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

export const adminModelIdSchema = z.string().uuid();
export const adminModelStatusSchema = z.enum(["active", "suspended"]);
export const adminModelProviderAdapterSchema = z.enum([
  "anthropic",
  "claude_code",
  "codex_cli",
  "deepseek",
  "mindie",
  "openai",
  "patched_deepseek",
  "patched_mimo",
  "patched_minimax",
  "patched_openai",
  "patched_stepfun",
  "vision_bridge_fake",
  "vllm",
]);

const logicalNameSchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$/u)
  .superRefine((value, context) => {
    if (hasSecretLikeValue(value)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Logical name must not contain a credential-like value",
      });
    }
  });
const providerFieldSchema = z
  .string()
  .trim()
  .min(1)
  .max(255)
  .regex(/^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$/u)
  .superRefine((value, context) => {
    if (hasSecretLikeValue(value)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Provider model must not contain a credential-like value",
      });
    }
  });
const credentialEnvKeySchema = z
  .string()
  .min(1)
  .max(128)
  .regex(/^[A-Z_][A-Z0-9_]*$/u);

export type AdminModelSettingValue =
  | string
  | number
  | boolean
  | null
  | AdminModelSettingValue[]
  | { [key: string]: AdminModelSettingValue };

const SECRET_VALUE_PATTERNS = [
  /\bbearer\s+[A-Za-z0-9._~+/=-]{8,}/iu,
  /\b(?:sk|pk|tp)-(?:proj-)?[A-Za-z0-9_-]{8,}/iu,
  /\bAKIA[0-9A-Z]{16}\b/u,
  /\bAIza[0-9A-Za-z_-]{20,}\b/u,
  /\bgh[pousr]_[A-Za-z0-9]{20,}\b/u,
  /\bxox[baprs]-[A-Za-z0-9-]{10,}\b/u,
  /\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/u,
  /-----BEGIN [A-Z ]*PRIVATE KEY-----/u,
  /\b(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|client[ _-]?secret|password|passwd|authorization|secret)\s*[:=]\s*(?:bearer\s+)?[^\s,;]{4,}/iu,
] as const;
const URL_IN_TEXT = /https?:\/\/[^\s<>()]+/giu;

function hasSecretLikeValue(value: string): boolean {
  if (
    [...value].some((character) => {
      const code = character.codePointAt(0) ?? 0;
      return code < 32 && character !== "\n" && character !== "\t";
    }) ||
    SECRET_VALUE_PATTERNS.some((pattern) => pattern.test(value))
  ) {
    return true;
  }
  for (const match of value.matchAll(URL_IN_TEXT)) {
    try {
      const url = new URL(match[0].replace(/[.,;:]+$/u, ""));
      if (url.username || url.password || url.search || url.hash) return true;
    } catch {
      return true;
    }
  }
  return false;
}

function secretSafeTextSchema(max: number, required: boolean) {
  return z
    .string()
    .trim()
    .max(max)
    .superRefine((value, context) => {
      if ((required && value.length === 0) || hasSecretLikeValue(value)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Public model text must not contain credential-like values",
        });
      }
    });
}

const baseUrlSchema = z
  .string()
  .trim()
  .min(1)
  .max(2048)
  .superRefine((value, context) => {
    try {
      const url = new URL(value);
      if (
        !["http:", "https:"].includes(url.protocol) ||
        value.includes("\\") ||
        value.includes("?") ||
        value.includes("#") ||
        /\s/u.test(value) ||
        url.username ||
        url.password ||
        url.search ||
        url.hash ||
        hasSecretLikeValue(value)
      ) {
        throw new Error("unsafe");
      }
    } catch {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message:
          "Base URL must be HTTP(S) without credentials, query or fragment",
      });
    }
  });
const reasoningEffortSchema = z.enum([
  "none",
  "minimal",
  "low",
  "medium",
  "high",
]);
const thinkingSchema = z
  .object({
    type: z.enum(["adaptive", "disabled", "enabled"]),
    budget_tokens: z.number().int().min(1).max(2_000_000).optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.type === "disabled" && value.budget_tokens !== undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["budget_tokens"],
        message: "Disabled thinking cannot have a token budget",
      });
    }
  });
const extraBodySchema = z
  .object({
    thinking: thinkingSchema.optional(),
    reasoning: z.object({ effort: reasoningEffortSchema }).strict().optional(),
    reasoning_format: z.literal("deepseek-style").optional(),
    chat_template_kwargs: z
      .object({
        thinking: z.boolean().optional(),
        enable_thinking: z.boolean().optional(),
      })
      .strict()
      .refine((value) => Object.keys(value).length > 0)
      .optional(),
  })
  .strict()
  .refine((value) => Object.keys(value).length > 0);

function thinkingTransitionSchema(enabled: boolean) {
  return z
    .object({
      extra_body: extraBodySchema.optional(),
      thinking: thinkingSchema.optional(),
      reasoning_effort: reasoningEffortSchema.optional(),
    })
    .strict()
    .refine((value) => Object.keys(value).length > 0)
    .superRefine((value, context) => {
      const expectedTypes = enabled
        ? new Set(["adaptive", "enabled"])
        : new Set(["disabled"]);
      const profiles = [value.thinking, value.extra_body?.thinking];
      if (
        profiles.some((profile) => profile && !expectedTypes.has(profile.type))
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Thinking transition type does not match its mode",
        });
      }
      const template = value.extra_body?.chat_template_kwargs;
      if (
        template &&
        Object.values(template).some((item) => item !== enabled)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["extra_body", "chat_template_kwargs"],
          message: "Thinking template flag does not match its mode",
        });
      }
    });
}

/**
 * Provider settings enter query and mutation caches only after this exact
 * schema proves every key, nested shape, string enum, number and endpoint.
 */
export const safeAdminModelSettingsSchema = z
  .object({
    base_url: baseUrlSchema.optional(),
    request_timeout: z.number().finite().min(0.1).max(3600).optional(),
    default_request_timeout: z.number().finite().min(0.1).max(3600).optional(),
    timeout: z.number().finite().min(0.1).max(3600).optional(),
    connect_timeout: z.number().finite().min(0.1).max(3600).optional(),
    read_timeout: z.number().finite().min(0.1).max(3600).optional(),
    write_timeout: z.number().finite().min(0.1).max(3600).optional(),
    pool_timeout: z.number().finite().min(0.1).max(3600).optional(),
    stream_chunk_timeout: z.number().finite().min(0.1).max(3600).optional(),
    max_retries: z.number().int().min(0).max(20).optional(),
    retry_max_attempts: z.number().int().min(1).max(20).optional(),
    max_tokens: z.number().int().min(1).max(2_000_000).optional(),
    prompt_cache_size: z.number().int().min(0).max(100).optional(),
    temperature: z.number().finite().min(-2).max(2).optional(),
    reasoning_effort: reasoningEffortSchema.optional(),
    extra_body: extraBodySchema.optional(),
    thinking: thinkingSchema.optional(),
    when_thinking_enabled: thinkingTransitionSchema(true).optional(),
    when_thinking_disabled: thinkingTransitionSchema(false).optional(),
    auto_thinking_budget: z.boolean().optional(),
    cumulative_stream_usage: z.boolean().optional(),
    enable_prompt_caching: z.boolean().optional(),
    use_responses_api: z.boolean().optional(),
    output_version: z.literal("responses/v1").optional(),
  })
  .strict()
  .superRefine((settings, context) => {
    if (new TextEncoder().encode(JSON.stringify(settings)).byteLength > 32768) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Model settings exceed the safe size limit",
      });
    }
  });

const credentialBindingFields = {
  credential_id: z.string().uuid().nullable(),
  credential_version_id: z.string().uuid().nullable(),
  credential_env_key: credentialEnvKeySchema.nullable(),
} as const;

function validateCredentialBinding(
  value: {
    provider_adapter: z.infer<typeof adminModelProviderAdapterSchema>;
    credential_id: string | null;
    credential_version_id: string | null;
    credential_env_key: string | null;
  },
  context: z.RefinementCtx,
): void {
  const values = [
    value.credential_id,
    value.credential_version_id,
    value.credential_env_key,
  ];
  const present = values.filter((item) => item !== null).length;
  if (present !== 0 && present !== values.length) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["credential_id"],
      message:
        "Credential id, version and environment key must be set together",
    });
    return;
  }
  const credentialRequired = ![
    "claude_code",
    "codex_cli",
    "vision_bridge_fake",
  ].includes(value.provider_adapter);
  if (
    (credentialRequired && present === 0) ||
    (!credentialRequired && present !== 0)
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["credential_id"],
      message: credentialRequired
        ? "This provider requires a Credential binding"
        : "This provider does not accept a Credential binding",
    });
  }
}

const PROVIDER_SETTING_FIELDS: Record<
  z.infer<typeof adminModelProviderAdapterSchema>,
  ReadonlySet<string>
> = {
  anthropic: new Set([
    "base_url",
    "default_request_timeout",
    "max_retries",
    "max_tokens",
    "request_timeout",
    "temperature",
    "thinking",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  claude_code: new Set([
    "auto_thinking_budget",
    "base_url",
    "default_request_timeout",
    "enable_prompt_caching",
    "max_retries",
    "max_tokens",
    "prompt_cache_size",
    "request_timeout",
    "retry_max_attempts",
    "temperature",
    "thinking",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  codex_cli: new Set(["reasoning_effort", "retry_max_attempts"]),
  deepseek: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  mindie: new Set([
    "base_url",
    "connect_timeout",
    "extra_body",
    "max_retries",
    "max_tokens",
    "pool_timeout",
    "read_timeout",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
    "write_timeout",
  ]),
  openai: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "output_version",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "use_responses_api",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  patched_deepseek: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  patched_mimo: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  patched_minimax: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  patched_openai: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "output_version",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "use_responses_api",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  patched_stepfun: new Set([
    "base_url",
    "extra_body",
    "max_retries",
    "max_tokens",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
  vision_bridge_fake: new Set(),
  vllm: new Set([
    "base_url",
    "cumulative_stream_usage",
    "extra_body",
    "max_retries",
    "max_tokens",
    "reasoning_effort",
    "request_timeout",
    "stream_chunk_timeout",
    "temperature",
    "timeout",
    "when_thinking_disabled",
    "when_thinking_enabled",
  ]),
};

function validateProviderSettings(
  value: {
    provider_adapter: z.infer<typeof adminModelProviderAdapterSchema>;
    settings: Record<string, unknown>;
  },
  context: z.RefinementCtx,
): void {
  const allowed = PROVIDER_SETTING_FIELDS[value.provider_adapter];
  for (const key of Object.keys(value.settings)) {
    if (!allowed.has(key)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["settings", key],
        message: "Setting is not supported by this provider adapter",
      });
    }
  }
}

const modelVersionFields = {
  display_name: secretSafeTextSchema(120, true),
  description: secretSafeTextSchema(4000, false),
  provider_adapter: adminModelProviderAdapterSchema,
  provider_model: providerFieldSchema,
  settings: safeAdminModelSettingsSchema,
  supports_thinking: z.boolean(),
  supports_reasoning_effort: z.boolean(),
  supports_vision: z.boolean(),
  ...credentialBindingFields,
  sort_order: z.number().int().nonnegative(),
} as const;

export const adminModelItemSchema = z
  .object({
    id: adminModelIdSchema,
    logical_name: logicalNameSchema,
    ...modelVersionFields,
    status: adminModelStatusSchema,
    is_default: z.boolean(),
    revision: z.number().int().positive(),
    version_number: z.number().int().positive(),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
    validateProviderSettings(item, context);
    if (item.is_default && item.status !== "active") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["is_default"],
        message: "The default model must be active",
      });
    }
  });

export const adminModelCatalogSchema = z
  .object({
    items: z.array(adminModelItemSchema),
    catalog_revision: z.number().int().positive(),
    request_id: z.string().min(1).max(255),
  })
  .strict()
  .superRefine((catalog, context) => {
    if (catalog.items.filter((item) => item.is_default).length > 1) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["items"],
        message: "A catalog cannot contain more than one default model",
      });
    }
    if (
      new Set(catalog.items.map((item) => item.logical_name)).size !==
      catalog.items.length
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["items"],
        message: "Model logical names must be unique",
      });
    }
  });

export const createAdminModelInputSchema = z
  .object({
    logical_name: logicalNameSchema,
    ...modelVersionFields,
    status: adminModelStatusSchema,
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
    validateProviderSettings(item, context);
  });

export const replaceAdminModelInputSchema = z
  .object({
    ...modelVersionFields,
    expected_revision: z.number().int().positive(),
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
    validateProviderSettings(item, context);
  });

export const testAdminModelConnectionInputSchema = z
  .object({
    provider_adapter: adminModelProviderAdapterSchema,
    provider_model: providerFieldSchema,
    settings: safeAdminModelSettingsSchema,
    ...credentialBindingFields,
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
    validateProviderSettings(item, context);
  });

export const adminModelStatusInputSchema = z
  .object({
    status: adminModelStatusSchema,
    expected_revision: z.number().int().positive(),
  })
  .strict();

export const adminModelDefaultInputSchema = z
  .object({
    expected_catalog_revision: z.number().int().positive(),
  })
  .strict();

export const adminModelMutationResponseSchema = z
  .object({
    item: adminModelItemSchema,
    catalog_revision: z.number().int().positive(),
    request_id: z.string().min(1).max(255),
  })
  .strict();

export const adminModelConnectionTestResponseSchema = z
  .object({
    status: z.enum(["succeeded", "failed"]),
    request_id: z.string().min(1).max(255),
  })
  .strict();

export type AdminModelItem = z.infer<typeof adminModelItemSchema>;
export type AdminModelCatalog = z.infer<typeof adminModelCatalogSchema>;
export type CreateAdminModelInput = z.infer<typeof createAdminModelInputSchema>;
export type ReplaceAdminModelInput = z.infer<
  typeof replaceAdminModelInputSchema
>;
export type TestAdminModelConnectionInput = z.infer<
  typeof testAdminModelConnectionInputSchema
>;
export type AdminModelStatusInput = z.infer<typeof adminModelStatusInputSchema>;
export type AdminModelDefaultInput = z.infer<
  typeof adminModelDefaultInputSchema
>;
export type AdminModelMutationResponse = z.infer<
  typeof adminModelMutationResponseSchema
>;
export type AdminModelConnectionTestResponse = z.infer<
  typeof adminModelConnectionTestResponseSchema
>;
