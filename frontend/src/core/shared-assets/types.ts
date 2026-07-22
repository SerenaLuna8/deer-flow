import { z } from "zod";

import { capabilitySchema } from "@/core/projects/types";

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
export const SKILL_SCAN_DECISIONS = ["allow", "warn", "block"] as const;
export const SKILL_FILE_PREVIEW_STATUSES = [
  "ready",
  "binary",
  "too_large",
] as const;
export const MCP_TRANSPORTS = [
  "stdio",
  "sse",
  "http",
  "streamable_http",
] as const;
export const CREDENTIAL_GRANT_STATUSES = ["active", "revoked"] as const;
export const CREDENTIAL_PAYLOAD_GROUPS = ["env", "headers", "oauth"] as const;

export const assetKindSchema = z.enum(ASSET_KINDS);
export const assetListKindSchema = z.enum(ASSET_LIST_KINDS);
export const assetScopeSchema = z.enum(["system", "project"]);
export const assetStatusSchema = z.enum(ASSET_STATUSES);
export const assetWorkflowStatusSchema = z.enum(ASSET_WORKFLOW_STATUSES);
export const skillScanDecisionSchema = z.enum(SKILL_SCAN_DECISIONS);
export const skillFilePreviewStatusSchema = z.enum(SKILL_FILE_PREVIEW_STATUSES);
export const mcpTransportSchema = z.enum(MCP_TRANSPORTS);
export const credentialGrantStatusSchema = z.enum(CREDENTIAL_GRANT_STATUSES);
export const credentialPayloadGroupSchema = z.enum(CREDENTIAL_PAYLOAD_GROUPS);
export const assetIdSchema = z.string().uuid();
export const assetCapabilitiesSchema = z.array(capabilitySchema);

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
const credentialFieldNameSchema = z.string().min(1).max(255);
const credentialFieldMapSchema = z
  .record(z.string())
  .superRefine((value, context) => {
    const fields = Object.keys(value);
    if (fields.length === 0) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Credential payload groups cannot be empty",
      });
    }
    for (const fieldName of fields) {
      if (!credentialFieldNameSchema.safeParse(fieldName).success) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Credential field names must contain 1 to 255 characters",
          path: [fieldName],
        });
      }
    }
  });
const credentialFieldListSchema = z
  .array(credentialFieldNameSchema)
  .min(1)
  .refine((fields) => new Set(fields).size === fields.length, {
    message: "Credential field names must be unique within a group",
  });
const credentialPayloadStructureSchema = z
  .object({
    env: credentialFieldListSchema.optional(),
    headers: credentialFieldListSchema.optional(),
    oauth: credentialFieldListSchema.optional(),
  })
  .strict()
  .refine((value) => Object.keys(value).length > 0, {
    message: "Credential payload schema cannot be empty",
  });

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

export const skillFilePathSchema = z
  .string()
  .min(1)
  .max(512)
  .refine((path) => path === path.normalize("NFC"), {
    message: "Skill file paths must use canonical NFC",
  })
  .refine(
    (path) =>
      !path.startsWith("/") &&
      !path.includes("\\") &&
      !path.includes("\0") &&
      path
        .split("/")
        .every((part) => part !== "" && part !== "." && part !== ".."),
    { message: "Skill file paths must be relative POSIX paths" },
  );

export const skillVersionFileContentSchema = z
  .object({
    path: skillFilePathSchema,
    media_type: z.string().min(1),
    size_bytes: z.number().int().nonnegative(),
    sha256: z.string().min(1),
    preview_status: skillFilePreviewStatusSchema,
    encoding: z.literal("utf-8").nullable(),
    content: z.string().nullable(),
    source_payload_checksum: z.string().min(1),
    asset_version: z.number().int().positive(),
  })
  .strict()
  .superRefine((value, context) => {
    const ready = value.preview_status === "ready";
    if (ready && (value.encoding !== "utf-8" || value.content === null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Ready Skill files require UTF-8 content",
      });
    }
    if (!ready && (value.encoding !== null || value.content !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Unavailable Skill files cannot include content",
      });
    }
  });

export const skillVersionFileContentResponseSchema = z
  .object({
    data: skillVersionFileContentSchema,
    request_id: z.string().min(1),
  })
  .strict();

const editableSkillFileChangeSchema = z
  .object({
    op: z.enum(["create", "replace"]),
    path: skillFilePathSchema,
    content: z.string().max(1024 * 1024),
    media_type: z.string().min(1).max(255),
  })
  .strict();
