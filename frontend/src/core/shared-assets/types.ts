import { z } from "zod";

export const ASSET_KINDS = ["agent", "skill", "mcp"] as const;
export const ASSET_LIST_KINDS = [
  "agents",
  "skills",
  "mcp-servers",
  "credentials",
] as const;
export const ASSET_STATUSES = ["active", "archived", "suspended"] as const;
export const ASSET_WORKFLOW_STATUSES = [
  "draft",
  "pending_approval",
  "published",
  "rejected",
] as const;

export const assetKindSchema = z.enum(ASSET_KINDS);
export const assetListKindSchema = z.enum(ASSET_LIST_KINDS);
export const assetScopeSchema = z.enum(["system", "project"]);
export const assetStatusSchema = z.enum(ASSET_STATUSES);
export const assetWorkflowStatusSchema = z.enum(ASSET_WORKFLOW_STATUSES);
export const assetIdSchema = z.string().uuid();
export const assetCapabilitiesSchema = z.array(z.string().min(1));

export const assetSummarySchema = z
  .object({
    id: assetIdSchema,
    scope: assetScopeSchema,
    project_id: assetIdSchema.nullable(),
    slug: z.string().min(1),
    display_name: z.string().min(1),
    status: assetStatusSchema,
    current_published_version_id: assetIdSchema.nullable(),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

const FORBIDDEN_RESPONSE_FIELDS = new Set([
  "plaintext",
  "ciphertext",
  "nonce",
  "key_id",
  "storage_locator",
  "secret_hash",
]);

function isSafeJsonValue(value: unknown): boolean {
  if (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean"
  ) {
    return true;
  }
  if (typeof value === "number") return Number.isFinite(value);
  if (Array.isArray(value)) return value.every(isSafeJsonValue);
  if (typeof value !== "object") return false;
  return Object.entries(value).every(
    ([key, item]) =>
      !FORBIDDEN_RESPONSE_FIELDS.has(key) && isSafeJsonValue(item),
  );
}

function isStringMap(value: unknown): value is Record<string, string> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every((item) => typeof item === "string")
  );
}

function isStringListMap(value: unknown): value is Record<string, string[]> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    Object.values(value).every(
      (item) =>
        Array.isArray(item) &&
        item.every((member) => typeof member === "string"),
    )
  );
}

const safeJsonObjectSchema = z.custom<Record<string, unknown>>(
  (value) =>
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value) &&
    isSafeJsonValue(value),
  "Expected a safe JSON object",
);
const stringMapSchema = z.custom<Record<string, string>>(isStringMap);
const stringListMapSchema = z.custom<Record<string, string[]>>(isStringListMap);

