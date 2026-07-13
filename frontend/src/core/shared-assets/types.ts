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

export const assetVersionSchema = z
  .object({
    id: assetIdSchema,
    asset_id: assetIdSchema,
    version_number: z.number().int().positive(),
    workflow_status: assetWorkflowStatusSchema,
    supersedes_version_id: assetIdSchema.nullable(),
    created_at: z.string().datetime({ offset: true }),
  })
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

export const createAssetInputSchema = z
  .object({
    slug: z.string().trim().min(1),
    display_name: z.string().trim().min(1),
  })
  .strict();

export const expectedAssetVersionInputSchema = z
  .object({ expected_asset_version: z.number().int().positive() })
  .strict();

export type AssetKind = z.infer<typeof assetKindSchema>;
export type AssetListKind = z.infer<typeof assetListKindSchema>;
export type AssetScope = z.infer<typeof assetScopeSchema>;
export type AssetStatus = z.infer<typeof assetStatusSchema>;
export type AssetSummary = z.infer<typeof assetSummarySchema>;
export type AssetVersion = z.infer<typeof assetVersionSchema>;
export type CredentialMetadata = z.infer<typeof credentialMetadataSchema>;
export type SystemBinding = z.infer<typeof systemBindingSchema>;
export type ProjectAssetList = z.infer<typeof projectAssetListSchema>;
export type ProjectCredentialList = z.infer<typeof projectCredentialListSchema>;
export type AdminAssetList = z.infer<typeof adminAssetListSchema>;
export type AdminCredentialList = z.infer<typeof adminCredentialListSchema>;
export type AssetMutationResponse = z.infer<typeof assetMutationResponseSchema>;
export type CreateAssetInput = z.input<typeof createAssetInputSchema>;
export type ExpectedAssetVersionInput = z.input<
  typeof expectedAssetVersionInputSchema
>;
