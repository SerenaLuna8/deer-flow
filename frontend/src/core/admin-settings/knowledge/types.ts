import { z } from "zod";

export const adminKnowledgeAccountIdSchema = z.union([
  z.string().uuid(),
  z.literal("default"),
]);

const positiveInteger = z.number().int().positive().safe();
const knowledgeSettingsFields = {
  enabled: z.boolean(),
  worker_concurrency: z.number().int().min(1).max(16),
  task_timeout_seconds: z.number().int().min(30).max(7200),
  upload_max_bytes: z.number().int().min(1).max(52_428_800),
  max_knowledge_bases_per_project: positiveInteger,
  max_documents_per_knowledge_base: positiveInteger,
  max_segments_per_document: z.number().int().min(1).max(5000),
  minio_endpoint: z.string().max(512).nullable(),
  minio_bucket: z.string().max(255).nullable(),
  minio_access_key: z.string().max(512).nullable(),
  minio_secure: z.boolean(),
  summary_model_name: z.string().nullable(),
  query_cache_enabled: z.boolean(),
  query_cache_max_entries: z.number().int().min(16).max(65_536),
  query_cache_ttl_seconds: z.number().int().min(5).max(86_400),
};

export const adminKnowledgeSettingsSchema = z
  .object({
    ...knowledgeSettingsFields,
    revision: positiveInteger,
    updated_at: z.string().datetime({ offset: true }),
    secret_key_configured: z.boolean(),
    summary_model: z
      .object({
        model_name: z.string(),
        display_name: z.string(),
      })
      .strict()
      .nullable(),
    request_id: z.string(),
  })
  .strict();

function isStorageEndpoint(value: string): boolean {
  if (value.includes("://") || /[/?#@]/u.test(value)) return false;
  try {
    const url = new URL(`http://${value}`);
    return !!url.hostname && /:\d+$/u.test(value);
  } catch {
    return false;
  }
}

export const adminKnowledgeSettingsUpdateSchema = z
  .object({
    ...knowledgeSettingsFields,
    minio_endpoint: z
      .string()
      .trim()
      .min(1)
      .max(512)
      .refine(isStorageEndpoint)
      .nullable(),
    minio_bucket: z.string().trim().min(1).max(255).nullable(),
    minio_access_key: z.string().trim().min(1).max(512).nullable(),
    summary_model_name: z.string().uuid().nullable(),
    expected_revision: positiveInteger,
    minio_secret_key: z
      .string()
      .min(1)
      .max(65_536)
      .refine((value) => value.trim().length > 0)
      .nullable()
      .optional(),
  })
  .strict()
  .superRefine((value, context) => {
    if (
      value.enabled &&
      (!value.minio_endpoint || !value.minio_bucket || !value.minio_access_key)
    ) {
      context.addIssue({
        code: "custom",
        message: "Enabled knowledge requires storage configuration",
      });
    }
  });

export type AdminKnowledgeSettings = z.infer<
  typeof adminKnowledgeSettingsSchema
>;
export type AdminKnowledgeSettingsUpdate = z.infer<
  typeof adminKnowledgeSettingsUpdateSchema
>;
export type AdminKnowledgeSettingsDraft = Omit<
  AdminKnowledgeSettingsUpdate,
  "expected_revision" | "minio_secret_key"
>;

/** Only non-secret, writable fields enter the editable draft. */
export function knowledgeSettingsDraft(
  settings: AdminKnowledgeSettings,
): AdminKnowledgeSettingsDraft {
  return {
    enabled: settings.enabled,
    worker_concurrency: settings.worker_concurrency,
    task_timeout_seconds: settings.task_timeout_seconds,
    upload_max_bytes: settings.upload_max_bytes,
    max_knowledge_bases_per_project: settings.max_knowledge_bases_per_project,
    max_documents_per_knowledge_base: settings.max_documents_per_knowledge_base,
    max_segments_per_document: settings.max_segments_per_document,
    minio_endpoint: settings.minio_endpoint,
    minio_bucket: settings.minio_bucket,
    minio_access_key: settings.minio_access_key,
    minio_secure: settings.minio_secure,
    summary_model_name: settings.summary_model_name,
    query_cache_enabled: settings.query_cache_enabled,
    query_cache_max_entries: settings.query_cache_max_entries,
    query_cache_ttl_seconds: settings.query_cache_ttl_seconds,
  };
}
