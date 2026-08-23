import { z } from "zod";

import { fetch } from "@/core/api/fetcher";
import { eventSequenceSchema } from "@/core/private-work/event-sequence";
import type { ProjectPrivateWorkScope } from "@/core/private-work/types";

export const REPEATED_CALL_EVENT_TYPE = "repeated_call";
export const TOOL_CALL_BUDGET_EVENT_TYPE = "tool_call_budget";
export const SUBAGENT_LIMIT_EVENT_TYPE = "subagent_limit";
export const LEGACY_TOOL_CALL_CONTROL_EVENT_TYPE = "tool_call_control";

export const REPEATED_CALL_RUN_EVENT_TYPE = "middleware:repeated_call";
export const TOOL_CALL_BUDGET_RUN_EVENT_TYPE = "middleware:tool_call_budget";
export const SUBAGENT_LIMIT_RUN_EVENT_TYPE = "middleware:subagent_limit";

const RUN_CONTROL_EVENT_PAGE_SIZE = 500;
const RUN_CONTROL_EVENT_MAX_PAGES = 100;
const OBSERVATION_ID_PATTERN = /^[0-9a-f]{64}$/u;
const EXECUTION_ID_PATTERN = /^[0-9a-f]{32}$/u;
const SAFE_TOOL_NAME_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/u;

export const repeatedCallReasonCodeSchema = z.enum([
  "repeated_call_warning",
  "repeated_call_limit",
]);
export const toolCallBudgetReasonCodeSchema = z.enum([
  "tool_budget_warning",
  "tool_budget_exhausted",
]);
const legacyToolCallControlReasonCodeSchema = z.union([
  repeatedCallReasonCodeSchema,
  toolCallBudgetReasonCodeSchema,
]);

const controlPayloadCommonShape = {
  schema_version: z.union([z.literal(1), z.literal(2)]),
  workload_profile: z.enum(["interactive", "research"]),
  role: z.enum(["lead", "subagent"]),
  run_id: z.string().uuid(),
  execution_id: z.string().regex(EXECUTION_ID_PATTERN).nullable(),
  count_before: z.number().int().nonnegative(),
  proposed: z.number().int().nonnegative(),
  admitted: z.number().int().nonnegative(),
  rejected: z.number().int().nonnegative(),
  count_after: z.number().int().nonnegative(),
  warn_threshold: z.number().int().positive().optional(),
  hard_limit: z.number().int().positive(),
  observation_id: z.string().regex(OBSERVATION_ID_PATTERN),
} as const;

type CommonControlPayload = {
  schema_version: 1 | 2;
  workload_profile: "interactive" | "research";
  role: "lead" | "subagent";
  run_id: string;
  execution_id: string | null;
  count_before: number;
  proposed: number;
  admitted: number;
  rejected: number;
  count_after: number;
  warn_threshold?: number;
  hard_limit: number;
  observation_id: string;
};

function validateCommonControlPayload(
  value: CommonControlPayload,
  context: z.RefinementCtx,
) {
  if (value.proposed !== value.admitted + value.rejected) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "proposed must equal admitted plus rejected",
      path: ["proposed"],
    });
  }
  if (
    value.warn_threshold !== undefined &&
    value.hard_limit < value.warn_threshold
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "hard_limit must be greater than or equal to warn_threshold",
      path: ["hard_limit"],
    });
  }
  if (value.role === "lead" && value.execution_id !== null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "lead observations cannot carry a delegated execution ID",
      path: ["execution_id"],
    });
  }
  if (value.role === "subagent" && value.execution_id === null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "subagent observations require a safe execution projection",
      path: ["execution_id"],
    });
  }
}

const repeatedCallPayloadShape = {
  ...controlPayloadCommonShape,
  reason_code: repeatedCallReasonCodeSchema,
  disposition: z.enum(["advisory", "tool_free_finalization"]),
} as const;

type RepeatedCallPayload = CommonControlPayload & {
  reason_code: string;
  disposition: string;
};

