import { z } from "zod";

export const MEMORY_DOCUMENT_MAX_LENGTH = 16_000;
export const MEMORY_DIFF_MAX_LENGTH = 64_000;
export const MEMORY_VERSION_PAGE_SIZE = 50;
export const MEMORY_EPISODE_PAGE_SIZE = 20;
export const MEMORY_EPISODE_SEARCH_LIMIT = 50;
export const MEMORY_EPISODE_QUERY_MAX_LENGTH = 200;
export const MEMORY_PENDING_PAGE_SIZE = 50;

export const memoryDateTimeSchema = z
  .string()
  .max(64)
  .datetime({ offset: true });
export const memoryVersionSchema = z.number().int().positive();

function unicodeCodePointBound(maximum: number, label: string) {
  return z.string().refine((value) => Array.from(value).length <= maximum, {
    message: `${label} must contain at most ${maximum} Unicode code points`,
  });
}

const memoryDocumentTextSchema = unicodeCodePointBound(
  MEMORY_DOCUMENT_MAX_LENGTH,
  "Memory document",
);
const memoryDiffTextSchema = unicodeCodePointBound(
  MEMORY_DIFF_MAX_LENGTH,
  "Memory diff",
);
const memoryEpisodeQuerySchema = z
  .string()
  .min(1)
  .refine(
    (value) => Array.from(value).length <= MEMORY_EPISODE_QUERY_MAX_LENGTH,
    {
      message: `Memory episode query must contain at most ${MEMORY_EPISODE_QUERY_MAX_LENGTH} Unicode code points`,
    },
  );

export const memoryDocumentSchema = z
  .object({
    content: memoryDocumentTextSchema,
    version: z.number().int().nonnegative(),
    updatedAt: memoryDateTimeSchema.nullable(),
    pendingCount: z.number().int().nonnegative(),
    dreamRunning: z.boolean(),
    injectionStatus: z.enum(["ok", "skipped_over_budget"]),
    injectionAdvisory: z
      .discriminatedUnion("status", [
        z
          .object({
            basis: z.literal("current_non_continuation"),
            status: z.literal("eligible"),
            reason: z.literal("within_budget"),
          })
          .strict(),
        z
          .object({
            basis: z.literal("current_non_continuation"),
            status: z.literal("skipped_over_budget"),
            reason: z.literal("over_budget"),
          })
          .strict(),
        z
          .object({
            basis: z.literal("current_non_continuation"),
            status: z.literal("inactive"),
            reason: z.enum([
              "platform_disabled",
              "account_disabled",
              "no_document",
            ]),
          })
          .strict(),
      ])
      .optional(),
  })
  .strict();

const memoryVersionSummaryBaseSchema = z
  .object({
    version: memoryVersionSchema,
    trigger: z.enum([
      "auto_dream",
      "manual_dream",
      "restore",
      "budget_rewrite",
    ]),
    historyCount: z.number().int().min(0).max(20).nullable(),
    changed: z.boolean(),
    needsReview: z.boolean(),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

function validateMemoryVersionHistory(
  value: z.infer<typeof memoryVersionSummaryBaseSchema>,
  context: z.RefinementCtx,
) {
  const valid =
    value.trigger === "restore"
      ? value.historyCount === null
      : value.trigger === "budget_rewrite"
        ? value.historyCount === 0
        : value.historyCount !== null && value.historyCount >= 1;
  if (!valid) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["historyCount"],
      message: "Memory version history count does not match its trigger",
    });
  }
}

export const memoryVersionSummarySchema =
  memoryVersionSummaryBaseSchema.superRefine(validateMemoryVersionHistory);

export const memoryVersionsSchema = z
  .object({
    items: z.array(memoryVersionSummarySchema).max(100),
  })
  .strict();

export const memoryVersionDetailSchema = memoryVersionSummaryBaseSchema
  .extend({
    content: memoryDocumentTextSchema,
    unifiedDiff: memoryDiffTextSchema,
    diffTruncated: z.boolean().optional(),
  })
  .strict()
  .superRefine(validateMemoryVersionHistory)
  .transform((value) => ({
    ...value,
    diffTruncated: value.diffTruncated ?? false,
  }));

const admittedDreamDispositionSchema = z.enum(["queued", "already_running"]);

