import { z } from "zod";

export const accountPersonalizationAccountIdSchema = z.string().uuid();

const safePositiveInteger = z.number().int().positive().safe();
const safeNonnegativeInteger = z.number().int().nonnegative().safe();

export const accountPersonalizationSchema = z
  .object({
    memoryEnabled: z.boolean(),
    effectiveMemoryEnabled: z.boolean(),
    platformMemoryAvailable: z.boolean(),
    version: safePositiveInteger,
  })
  .strict();

export const updateAccountPersonalizationInputSchema = z
  .object({
    memoryEnabled: z.boolean(),
    expectedVersion: safePositiveInteger,
  })
  .strict();

export const resetAccountMemoryInputSchema = z
  .object({
    confirm: z.literal(true),
    expectedVersion: safePositiveInteger,
  })
  .strict();

export const resetAccountMemoryResultSchema = z
  .object({
    version: safePositiveInteger,
    scopesReset: safeNonnegativeInteger,
    v1Memories: safeNonnegativeInteger,
    sourceBatches: safeNonnegativeInteger,
    candidates: safeNonnegativeInteger,
    facts: safeNonnegativeInteger,
    snapshots: safeNonnegativeInteger,
    jobsCancelled: safeNonnegativeInteger,
  })
  .strict();

export type AccountPersonalization = z.infer<
  typeof accountPersonalizationSchema
>;
export type UpdateAccountPersonalizationInput = z.infer<
  typeof updateAccountPersonalizationInputSchema
>;
export type ResetAccountMemoryInput = z.infer<
  typeof resetAccountMemoryInputSchema
>;
export type ResetAccountMemoryResult = z.infer<
  typeof resetAccountMemoryResultSchema
>;