function validateRepeatedCallPayload(
  value: RepeatedCallPayload,
  context: z.RefinementCtx,
) {
  validateCommonControlPayload(value, context);
  if (value.schema_version !== 1 || value.warn_threshold === undefined) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "repeated-call observations require schema version 1",
      path: ["schema_version"],
    });
  }
  if (value.proposed !== 1 || value.count_after !== value.count_before + 1) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "repeated-call observation counters do not reconcile",
      path: ["count_after"],
    });
  }
  if (
    value.reason_code === "repeated_call_warning" &&
    (value.admitted !== 1 ||
      value.rejected !== 0 ||
      value.disposition !== "advisory")
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "repeated-call warning must be one admitted advisory",
      path: ["disposition"],
    });
  }
  if (
    value.reason_code === "repeated_call_limit" &&
    (value.admitted !== 0 ||
      value.rejected !== 1 ||
      value.disposition !== "tool_free_finalization")
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "repeated-call limit must reject one repeated proposal",
      path: ["disposition"],
    });
  }
}

const repeatedCallPayloadSchemaWithoutInvariants = z
  .object(repeatedCallPayloadShape)
  .strict();
const repeatedCallPayloadSchema =
  repeatedCallPayloadSchemaWithoutInvariants.superRefine(
    validateRepeatedCallPayload,
  );

export const repeatedCallEventSchema = z
  .object({
    type: z.literal(REPEATED_CALL_EVENT_TYPE),
    ...repeatedCallPayloadShape,
  })
  .strict()
  .superRefine(validateRepeatedCallPayload);

export type RepeatedCallEvent = z.infer<typeof repeatedCallEventSchema>;

const toolCallBudgetPayloadShape = {
  ...controlPayloadCommonShape,
  reason_code: toolCallBudgetReasonCodeSchema,
  tool_name: z.string().regex(SAFE_TOOL_NAME_PATTERN).optional(),
  disposition: z.enum([
    "advisory",
    "truncate_tool_calls",
    "exhaust_tool",
    "exhaust_run",
  ]),
} as const;

type ToolCallBudgetPayload = CommonControlPayload & {
  reason_code: string;
  disposition: string;
  tool_name?: string;
};

function validateToolCallBudgetPayload(
  value: ToolCallBudgetPayload,
  context: z.RefinementCtx,
) {
  validateCommonControlPayload(value, context);
  if (value.count_after !== value.count_before + value.admitted) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "tool-budget counters do not reconcile",
      path: ["count_after"],
    });
  }
  if (value.schema_version === 2) {
    if (
      value.reason_code !== "tool_budget_exhausted" ||
      value.warn_threshold !== undefined ||
      value.tool_name !== undefined ||
      (value.disposition !== "truncate_tool_calls" &&
        value.disposition !== "exhaust_run")
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Run tool-call limit payload is invalid",
        path: ["disposition"],
      });
    }
    return;
  }
  if (value.warn_threshold === undefined || value.tool_name === undefined) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "legacy tool-budget observations require a tool and warning",
      path: ["tool_name"],
    });
    return;
  }
  if (
    value.reason_code === "tool_budget_warning" &&
    (value.rejected !== 0 || value.disposition !== "advisory")
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "tool-budget warning must be advisory",
      path: ["disposition"],
    });
  }
  if (
    value.reason_code === "tool_budget_exhausted" &&
    value.disposition !== "truncate_tool_calls" &&
    value.disposition !== "exhaust_tool"
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "tool-budget exhaustion must cap the affected tool",
      path: ["disposition"],
    });
  }
}

const toolCallBudgetPayloadSchemaWithoutInvariants = z
  .object(toolCallBudgetPayloadShape)
  .strict();
const toolCallBudgetPayloadSchema =
  toolCallBudgetPayloadSchemaWithoutInvariants.superRefine(
    validateToolCallBudgetPayload,
  );