export const memoryDreamResultSchema = z.union([
  z
    .object({
      disposition: z.literal("nothing_pending"),
      jobId: z.null(),
      historyCount: z.literal(0),
    })
    .strict(),
  z
    .object({
      disposition: admittedDreamDispositionSchema,
      jobId: z.string().uuid(),
      historyCount: z.literal(0),
      admissionKind: z.literal("budget_rewrite"),
    })
    .strict(),
  z
    .object({
      disposition: admittedDreamDispositionSchema,
      jobId: z.string().uuid(),
      historyCount: z.number().int().min(1).max(20),
    })
    .strict(),
]);

export const memoryDreamInputSchema = z.object({}).strict();

export const memoryDreamPreparationInputSchema = z
  .object({
    threadId: z.string().min(1).max(64),
    operationId: z.string().uuid(),
  })
  .strict();

export const memoryDreamPreparationAdmissionSchema = z
  .object({
    disposition: z.enum(["queued", "already_running"]),
    jobId: z.string().uuid(),
    status: z.enum(["queued", "running", "succeeded", "cancelled", "failed"]),
  })
  .strict();

export const memoryDreamPreparationStatusSchema = z
  .object({
    jobId: z.string().uuid(),
    status: z.enum(["queued", "running", "succeeded", "cancelled", "failed"]),
    phase: z.enum([
      "queued",
      "draining",
      "verifying",
      "dream_admitted",
      "succeeded",
      "cancelled",
      "failed",
    ]),
    compactedPasses: z.number().int().nonnegative(),
    dreamJobId: z.string().uuid().nullable(),
    historyCount: z.number().int().min(0).max(20).nullable(),
    admissionKind: z.enum(["history", "budget_rewrite"]).nullable(),
    resultDisposition: z.enum([
      "queued",
      "already_running",
      "nothing_pending",
      "cancelled",
      "failed",
    ]),
    cancelRequested: z.boolean(),
    publicErrorCode: z
      .string()
      .regex(/^[A-Z][A-Z0-9_]{0,63}$/)
      .nullable(),
    updatedAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryEpisodeTagSchema = z.enum([
  "permanent",
  "durable",
  "ephemeral",
  "correction",
]);

export const memoryEpisodeSchema = z
  .object({
    id: z.string().uuid(),
    threadId: z.string().min(1).max(64),
    origin: z.enum(["snip", "tool"]),
    taggedText: z.string().min(1).max(1_000),
    occurredAt: memoryDateTimeSchema,
    createdAt: memoryDateTimeSchema,
  })
  .strict();

const memoryEpisodesLegacySchema = z
  .object({
    items: z.array(memoryEpisodeSchema).max(50),
  })
  .strict();

const memoryEpisodesCursorSchema = z
  .object({
    items: z.array(memoryEpisodeSchema).max(50),
    nextCursor: z.string().min(1).max(512).nullable(),
  })
  .strict();

export const memoryEpisodesSchema = z.union([
  memoryEpisodesCursorSchema,
  memoryEpisodesLegacySchema,
]);

export const memoryEpisodesInputSchema = z
  .object({
    q: memoryEpisodeQuerySchema.optional(),
    tags: z.array(memoryEpisodeTagSchema).max(4).optional(),
    cursor: z.string().min(1).max(512).optional(),
    before: memoryDateTimeSchema.optional(),
    limit: z.number().int().min(1).max(50),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.cursor !== undefined && value.before !== undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["cursor"],
        message: "Memory episode cursors are mutually exclusive",
      });
    }
    if (value.q !== undefined && value.cursor !== undefined) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["cursor"],
        message: "Memory episode search is not cursor-paged",
      });
    }
  });

export const memoryEpisodesFilterSchema = z
  .object({
    q: memoryEpisodeQuerySchema.optional(),
    tags: z.array(memoryEpisodeTagSchema).max(4).optional(),
  })
  .strict();

export const memoryPendingEntrySchema = z
  .object({
    sequence: z.number().int().positive(),
    origin: z.enum(["snip", "tool"]),
    taggedText: z.string().min(1).max(1_000),
    createdAt: memoryDateTimeSchema,
  })
  .strict();

export const memoryPendingSchema = z
  .object({
    items: z.array(memoryPendingEntrySchema).max(100),
  })
  .strict();

export const memoryPendingInputSchema = z
  .object({
    limit: z.number().int().min(1).max(100).default(MEMORY_PENDING_PAGE_SIZE),
    offset: z.number().int().min(0).max(10_000).default(0),
  })
  .strict();

export const memoryVersionPageInputSchema = z
  .object({
    limit: z.number().int().min(1).max(100),
    offset: z.number().int().min(0).max(10_000),
  })
  .strict();

export const memoryRestoreInputSchema = z
  .object({
    expectedCurrentVersion: z.number().int().nonnegative(),
  })
  .strict();
