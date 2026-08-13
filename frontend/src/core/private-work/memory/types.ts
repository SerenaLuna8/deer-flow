import type { z } from "zod";

import type { PrivateWorkAccess } from "../types";

import type {
  memoryDocumentSchema,
  memoryDreamInputSchema,
  memoryDreamPreparationAdmissionSchema,
  memoryDreamPreparationInputSchema,
  memoryDreamPreparationStatusSchema,
  memoryDreamResultSchema,
  memoryEpisodeSchema,
  memoryEpisodesFilterSchema,
  memoryEpisodesInputSchema,
  memoryEpisodeTagSchema,
  memoryPendingEntrySchema,
  memoryPendingInputSchema,
  memoryRestoreInputSchema,
  memoryVersionDetailSchema,
  memoryVersionPageInputSchema,
  memoryVersionSummarySchema,
} from "./schemas";

export type MemoryDocument = z.infer<typeof memoryDocumentSchema>;
export type MemoryVersionSummary = z.infer<typeof memoryVersionSummarySchema>;
export type MemoryVersionDetail = z.infer<typeof memoryVersionDetailSchema>;
export type MemoryDreamResult = z.infer<typeof memoryDreamResultSchema>;
export type MemoryDreamInput = z.input<typeof memoryDreamInputSchema>;
export type MemoryDreamPreparationInput = z.input<
  typeof memoryDreamPreparationInputSchema
>;
export type MemoryDreamPreparationAdmission = z.infer<
  typeof memoryDreamPreparationAdmissionSchema
>;
export type MemoryDreamPreparationStatus = z.infer<
  typeof memoryDreamPreparationStatusSchema
>;
export type MemoryEpisode = z.infer<typeof memoryEpisodeSchema>;
export type MemoryEpisodeTag = z.infer<typeof memoryEpisodeTagSchema>;
export type MemoryEpisodesInput = z.infer<typeof memoryEpisodesInputSchema>;
export type MemoryEpisodesFilter = z.infer<typeof memoryEpisodesFilterSchema>;
export type MemoryPendingEntry = z.infer<typeof memoryPendingEntrySchema>;
export type MemoryPendingInput = z.input<typeof memoryPendingInputSchema>;
export type MemoryVersionPageInput = z.infer<
  typeof memoryVersionPageInputSchema
>;
export type MemoryRestoreInput = z.infer<typeof memoryRestoreInputSchema>;
export type ProjectMemoryAccess = Pick<
  PrivateWorkAccess,
  "apiBaseURL" | "scope"
>;

export type ProjectMemoryPermissions = {
  canRead: boolean;
  canDream: boolean;
  canRestore: boolean;
};