export const toolCallBudgetEventSchema = z
  .object({
    type: z.literal(TOOL_CALL_BUDGET_EVENT_TYPE),
    ...toolCallBudgetPayloadShape,
  })
  .strict()
  .superRefine(validateToolCallBudgetPayload);

export type ToolCallBudgetEvent = z.infer<typeof toolCallBudgetEventSchema>;

const legacyToolCallControlPayloadShape = {
  ...controlPayloadCommonShape,
  schema_version: z.literal(1),
  warn_threshold: z.number().int().positive(),
  reason_code: legacyToolCallControlReasonCodeSchema,
  tool_name: z.string().regex(SAFE_TOOL_NAME_PATTERN).nullable(),
  disposition: z.enum([
    "advisory",
    "tool_free_finalization",
    "truncate_tool_calls",
    "exhaust_tool",
  ]),
} as const;

type LegacyToolCallControlPayload = z.infer<
  typeof legacyToolCallControlPayloadSchemaWithoutInvariants
>;

function validateLegacyToolCallControlPayload(
  value: LegacyToolCallControlPayload,
  context: z.RefinementCtx,
) {
  if (
    value.reason_code === "repeated_call_warning" ||
    value.reason_code === "repeated_call_limit"
  ) {
    if (value.tool_name !== null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "legacy repeated-call observations cannot name a tool",
        path: ["tool_name"],
      });
    }
    validateRepeatedCallPayload(value, context);
    return;
  }
  if (value.tool_name === null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "legacy tool-budget observations require a safe tool name",
      path: ["tool_name"],
    });
    validateCommonControlPayload(value, context);
    return;
  }
  validateToolCallBudgetPayload(
    { ...value, tool_name: value.tool_name },
    context,
  );
}

const legacyToolCallControlPayloadSchemaWithoutInvariants = z
  .object(legacyToolCallControlPayloadShape)
  .strict();
const legacyToolCallControlPayloadSchema =
  legacyToolCallControlPayloadSchemaWithoutInvariants.superRefine(
    validateLegacyToolCallControlPayload,
  );

const legacyToolCallControlEventSchema = z
  .object({
    type: z.literal(LEGACY_TOOL_CALL_CONTROL_EVENT_TYPE),
    ...legacyToolCallControlPayloadShape,
  })
  .strict()
  .superRefine(validateLegacyToolCallControlPayload);

const subagentLimitPayloadShape = {
  schema_version: z.literal(1),
  reason_code: z.literal("subagent_total_limit"),
  role: z.literal("lead"),
  run_id: z.string().uuid(),
  count_before: z.number().int().nonnegative(),
  proposed: z.number().int().nonnegative(),
  admitted: z.number().int().nonnegative(),
  rejected: z.number().int().nonnegative(),
  count_after: z.number().int().nonnegative(),
  hard_limit: z.number().int().positive(),
  disposition: z.literal("truncate_tool_calls"),
  observation_id: z.string().regex(OBSERVATION_ID_PATTERN),
} as const;

type SubagentLimitPayload = z.infer<
  typeof subagentLimitPayloadSchemaWithoutInvariants
>;

function validateSubagentLimitPayload(
  value: SubagentLimitPayload,
  context: z.RefinementCtx,
) {
  if (value.proposed !== value.admitted + value.rejected) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "proposed must equal admitted plus rejected",
      path: ["proposed"],
    });
  }
  if (value.count_after !== value.count_before + value.admitted) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "admitted counters do not reconcile",
      path: ["count_after"],
    });
  }
  if (value.count_after > value.hard_limit || value.rejected === 0) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "subagent limit must reject calls at the hard limit",
      path: ["rejected"],
    });
  }
}

const subagentLimitPayloadSchemaWithoutInvariants = z
  .object(subagentLimitPayloadShape)
  .strict();
const subagentLimitPayloadSchema =
  subagentLimitPayloadSchemaWithoutInvariants.superRefine(
    validateSubagentLimitPayload,
  );

