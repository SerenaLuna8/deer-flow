import { z } from "zod";

export const adminModelAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

export const adminModelIdSchema = z.string().uuid();
export const adminModelStatusSchema = z.enum(["active", "suspended"]);
// Adapter identity comes from the backend registry. The browser constrains the
// wire shape only; catalog descriptor membership decides authorability.
export const adminModelProviderAdapterSchema = z
  .string()
  .trim()
  .min(1)
  .max(64)
  .regex(/^[a-z][a-z0-9_]*$/u);
const readableAdminModelProviderAdapterSchema = adminModelProviderAdapterSchema;

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
const settingKeySchema = z
  .string()
  .min(1)
  .max(64)
  .regex(/^[a-z][a-z0-9_]*$/u);
const SECRET_SETTING_KEY =
  /(^|_)(api_key|apikey|access_key|private_key|client_secret|refresh_token|secret|token|password|passwd|credential|credentials|auth|authorization|bearer|cookie)(_|$)/iu;

function validateGenericSettingValue(
  value: unknown,
  context: z.RefinementCtx,
  path: (string | number)[],
  depth: number,
): void {
  if (depth > 8) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path,
      message: "Model setting nesting is too deep",
    });
    return;
  }
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path,
        message: "Model setting numbers must be finite",
      });
    }
    return;
  }
  if (typeof value === "string") {
    if (value.length > 8_192 || hasSecretLikeValue(value)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path,
        message: "Model setting text is unsafe",
      });
    }
    return;
  }
  if (Array.isArray(value)) {
    if (value.length > 128) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path,
        message: "Model setting arrays are too large",
      });
      return;
    }
    value.forEach((item, index) =>
      validateGenericSettingValue(item, context, [...path, index], depth + 1),
    );
    return;
  }
  if (typeof value !== "object") {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path,
      message: "Model setting values must be JSON",
    });
    return;
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length > 128) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path,
      message: "Model setting objects are too large",
    });
    return;
  }
  for (const [key, item] of entries) {
    if (
      !settingKeySchema.safeParse(key).success ||
      SECRET_SETTING_KEY.test(key)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: [...path, key],
        message: "Model setting key is unsafe",
      });
      continue;
    }
    validateGenericSettingValue(item, context, [...path, key], depth + 1);
  }
}

function validateAdminModelSettingsSize(
  settings: Record<string, unknown>,
  context: z.RefinementCtx,
): void {
  if (new TextEncoder().encode(JSON.stringify(settings)).byteLength > 32768) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "Model settings exceed the safe size limit",
    });
  }
}

const genericAdminModelSettingsSchema: z.ZodType<
  Record<string, AdminModelSettingValue>,
  z.ZodTypeDef,
  Record<string, unknown>
> = z
  .record(z.unknown())
  .superRefine((settings, context) => {
    validateGenericSettingValue(settings, context, [], 0);
    validateAdminModelSettingsSize(settings, context);
  })
  .transform((settings) => settings as Record<string, AdminModelSettingValue>);

/** Generic mutation boundary; the selected descriptor adds field authority. */
export const safeAdminModelSettingsSchema =
  genericAdminModelSettingsSchema.superRefine((settings, context) => {
    if (Object.hasOwn(settings, "max_retries")) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["max_retries"],
        message: "Retry policy is runtime-owned",
      });
    }
  });

// Read compatibility only: old rows may contain the now runtime-owned retry
// count, but every new mutation strips and rejects it.
const readableAdminModelSettingsSchema = genericAdminModelSettingsSchema;

const credentialBindingFields = {
  credential_id: z.string().uuid().nullable(),
  credential_version_id: z.string().uuid().nullable(),
  credential_env_key: credentialEnvKeySchema.nullable(),
} as const;

function validateCredentialBinding(
  value: {
    provider_adapter: z.infer<typeof readableAdminModelProviderAdapterSchema>;
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
}

export const adminModelProviderSettingFieldSchema = z
  .object({
    name: settingKeySchema,
    label: secretSafeTextSchema(120, true),
    input_type: z.enum([
      "boolean",
      "enum",
      "integer",
      "json",
      "number",
      "string",
      "url",
    ]),
    advanced: z.boolean(),
    minimum: z.number().finite().nullable(),
    maximum: z.number().finite().nullable(),
    step: z.number().finite().positive().nullable(),
    options: z.array(secretSafeTextSchema(255, true)).max(64),
  })
  .strict()
  .superRefine((field, context) => {
    const numeric =
      field.input_type === "integer" || field.input_type === "number";
    if (
      !numeric &&
      (field.minimum !== null || field.maximum !== null || field.step !== null)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Only numeric fields may declare numeric constraints",
      });
    }
    if (
      field.minimum !== null &&
      field.maximum !== null &&
      field.minimum > field.maximum
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Provider setting minimum exceeds maximum",
      });
    }
    if (
      field.input_type === "integer" &&
      [field.minimum, field.maximum, field.step].some(
        (value) => value !== null && !Number.isInteger(value),
      )
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Integer fields require integer constraints",
      });
    }
    if (field.input_type === "enum") {
      if (
        field.options.length === 0 ||
        new Set(field.options).size !== field.options.length
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["options"],
          message: "Enum fields require unique options",
        });
      }
    } else if (field.options.length !== 0) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["options"],
        message: "Only enum fields may declare options",
      });
    }
    if (!field.advanced && field.input_type === "json") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["advanced"],
        message: "JSON fields must use the advanced editor",
      });
    }
  });

