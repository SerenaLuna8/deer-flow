import { z } from "zod";

export const adminKnowledgeAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

export const adminKnowledgeModelStatusSchema = z.enum(["active", "disabled"]);
export type AdminKnowledgeModelStatus = z.infer<
  typeof adminKnowledgeModelStatusSchema
>;

export const adminKnowledgeModelItemSchema = z
  .object({
    id: z.string().uuid(),
    display_name: z.string(),
    status: adminKnowledgeModelStatusSchema,
    base_url: z.string(),
    embedding_model: z.string(),
    embedding_dimension: z.number().int(),
    embedding_max_batch: z.number().int(),
    reranker_model: z.string(),
    reranker_max_batch: z.number().int(),
    request_timeout_seconds: z.number().int(),
    in_use: z.boolean(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict();
export type AdminKnowledgeModelItem = z.infer<
  typeof adminKnowledgeModelItemSchema
>;

export const adminKnowledgeModelListSchema = z
  .object({
    items: z.array(adminKnowledgeModelItemSchema),
    total: z.number().int(),
    page: z.number().int(),
    page_size: z.number().int(),
    request_id: z.string(),
  })
  .strict();
export type AdminKnowledgeModelList = z.infer<
  typeof adminKnowledgeModelListSchema
>;

export const adminKnowledgeModelMutationSchema = z
  .object({
    item: adminKnowledgeModelItemSchema,
    request_id: z.string(),
  })
  .strict();

export const adminKnowledgeModelDeleteSchema = z
  .object({ request_id: z.string() })
  .strict();

export const adminKnowledgeModelTestSchema = z
  .object({
    ok: z.boolean(),
    message: z.string(),
    request_id: z.string(),
  })
  .strict();
export type AdminKnowledgeModelTestResult = z.infer<
  typeof adminKnowledgeModelTestSchema
>;

/** Create payload; `api_key` is write-only and must never enter query caches. */
export type CreateAdminKnowledgeModelInput = {
  display_name: string;
  base_url: string;
  embedding_model: string;
  embedding_dimension: number;
  embedding_max_batch?: number;
  reranker_model: string;
  reranker_max_batch?: number;
  request_timeout_seconds?: number;
  api_key: string;
};

/** Partial update; a blank `api_key` means "keep the stored secret". */
export type UpdateAdminKnowledgeModelInput = {
  display_name?: string;
  status?: AdminKnowledgeModelStatus;
  base_url?: string;
  embedding_model?: string;
  embedding_dimension?: number;
  embedding_max_batch?: number;
  reranker_model?: string;
  reranker_max_batch?: number;
  request_timeout_seconds?: number;
  api_key?: string;
};