export const subagentLimitEventSchema = z
  .object({
    type: z.literal(SUBAGENT_LIMIT_EVENT_TYPE),
    ...subagentLimitPayloadShape,
  })
  .strict()
  .superRefine(validateSubagentLimitPayload);

export type SubagentLimitEvent = z.infer<typeof subagentLimitEventSchema>;

export type RunControlObservation =
  | RepeatedCallEvent
  | ToolCallBudgetEvent
  | SubagentLimitEvent;

export type RunControlProgress = {
  runId: string | null;
  observations: RunControlObservation[];
};

type ProjectedDurablePayload = {
  run_id: string;
  reason_code: string;
  observation_id: string;
};

type ProjectedDurableRow = {
  run_id: string;
  content: ProjectedDurablePayload;
  metadata: { reason_code: string; observation_id: string };
};

function validateDurableProjection(
  value: ProjectedDurableRow,
  context: z.RefinementCtx,
) {
  if (value.content.run_id !== value.run_id) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "durable content Run does not match its row",
      path: ["content", "run_id"],
    });
  }
  if (value.content.reason_code !== value.metadata.reason_code) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "durable reason metadata does not match its content",
      path: ["metadata", "reason_code"],
    });
  }
  if (value.content.observation_id !== value.metadata.observation_id) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: "durable observation metadata does not match its content",
      path: ["metadata", "observation_id"],
    });
  }
}

const durableRowCommonShape = {
  thread_id: z.string().uuid(),
  run_id: z.string().uuid(),
  category: z.literal("middleware"),
  seq: eventSequenceSchema,
  created_at: z.string().datetime({ offset: true }),
} as const;

const fetchedRepeatedCallEventSchema = z
  .object({
    ...durableRowCommonShape,
    event_type: z.literal(REPEATED_CALL_RUN_EVENT_TYPE),
    content: repeatedCallPayloadSchema,
    metadata: z
      .object({
        reason_code: repeatedCallReasonCodeSchema,
        observation_id: z.string().regex(OBSERVATION_ID_PATTERN),
      })
      .strict(),
  })
  .strict()
  .superRefine(validateDurableProjection);

const fetchedToolCallBudgetEventSchema = z
  .object({
    ...durableRowCommonShape,
    event_type: z.literal(TOOL_CALL_BUDGET_RUN_EVENT_TYPE),
    content: toolCallBudgetPayloadSchema,
    metadata: z
      .object({
        reason_code: toolCallBudgetReasonCodeSchema,
        observation_id: z.string().regex(OBSERVATION_ID_PATTERN),
      })
      .strict(),
  })
  .strict()
  .superRefine(validateDurableProjection);

const fetchedLegacyToolCallControlEventSchema = z
  .object({
    ...durableRowCommonShape,
    event_type: z.literal(TOOL_CALL_BUDGET_RUN_EVENT_TYPE),
    content: legacyToolCallControlPayloadSchema,
    metadata: z
      .object({
        reason_code: legacyToolCallControlReasonCodeSchema,
        observation_id: z.string().regex(OBSERVATION_ID_PATTERN),
      })
      .strict(),
  })
  .strict()
  .superRefine(validateDurableProjection);

const fetchedSubagentLimitEventSchema = z
  .object({
    ...durableRowCommonShape,
    event_type: z.literal(SUBAGENT_LIMIT_RUN_EVENT_TYPE),
    content: subagentLimitPayloadSchema,
    metadata: z
      .object({
        reason_code: z.literal("subagent_total_limit"),
        observation_id: z.string().regex(OBSERVATION_ID_PATTERN),
      })
      .strict(),
  })
  .strict()
  .superRefine(validateDurableProjection);