export const adminModelProviderAdapterDescriptorSchema = z
  .object({
    id: adminModelProviderAdapterSchema,
    credential_required: z.boolean(),
    setting_fields: z.array(adminModelProviderSettingFieldSchema).max(64),
  })
  .strict()
  .superRefine((descriptor, context) => {
    const fieldNames = descriptor.setting_fields.map((field) => field.name);
    if (new Set(fieldNames).size !== fieldNames.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["setting_fields"],
        message: "Provider setting fields must be unique",
      });
    }
  });

function validateDescriptorSettingValue(
  field: z.infer<typeof adminModelProviderSettingFieldSchema>,
  value: AdminModelSettingValue,
  context: z.RefinementCtx,
): void {
  if (field.input_type === "json") return;
  if (field.input_type === "url") {
    const parsed = baseUrlSchema.safeParse(value);
    if (!parsed.success) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: [field.name],
        message: "Provider setting must be a safe URL",
      });
    }
    return;
  }
  if (field.input_type === "string") {
    if (
      typeof value !== "string" ||
      value.length === 0 ||
      value.length > 2_048 ||
      hasSecretLikeValue(value)
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: [field.name],
        message: "Provider setting must be safe text",
      });
    }
    return;
  }
  if (field.input_type === "boolean") {
    if (typeof value !== "boolean") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: [field.name],
        message: "Provider setting must be boolean",
      });
    }
    return;
  }
  if (field.input_type === "enum") {
    if (typeof value !== "string" || !field.options.includes(value)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: [field.name],
        message: "Provider setting must use a declared option",
      });
    }
    return;
  }
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    (field.input_type === "integer" && !Number.isInteger(value)) ||
    (field.minimum !== null && value < field.minimum) ||
    (field.maximum !== null && value > field.maximum)
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: [field.name],
      message: "Provider setting violates numeric constraints",
    });
  }
}

export function adminModelSettingsSchemaForProvider(
  descriptor: z.infer<typeof adminModelProviderAdapterDescriptorSchema>,
) {
  const fields = new Map(
    descriptor.setting_fields.map((field) => [field.name, field] as const),
  );
  return safeAdminModelSettingsSchema.superRefine((settings, context) => {
    for (const [key, value] of Object.entries(settings)) {
      const field = fields.get(key);
      if (!field) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: [key],
          message: "Provider setting is not declared by its adapter",
        });
        continue;
      }
      validateDescriptorSettingValue(field, value, context);
    }
  });
}

const modelVersionFields = {
  display_name: secretSafeTextSchema(120, true),
  provider_adapter: readableAdminModelProviderAdapterSchema,
  provider_model: providerFieldSchema,
  settings: readableAdminModelSettingsSchema,
  supports_thinking: z.boolean(),
  supports_reasoning_effort: z.boolean(),
  supports_vision: z.boolean(),
  ...credentialBindingFields,
} as const;

const writableModelVersionFields = {
  ...modelVersionFields,
  provider_adapter: adminModelProviderAdapterSchema,
  settings: safeAdminModelSettingsSchema,
} as const;

export const adminModelItemSchema = z
  .object({
    id: adminModelIdSchema,
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
    provider_adapters: z.array(adminModelProviderAdapterDescriptorSchema),
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
    const adapterIds = catalog.provider_adapters.map((adapter) => adapter.id);
    if (new Set(adapterIds).size !== adapterIds.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["provider_adapters"],
        message: "Provider adapter descriptors must be unique",
      });
    }
  });

export const createAdminModelInputSchema = z
  .object({
    ...writableModelVersionFields,
    status: adminModelStatusSchema,
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
  });

export const replaceAdminModelInputSchema = z
  .object({
    ...writableModelVersionFields,
    expected_revision: z.number().int().positive(),
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
  });

export const testAdminModelConnectionInputSchema = z
  .object({
    provider_adapter: adminModelProviderAdapterSchema,
    provider_model: providerFieldSchema,
    settings: safeAdminModelSettingsSchema,
    supports_vision: z.boolean(),
    ...credentialBindingFields,
  })
  .strict()
  .superRefine((item, context) => {
    validateCredentialBinding(item, context);
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
export type AdminModelProviderSettingField = z.infer<
  typeof adminModelProviderSettingFieldSchema
>;
export type AdminModelProviderAdapterDescriptor = z.infer<
  typeof adminModelProviderAdapterDescriptorSchema
>;
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
