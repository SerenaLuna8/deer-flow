import { z } from "zod";

export const adminModelRegistryAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

export const adminProviderModelTypeSchema = z.enum(["embedding", "rerank"]);
export type AdminProviderModelType = z.infer<
  typeof adminProviderModelTypeSchema
>;

export const adminProviderModelStatusSchema = z.enum(["active", "disabled"]);
export type AdminProviderModelStatus = z.infer<
  typeof adminProviderModelStatusSchema
>;

export const adminModelProviderItemSchema = z
  .object({
    id: z.string().uuid(),
    name: z.string(),
    base_url: z.string(),
    request_timeout_seconds: z.number().int(),
    api_key_configured: z.boolean(),
    model_count: z.number().int(),
    active_model_count: z.number().int(),
    endpoint_frozen: z.boolean(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type AdminModelProviderItem = z.infer<
  typeof adminModelProviderItemSchema
>;

export const adminModelProviderListSchema = z
  .object({
    items: z.array(adminModelProviderItemSchema),
    request_id: z.string(),
  })
  .strict();

export const adminModelProviderMutationSchema = z
  .object({
    item: adminModelProviderItemSchema,
    request_id: z.string(),
  })
  .strict();

export const adminModelProviderDeleteSchema = z
  .object({ request_id: z.string() })
  .strict();

export const adminProviderModelItemSchema = z
  .object({
    id: z.string().uuid(),
    provider_id: z.string().uuid(),
    model_type: adminProviderModelTypeSchema,
    model_name: z.string(),
    embedding_dimension: z.number().int().nullable(),
    max_batch: z.number().int(),
    status: adminProviderModelStatusSchema,
    in_use: z.boolean(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type AdminProviderModelItem = z.infer<
  typeof adminProviderModelItemSchema
>;

export const adminProviderModelListSchema = z
  .object({
    items: z.array(adminProviderModelItemSchema),
    request_id: z.string(),
  })
  .strict();

export const adminProviderModelMutationSchema = z
  .object({
    item: adminProviderModelItemSchema,
    request_id: z.string(),
  })
  .strict();

export const adminProviderModelDeleteSchema = z
  .object({ request_id: z.string() })
  .strict();

export const adminProviderModelTestSchema = z
  .object({
    ok: z.boolean(),
    message: z.string(),
    request_id: z.string(),
  })
  .strict();
export type AdminProviderModelTestResult = z.infer<
  typeof adminProviderModelTestSchema
>;

/** Create payload; `api_key` is write-only and must never enter query caches. */
export type CreateAdminModelProviderInput = {
  name: string;
  base_url: string;
  request_timeout_seconds?: number;
  api_key: string;
};

/** Partial update; an omitted `api_key` means "keep the stored secret". */
export type UpdateAdminModelProviderInput = {
  name?: string;
  base_url?: string;
  request_timeout_seconds?: number;
  api_key?: string;
};

export type CreateAdminProviderModelInput = {
  model_type: AdminProviderModelType;
  model_name: string;
  embedding_dimension?: number;
  max_batch?: number;
};