const deletedSkillFileChangeSchema = z
  .object({
    op: z.literal("delete"),
    path: skillFilePathSchema.refine((path) => path !== "SKILL.md", {
      message: "SKILL.md cannot be deleted",
    }),
  })
  .strict();

export const skillFileChangeSchema = z.union([
  editableSkillFileChangeSchema,
  deletedSkillFileChangeSchema,
]);
export const skillFileForkInputSchema = z
  .object({
    expected_asset_version: z.number().int().positive(),
    expected_source_payload_checksum: z.string().min(1),
    changes: z.array(skillFileChangeSchema).min(1).max(256),
  })
  .strict()
  .refine(
    (value) =>
      new Set(value.changes.map((change) => change.path)).size ===
      value.changes.length,
    { message: "Skill file changes must use unique paths", path: ["changes"] },
  );

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
    scan_decision: skillScanDecisionSchema,
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
    payload_schema: stringListMapSchema,
    required: z.boolean(),
  })
  .strict();
const credentialGrantSchema = z
  .object({
    id: assetIdSchema,
    mcp_server_version_id: assetIdSchema,
    credential_slot_id: assetIdSchema,
    credential_version_id: assetIdSchema,
    status: credentialGrantStatusSchema,
    version: z.number().int().positive(),
    created_by_user_id: z.string().min(1),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict();
const mcpDefinitionSchema = z
  .object({
    description: z.string(),
    transport: mcpTransportSchema,
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
    payload_schema: credentialPayloadStructureSchema,
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

export const credentialRotationStatusSchema = z
  .object({
    eligible_total: z.number().int().nonnegative(),
    current: z.number().int().nonnegative(),
    pending: z.number().int().nonnegative(),
    status: z.enum(["current", "pending"]),
  })
  .strict()
  .refine(
    (value) =>
      value.current + value.pending === value.eligible_total &&
      (value.status === "pending" ? value.pending > 0 : value.pending === 0),
  );

const systemBindingItemSchema = z
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
  })
  .strict();

export const systemBindingSchema = systemBindingItemSchema
  .extend({ request_id: z.string().min(1) })
  .strict();

export const projectAssetItemSchema = assetSummarySchema
  .extend({
    capabilities: assetCapabilitiesSchema,
    binding: systemBindingItemSchema.nullable(),
  })
  .strict();

export const projectCredentialItemSchema = credentialMetadataSchema
  .extend({ capabilities: assetCapabilitiesSchema })
  .strict();

export const projectAssetListSchema = z
  .object({
    system_items: z.array(projectAssetItemSchema),
    project_items: z.array(projectAssetItemSchema),
    request_id: z.string().min(1),
  })
  .strict();

export const projectCredentialListSchema = z
  .object({
    system_items: z.array(projectCredentialItemSchema),
    project_items: z.array(projectCredentialItemSchema),
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
    transport: mcpTransportSchema.default("stdio"),
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
    env: credentialFieldMapSchema.optional(),
    headers: credentialFieldMapSchema.optional(),
    oauth: credentialFieldMapSchema.optional(),
  })
  .strict()
  .refine((value) => Object.keys(value).length > 0, {
    message: "Credential payload cannot be empty",
  });

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
export type CredentialPayloadGroup = z.infer<
  typeof credentialPayloadGroupSchema
>;
export type CredentialPayload = z.infer<typeof credentialPayloadSchema>;
export type AssetSummary = z.infer<typeof assetSummarySchema>;
export type ProjectAssetItem = z.infer<typeof projectAssetItemSchema>;
export type ProjectCredentialItem = z.infer<typeof projectCredentialItemSchema>;
export type AssetVersion = z.infer<typeof assetVersionSchema>;
export type VersionResponse = z.infer<typeof versionResponseSchema>;
export type VersionHistoryResponse = z.infer<
  typeof versionHistoryResponseSchema
>;
export type CredentialMetadata = z.infer<typeof credentialMetadataSchema>;
export type CredentialRotationStatus = z.infer<
  typeof credentialRotationStatusSchema
>;
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
export type SkillVersionFileContent = z.infer<
  typeof skillVersionFileContentSchema
>;
export type SkillVersionFileContentResponse = z.infer<
  typeof skillVersionFileContentResponseSchema
>;
export type SkillFileChange = z.input<typeof skillFileChangeSchema>;
export type SkillFileForkInput = z.input<typeof skillFileForkInputSchema>;
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
