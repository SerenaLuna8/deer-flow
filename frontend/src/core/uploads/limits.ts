import { z } from "zod";

export const uploadProjectStorageSchema = z
  .object({
    policy: z.literal("project_quota"),
    remaining_bytes: z.number().int().nonnegative(),
  })
  .strict();

export const uploadLimitsSchema = z
  .object({
    max_files: z.number().int().positive(),
    max_file_size: z.number().int().positive(),
    max_total_size: z.number().int().positive(),
    project_storage: uploadProjectStorageSchema,
    request_id: z.string().min(1),
  })
  .strict();

export type UploadLimits = z.infer<typeof uploadLimitsSchema>;