export const agentVersionSchema = z
  .object({
    id: assetIdSchema,
    agent_id: assetIdSchema,
    version_number: z.number().int().positive(),
    workflow_status: assetWorkflowStatusSchema,
    description: z.string(),
    soul: z.string(),
    model_ref: z.string(),
    tool_groups: z.array(z.string()),
    skill_version_ids: z.array(assetIdSchema),
    mcp_version_ids: z.array(assetIdSchema),
    supersedes_version_id: assetIdSchema.nullable(),
    payload_checksum: z.string().min(1),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

const skillSecretRequirementSchema = z
  .object({ name: z.string().min(1), optional: z.boolean() })
  .strict();
const skillFileViewSchema = z
  .object({
    path: z.string().min(1),
    media_type: z.string().min(1),
    size_bytes: z.number().int().nonnegative(),
    sha256: z.string().min(1),
  })
  .strict();

export const skillVersionSchema = z
  .object({
    id: assetIdSchema,
    skill_id: assetIdSchema,
    version_number: z.number().int().positive(),
    workflow_status: assetWorkflowStatusSchema,
    description: z.string(),
    frontmatter: safeJsonObjectSchema,
    compatibility: z.string().nullable(),
    secret_requirements: z.array(skillSecretRequirementSchema),
    scan_decision: z.string().min(1),
    scan_rule_ids: z.array(z.string()),
    scan_summary: safeJsonObjectSchema,
    file_views: z.array(skillFileViewSchema),
    supersedes_version_id: assetIdSchema.nullable(),
    payload_checksum: z.string().min(1),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

const mcpCredentialSlotSchema = z
  .object({
    id: assetIdSchema,
    name: z.string().min(1),
    purpose: z.string(),
    payload_schema: stringListMapSchema,
    required: z.boolean(),
  })
  .strict();
const mcpDefinitionSlotSchema = z
  .object({
    name: z.string().min(1),
    purpose: z.string(),
    payload_schema: safeJsonObjectSchema,
    required: z.boolean(),
  })
  .strict();
const credentialGrantSchema = z
  .object({
    id: assetIdSchema,
    mcp_server_version_id: assetIdSchema,
    credential_slot_id: assetIdSchema,
    credential_version_id: assetIdSchema,
    status: z.string().min(1),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();
const mcpDefinitionSchema = z
  .object({
    description: z.string(),
    transport: z.string().min(1),
    command: z.string().nullable(),
    args: z.array(z.string()),
    url: z.string().nullable(),
    env: stringMapSchema,
    headers: stringMapSchema,
    oauth: safeJsonObjectSchema,
    routing: safeJsonObjectSchema,
    tool_overrides: safeJsonObjectSchema,
    timeout_seconds: z.number().int().positive(),
    credential_slots: z.array(mcpDefinitionSlotSchema),
  })
  .strict();

export const mcpVersionSchema = z
  .object({
    id: assetIdSchema,
    mcp_server_id: assetIdSchema,
    version_number: z.number().int().positive(),
    workflow_status: assetWorkflowStatusSchema,
    definition: mcpDefinitionSchema,
    credential_slots: z.array(mcpCredentialSlotSchema),
    credential_grants: z.array(credentialGrantSchema),
    supersedes_version_id: assetIdSchema.nullable(),
    payload_checksum: z.string().min(1),
    submitted_at: z.string().datetime({ offset: true }).nullable(),
    reviewed_at: z.string().datetime({ offset: true }).nullable(),
    reviewed_by_user_id: z.string().min(1).nullable(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const credentialVersionSchema = z
  .object({
    id: assetIdSchema,
    credential_id: assetIdSchema,
    version_number: z.number().int().positive(),
    status: z.enum(["active", "retired", "revoked"]),
    payload_schema_version: z.number().int().positive(),
    payload_schema: stringListMapSchema,
    supersedes_version_id: assetIdSchema.nullable(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const assetVersionSchema = z.union([
  agentVersionSchema,
  skillVersionSchema,
  mcpVersionSchema,
  credentialVersionSchema,
]);

export const agentVersionResponseSchema = z
  .object({ data: agentVersionSchema, request_id: z.string().min(1) })
  .strict();
export const skillVersionResponseSchema = z
  .object({ data: skillVersionSchema, request_id: z.string().min(1) })
  .strict();
export const mcpVersionResponseSchema = z
  .object({ data: mcpVersionSchema, request_id: z.string().min(1) })
  .strict();
export const credentialVersionResponseSchema = z
  .object({ data: credentialVersionSchema, request_id: z.string().min(1) })
  .strict();
export const versionResponseSchema = z
  .object({ data: assetVersionSchema, request_id: z.string().min(1) })
  .strict();
export const agentVersionHistoryResponseSchema = z
  .object({ data: z.array(agentVersionSchema), request_id: z.string().min(1) })
  .strict();
export const skillVersionHistoryResponseSchema = z
  .object({ data: z.array(skillVersionSchema), request_id: z.string().min(1) })
  .strict();
export const mcpVersionHistoryResponseSchema = z
  .object({ data: z.array(mcpVersionSchema), request_id: z.string().min(1) })
  .strict();
export const credentialVersionHistoryResponseSchema = z
  .object({
    data: z.array(credentialVersionSchema),
    request_id: z.string().min(1),
  })
  .strict();
export const versionHistoryResponseSchema = z
  .object({ data: z.array(assetVersionSchema), request_id: z.string().min(1) })
  .strict();

export const credentialMetadataSchema = z
  .object({
    id: assetIdSchema,
    scope: assetScopeSchema,
    project_id: assetIdSchema.nullable(),
    name: z.string().min(1),
    display_name: z.string().min(1),
    credential_type: z.string().min(1),
    status: z.enum(["active", "revoked"]),
    current_version_id: assetIdSchema.nullable(),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

export const systemBindingSchema = z
  .object({
    project_id: assetIdSchema,
    kind: assetKindSchema,
    asset_id: assetIdSchema,
    version_id: assetIdSchema,
    enabled: z.boolean(),
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    updated_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
    request_id: z.string().min(1),
  })
  .strict();

export const projectAssetListSchema = z
  .object({
    system_items: z.array(assetSummarySchema),
    project_items: z.array(assetSummarySchema),
    request_id: z.string().min(1),
  })
  .strict();

export const projectCredentialListSchema = z
  .object({
    system_items: z.array(credentialMetadataSchema),
    project_items: z.array(credentialMetadataSchema),
    request_id: z.string().min(1),
  })
  .strict();

export const adminAssetListSchema = z
  .object({
    items: z.array(assetSummarySchema),
    request_id: z.string().min(1),
  })
  .strict();

export const adminCredentialListSchema = z
  .object({
    items: z.array(credentialMetadataSchema),
    request_id: z.string().min(1),
  })
  .strict();

export const assetMutationResponseSchema = z
  .object({
    item: assetSummarySchema,
    request_id: z.string().min(1),
  })
  .strict();

export const credentialMutationResponseSchema = z
  .object({
    item: credentialMetadataSchema,
    request_id: z.string().min(1),
  })
  .strict();

export const createAssetInputSchema = z
  .object({
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
  })
  .strict();

export const expectedAssetVersionInputSchema = z
  .object({ expected_asset_version: z.number().int().positive() })
  .strict();

export const agentVersionInputSchema = z
  .object({
    description: z.string().default(""),
    soul: z.string().default(""),
    model_ref: z.string().default(""),
    tool_groups: z.array(z.string()).default([]),
    skill_version_ids: z.array(assetIdSchema).default([]),
    mcp_version_ids: z.array(assetIdSchema).default([]),
    expected_asset_version: z.number().int().positive(),
  })
  .strict();
export const skillVersionInputSchema = z
  .object({
    files: z
      .array(
        z
          .object({
            path: z.string().min(1),
            content_base64: z.string().min(1),
            media_type: z.string().min(1).default("application/octet-stream"),
          })
          .strict(),
      )
      .min(1),
    expected_asset_version: z.number().int().positive(),
  })
  .strict();
export const mcpVersionInputSchema = z
  .object({
    description: z.string().default(""),
    transport: z.string().min(1).default("stdio"),
    command: z.string().nullable().default(null),
    args: z.array(z.string()).default([]),
    url: z.string().nullable().default(null),
    env: stringMapSchema.default({}),
    headers: stringMapSchema.default({}),
    oauth: safeJsonObjectSchema.default({}),
    routing: safeJsonObjectSchema.default({}),
    tool_overrides: safeJsonObjectSchema.default({}),
    timeout_seconds: z.number().int().positive().default(30),
    credential_slots: z
      .array(
        z
          .object({
            name: z.string().min(1),
            purpose: z.string().default(""),
            payload_schema: stringListMapSchema,
            required: z.boolean().default(true),
          })
          .strict(),
      )
      .default([]),
    expected_asset_version: z.number().int().positive(),
  })
  .strict();

export const credentialPayloadSchema = z
  .object({
    env: stringMapSchema.optional(),
    headers: stringMapSchema.optional(),
    oauth: stringMapSchema.optional(),
  })
  .strict()
  .refine((value) => Object.keys(value).length > 0);

export const createCredentialInputSchema = z
  .object({
    name: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
    credential_type: z.string().trim().min(1),
    payload: credentialPayloadSchema,
  })
  .strict();

export const replaceCredentialInputSchema = z
  .object({
    payload: credentialPayloadSchema,
    expected_credential_version: z.number().int().positive(),
  })
  .strict();
export const revokeCredentialInputSchema = z
  .object({ expected_credential_version: z.number().int().positive() })
  .strict();
export const approveMcpInputSchema = z
  .object({
    credential_versions: z.custom<Record<string, string>>(
      (value) =>
        isStringMap(value) &&
        Object.values(value).every(
          (item) => assetIdSchema.safeParse(item).success,
        ),
    ),
    expected_asset_version: z.number().int().positive(),
  })
  .strict();
export const enableSystemBindingInputSchema = z
  .object({
    asset_id: assetIdSchema,
    version_id: assetIdSchema,
    expected_binding_version: z.number().int().positive().optional(),
  })
  .strict();
export const moveSystemBindingInputSchema = z
  .object({
    version_id: assetIdSchema,
    expected_binding_version: z.number().int().positive(),
  })
  .strict();
export const disableSystemBindingInputSchema = z
  .object({ expected_binding_version: z.number().int().positive() })
  .strict();

export type AssetKind = z.infer<typeof assetKindSchema>;
export type AssetListKind = z.infer<typeof assetListKindSchema>;
export type AssetScope = z.infer<typeof assetScopeSchema>;
export type AssetStatus = z.infer<typeof assetStatusSchema>;
export type AssetSummary = z.infer<typeof assetSummarySchema>;
export type AssetVersion = z.infer<typeof assetVersionSchema>;
export type VersionResponse = z.infer<typeof versionResponseSchema>;
export type VersionHistoryResponse = z.infer<
  typeof versionHistoryResponseSchema
>;
export type CredentialMetadata = z.infer<typeof credentialMetadataSchema>;
export type SystemBinding = z.infer<typeof systemBindingSchema>;
export type ProjectAssetList = z.infer<typeof projectAssetListSchema>;
export type ProjectCredentialList = z.infer<typeof projectCredentialListSchema>;
export type AdminAssetList = z.infer<typeof adminAssetListSchema>;
export type AdminCredentialList = z.infer<typeof adminCredentialListSchema>;
export type AssetMutationResponse = z.infer<typeof assetMutationResponseSchema>;
export type CredentialMutationResponse = z.infer<
  typeof credentialMutationResponseSchema
>;
export type CreateAssetInput = z.input<typeof createAssetInputSchema>;
export type AgentVersionInput = z.input<typeof agentVersionInputSchema>;
export type SkillVersionInput = z.input<typeof skillVersionInputSchema>;
export type McpVersionInput = z.input<typeof mcpVersionInputSchema>;
export type CreateCredentialInput = z.input<typeof createCredentialInputSchema>;
export type ExpectedAssetVersionInput = z.input<
  typeof expectedAssetVersionInputSchema
>;
export type ReplaceCredentialInput = z.input<
  typeof replaceCredentialInputSchema
>;
export type RevokeCredentialInput = z.input<typeof revokeCredentialInputSchema>;
export type ApproveMcpInput = z.input<typeof approveMcpInputSchema>;
export type EnableSystemBindingInput = z.input<
  typeof enableSystemBindingInputSchema
>;
export type MoveSystemBindingInput = z.input<
  typeof moveSystemBindingInputSchema
>;
export type DisableSystemBindingInput = z.input<
  typeof disableSystemBindingInputSchema
>;