const fetchedLegacySubagentLimitEventSchema = z
  .object({
    ...durableRowCommonShape,
    event_type: z.literal(SUBAGENT_LIMIT_RUN_EVENT_TYPE),
    content: z
      .object({
        name: z.literal("SubagentLimitMiddleware"),
        hook: z.literal("after_model"),
        action: z.literal("truncate_tool_calls"),
        changes: z
          .object({
            reason: z.literal("subagent_total_limit"),
            max_total: z.number().int().positive(),
            prior_delegations: z.number().int().nonnegative(),
            admitted_task_calls: z.number().int().nonnegative(),
            dropped_task_calls: z.number().int().positive(),
          })
          .strict(),
      })
      .strict(),
    metadata: z.object({}).strict(),
  })
  .strict();

const fetchedRunControlEventSchema = z.union([
  fetchedRepeatedCallEventSchema,
  fetchedToolCallBudgetEventSchema,
  fetchedLegacyToolCallControlEventSchema,
  fetchedSubagentLimitEventSchema,
  fetchedLegacySubagentLimitEventSchema,
]);

type FetchedRunControlEvent = z.infer<typeof fetchedRunControlEventSchema>;

function historicalSubagentLimitObservationId(
  runId: string,
  sequence: string,
): string {
  const runHex = runId.replaceAll("-", "");
  const sequenceHex = BigInt(sequence)
    .toString(16)
    .padStart(32, "0")
    .slice(-32);
  return `${runHex}${sequenceHex}`;
}

export function parseRepeatedCallEvent(
  event: unknown,
): RepeatedCallEvent | null {
  const parsed = repeatedCallEventSchema.safeParse(event);
  return parsed.success ? parsed.data : null;
}

export function parseToolCallBudgetEvent(
  event: unknown,
): ToolCallBudgetEvent | null {
  const parsed = toolCallBudgetEventSchema.safeParse(event);
  return parsed.success ? parsed.data : null;
}

export function parseSubagentLimitEvent(
  event: unknown,
): SubagentLimitEvent | null {
  const parsed = subagentLimitEventSchema.safeParse(event);
  return parsed.success ? parsed.data : null;
}

function normalizeLegacyToolCallControlPayload(
  input: unknown,
): RepeatedCallEvent | ToolCallBudgetEvent | null {
  const parsed = legacyToolCallControlPayloadSchema.safeParse(input);
  if (!parsed.success) return null;
  const { tool_name: toolName, ...payload } = parsed.data;
  if (
    payload.reason_code === "repeated_call_warning" ||
    payload.reason_code === "repeated_call_limit"
  ) {
    return repeatedCallEventSchema.parse({
      type: REPEATED_CALL_EVENT_TYPE,
      ...payload,
    });
  }
  if (toolName === null) return null;
  return toolCallBudgetEventSchema.parse({
    type: TOOL_CALL_BUDGET_EVENT_TYPE,
    ...payload,
    tool_name: toolName,
  });
}

export function parseRunControlLiveEvent(
  event: unknown,
): RunControlObservation | null {
  const current =
    parseRepeatedCallEvent(event) ??
    parseToolCallBudgetEvent(event) ??
    parseSubagentLimitEvent(event);
  if (current) return current;

  const legacy = legacyToolCallControlEventSchema.safeParse(event);
  if (!legacy.success) return null;
  const { type: legacyType, ...payload } = legacy.data;
  if (legacyType !== LEGACY_TOOL_CALL_CONTROL_EVENT_TYPE) return null;
  return normalizeLegacyToolCallControlPayload(payload);
}

function fetchedRowToObservation(
  row: FetchedRunControlEvent,
): RunControlObservation {
  if (row.event_type === REPEATED_CALL_RUN_EVENT_TYPE) {
    return repeatedCallEventSchema.parse({
      type: REPEATED_CALL_EVENT_TYPE,
      ...row.content,
    });
  }
  if (row.event_type === TOOL_CALL_BUDGET_RUN_EVENT_TYPE) {
    const current = toolCallBudgetPayloadSchema.safeParse(row.content);
    if (current.success) {
      return toolCallBudgetEventSchema.parse({
        type: TOOL_CALL_BUDGET_EVENT_TYPE,
        ...current.data,
      });
    }
    const normalized = normalizeLegacyToolCallControlPayload(row.content);
    if (!normalized) {
      throw new Error("Durable tool-call control event was not normalized.");
    }
    return normalized;
  }
  if ("schema_version" in row.content) {
    return subagentLimitEventSchema.parse({
      type: SUBAGENT_LIMIT_EVENT_TYPE,
      ...row.content,
    });
  }
  const changes = row.content.changes;
  return subagentLimitEventSchema.parse({
    type: SUBAGENT_LIMIT_EVENT_TYPE,
    schema_version: 1,
    reason_code: "subagent_total_limit",
    role: "lead",
    run_id: row.run_id,
    count_before: changes.prior_delegations,
    proposed: changes.admitted_task_calls + changes.dropped_task_calls,
    admitted: changes.admitted_task_calls,
    rejected: changes.dropped_task_calls,
    count_after: changes.prior_delegations + changes.admitted_task_calls,
    hard_limit: changes.max_total,
    disposition: "truncate_tool_calls",
    observation_id: historicalSubagentLimitObservationId(row.run_id, row.seq),
  });
}

export function parseRunControlEventRows(
  input: unknown,
): RunControlObservation[] {
  return z
    .array(fetchedRunControlEventSchema)
    .parse(input)
    .map(fetchedRowToObservation);
}

export function emptyRunControlProgress(): RunControlProgress {
  return { runId: null, observations: [] };
}

export function mergeRunControlObservations(
  current: RunControlProgress,
  incoming: readonly RunControlObservation[],
): RunControlProgress {
  let next = current;
  for (const observation of incoming) {
    if (next.runId !== observation.run_id) {
      next = { runId: observation.run_id, observations: [] };
    }
    if (
      next.observations.some(
        (existing) => existing.observation_id === observation.observation_id,
      )
    ) {
      continue;
    }
    next = {
      runId: next.runId,
      observations: [...next.observations, observation],
    };
  }
  return next;
}

export async function fetchRunControlObservations(
  privateWork: Pick<ProjectPrivateWorkScope, "apiBaseURL">,
  threadId: string,
  runId: string,
  signal?: AbortSignal,
): Promise<RunControlObservation[]> {
  const base = `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
    threadId,
  )}/runs/${encodeURIComponent(runId)}/events`;
  const observations: RunControlObservation[] = [];
  let afterSeq: string | undefined;

  for (let page = 0; page < RUN_CONTROL_EVENT_MAX_PAGES; page += 1) {
    signal?.throwIfAborted();
    const params = new URLSearchParams({
      event_types: [
        REPEATED_CALL_RUN_EVENT_TYPE,
        TOOL_CALL_BUDGET_RUN_EVENT_TYPE,
        SUBAGENT_LIMIT_RUN_EVENT_TYPE,
      ].join(","),
      limit: String(RUN_CONTROL_EVENT_PAGE_SIZE),
    });
    if (afterSeq !== undefined) {
      params.set("after_seq", afterSeq);
    }
    const response = await fetch(`${base}?${params.toString()}`, { signal });
    if (!response.ok) {
      throw new Error(
        `Failed to fetch Run control observations: ${response.status}`,
      );
    }
    const rawBatch: unknown = await response.json();
    const parsedRows = z.array(fetchedRunControlEventSchema).parse(rawBatch);
    observations.push(...parsedRows.map(fetchedRowToObservation));
    if (parsedRows.length < RUN_CONTROL_EVENT_PAGE_SIZE) {
      return observations;
    }
    const nextAfterSeq = parsedRows.at(-1)?.seq;
    if (nextAfterSeq === undefined || nextAfterSeq === afterSeq) {
      throw new Error("Run control event pagination did not advance.");
    }
    afterSeq = nextAfterSeq;
  }

  throw new Error("Run control event pagination exceeded its safety limit.");
}
