import { z } from "zod";

import { throwGatewayApiError } from "@/core/api/errors";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import {
  isPrivateWorkAccessActive,
  runPrivateWorkAbortable,
  type ProjectClientScope,
  type ProjectPrivateWorkScope,
} from "@/core/private-work/types";

export const CONTEXT_PROJECTION_EVENT_NAME =
  "context.projection.updated.v2" as const;

export const CONTEXT_PROJECTION_LANES = [
  "system_prompt",
  "agent_instructions",
  "tool_definitions",
  "skills",
  "mcp_dynamic_tools",
  "subagent_definitions",
  "summarized_conversation",
  "conversation",
  "visual_media",
  "provider_overhead",
] as const;

const uuidSchema = z
  .string()
  .regex(
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/u,
  );
const threadIdSchema = z
  .string()
  .min(1)
  .max(64)
  .regex(/^[A-Za-z0-9][A-Za-z0-9_-]*$/u);
const decimalStringSchema = z
  .string()
  .regex(/^(?:0|[1-9][0-9]*)$/u)
  .refine((value) => BigInt(value) <= 9_223_372_036_854_775_807n, {
    message: "sequence exceeds signed BIGINT",
  });
const digestSchema = z.string().regex(/^[a-f0-9]{64}$/u);
const nonnegativeIntegerSchema = z.number().int().nonnegative();
const nullableNonnegativeIntegerSchema = nonnegativeIntegerSchema.nullable();
const laneNameSchema = z.enum(CONTEXT_PROJECTION_LANES);

const leadSubjectSchema = z
  .object({
    kind: z.literal("lead_thread"),
    thread_id: threadIdSchema,
    execution_id: z.null(),
  })
  .strict();

const subagentSubjectSchema = z
  .object({
    kind: z.literal("subagent_task"),
    thread_id: threadIdSchema,
    execution_id: uuidSchema,
  })
  .strict();

const subjectSchema = z.discriminatedUnion("kind", [
  leadSubjectSchema,
  subagentSubjectSchema,
]);

const laneSchema = z
  .object({
    lane: laneNameSchema,
    projected_tokens: nonnegativeIntegerSchema,
    lower_bound_tokens: nonnegativeIntegerSchema,
    safety_upper_bound_tokens: nullableNonnegativeIntegerSchema,
  })
  .strict()
  .superRefine((value, context) => {
    if (value.lower_bound_tokens > value.projected_tokens) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "lane lower bound cannot exceed its point projection",
        path: ["lower_bound_tokens"],
      });
    }
    if (
      value.safety_upper_bound_tokens !== null &&
      value.safety_upper_bound_tokens < value.projected_tokens
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "lane safety bound cannot be below its point projection",
        path: ["safety_upper_bound_tokens"],
      });
    }
  });

const totalsSchema = z
  .object({
    projected_tokens: nonnegativeIntegerSchema,
    lower_bound_tokens: nonnegativeIntegerSchema,
    safety_upper_bound_tokens: nullableNonnegativeIntegerSchema,
    context_window_tokens: z
      .number()
      .int()
      .positive()
      .max(2_000_000)
      .nullable(),
    remaining_tokens: nullableNonnegativeIntegerSchema,
    progress_percent: z.number().finite().min(0).max(100).nullable(),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.lower_bound_tokens > value.projected_tokens) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "total lower bound cannot exceed the point projection",
        path: ["lower_bound_tokens"],
      });
    }
    if (
      value.safety_upper_bound_tokens !== null &&
      value.safety_upper_bound_tokens < value.projected_tokens
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "total safety bound cannot be below the point projection",
        path: ["safety_upper_bound_tokens"],
      });
    }
    if (value.context_window_tokens === null) {
      if (value.remaining_tokens !== null || value.progress_percent !== null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "capacity-derived totals require a known context window",
          path: ["context_window_tokens"],
        });
      }
    } else if (
      value.remaining_tokens === null ||
      value.progress_percent === null
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "known context windows require remaining and progress values",
        path: ["context_window_tokens"],
      });
    }
  });

const noticeSchema = z
  .object({
    code: z.enum([
      "VISUAL_COST_UNMEASURED",
      "PROJECTION_STALE",
      "CAPACITY_UNKNOWN",
      "PROVIDER_USAGE_UNREPORTED",
      "PROVIDER_CALL_AMBIGUOUS",
    ]),
    count: z.number().int().positive().nullable(),
    lane: laneNameSchema.nullable(),
  })
  .strict();

const threadContextProjectionSchema = z
  .object({
    contract_version: z.literal(2),
    thread_id: threadIdSchema,
    subject: subjectSchema,
    phase: z.enum(["idle", "active", "settled"]),
    projection_seq: decimalStringSchema,
    evidence_seq: decimalStringSchema,
    context_window_generation: uuidSchema,
    checkpoint_id: z
      .string()
      .min(1)
      .max(128)
      .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*$/u)
      .nullable(),
    projector_revision: z
      .string()
      .min(4)
      .max(128)
      .regex(/^[A-Za-z0-9][A-Za-z0-9._:-]*-v[1-9][0-9]*$/u),
    model: z
      .object({
        identity_digest: digestSchema,
        context_window_tokens: z
          .number()
          .int()
          .positive()
          .max(2_000_000)
          .nullable(),
      })
      .strict(),
    basis: z.enum(["provider_confirmed", "hybrid", "estimated", "empty"]),
    coverage: z.enum(["complete", "partial"]),
    freshness: z.enum(["current", "stale"]),
    totals: totalsSchema,
    lanes: z.array(laneSchema).max(CONTEXT_PROJECTION_LANES.length),
    last_provider_observation: z
      .object({
        provider_call_id: digestSchema,
        input_tokens: nonnegativeIntegerSchema,
        observed_at: z.string().datetime({ offset: true }),
      })
      .strict()
      .nullable(),
    compaction: z
      .object({
        enabled: z.boolean(),
        threshold_tokens: z.number().int().positive().nullable(),
        reached: z.boolean(),
        authority: z.enum(["frozen_run", "idle_history"]).nullable(),
        blocked_reason: z
          .string()
          .min(1)
          .max(64)
          .regex(/^[A-Z][A-Z0-9_]*$/u)
          .nullable(),
      })
      .strict(),
    notices: z.array(noticeSchema).max(32),
    as_of: z.string().datetime({ offset: true }),
  })
  .strict()
  .superRefine((value, context) => {
    if (value.subject.thread_id !== value.thread_id) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Context Subject must belong to the response Thread",
        path: ["subject", "thread_id"],
      });
    }
    if (
      value.model.context_window_tokens !== value.totals.context_window_tokens
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "model and total context windows must agree",
        path: ["totals", "context_window_tokens"],
      });
    }
    const seen = new Set<string>();
    let previousIndex = -1;
    let projectedTokens = 0;
    let lowerBoundTokens = 0;
    let safetyUpperBoundTokens = 0;
    let hasUnknownUpperBound = false;
    value.lanes.forEach((lane, index) => {
      if (seen.has(lane.lane)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Context Projection lanes must be unique",
          path: ["lanes", index, "lane"],
        });
      }
      seen.add(lane.lane);
      const laneIndex = CONTEXT_PROJECTION_LANES.indexOf(lane.lane);
      if (laneIndex <= previousIndex) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "Context Projection lanes must use canonical order",
          path: ["lanes", index, "lane"],
        });
      }
      previousIndex = laneIndex;
      projectedTokens += lane.projected_tokens;
      lowerBoundTokens += lane.lower_bound_tokens;
      if (lane.safety_upper_bound_tokens === null) {
        hasUnknownUpperBound = true;
      } else {
        safetyUpperBoundTokens += lane.safety_upper_bound_tokens;
      }
    });
    if (projectedTokens !== value.totals.projected_tokens) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "projected total must equal the lane projection sum",
        path: ["totals", "projected_tokens"],
      });
    }
    if (lowerBoundTokens !== value.totals.lower_bound_tokens) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "lower-bound total must equal the lane lower-bound sum",
        path: ["totals", "lower_bound_tokens"],
      });
    }
    const expectedSafetyUpperBound = hasUnknownUpperBound
      ? null
      : safetyUpperBoundTokens;
    if (expectedSafetyUpperBound !== value.totals.safety_upper_bound_tokens) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "safety total must equal the lane safety-bound sum",
        path: ["totals", "safety_upper_bound_tokens"],
      });
    }
    const expectedCoverage = hasUnknownUpperBound ? "partial" : "complete";
    if (value.coverage !== expectedCoverage) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "coverage must agree with lane upper bounds",
        path: ["coverage"],
      });
    }
    if (value.totals.context_window_tokens !== null) {
      const capacity = value.totals.context_window_tokens;
      if (
        value.totals.remaining_tokens !==
        Math.max(capacity - value.totals.projected_tokens, 0)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "remaining Tokens must use the point projection",
          path: ["totals", "remaining_tokens"],
        });
      }
      const expectedProgress = Math.min(
        Math.round((value.totals.projected_tokens * 1000) / capacity) / 10,
        100,
      );
      if (value.totals.progress_percent !== expectedProgress) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "progress must use the point projection",
          path: ["totals", "progress_percent"],
        });
      }
    }
    const noticeCodes = new Set(value.notices.map((notice) => notice.code));
    value.notices.forEach((notice, index) => {
      const visualNotice = notice.code === "VISUAL_COST_UNMEASURED";
      if (
        visualNotice
          ? notice.count === null || notice.lane !== "visual_media"
          : notice.count !== null || notice.lane !== null
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          message: "notice details do not match their public code",
          path: ["notices", index],
        });
      }
    });
    if (noticeCodes.size !== value.notices.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "notice codes must be unique",
        path: ["notices"],
      });
    }
    if (value.freshness === "stale" && !noticeCodes.has("PROJECTION_STALE")) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "stale Projections require a stale notice",
        path: ["notices"],
      });
    }
    if (
      value.totals.context_window_tokens === null &&
      !noticeCodes.has("CAPACITY_UNKNOWN")
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: "unknown capacity requires a notice",
        path: ["notices"],
      });
    }
  });

export type ThreadContextProjection = z.infer<
  typeof threadContextProjectionSchema
>;
export type ContextProjectionLane = ThreadContextProjection["lanes"][number];
export type ContextProjectionLaneName = ContextProjectionLane["lane"];

export type ContextProjectionSubjectRequest =
  | { kind: "lead_thread" }
  | { kind: "subagent_task"; executionId: string };

export type ContextProjectionReadState = {
  data?: ThreadContextProjection | null;
  error: unknown;
  isLoading: boolean;
};

const EMPTY_CONTEXT_PROJECTION_READ_STATE: ContextProjectionReadState =
  Object.freeze({ error: null, isLoading: false });

type ProjectionListener = (state: ContextProjectionReadState) => void;
type ProjectionLoader = (
  subject: ContextProjectionSubjectRequest,
  signal: AbortSignal,
) => Promise<ThreadContextProjection | null>;
type ProjectionStreamOpener = (
  afterSeq: string,
  listener: (data: string) => void,
) => () => void;

function compareDecimalStrings(left: string, right: string): number {
  if (left.length !== right.length) return left.length - right.length;
  return left.localeCompare(right);
}

function subjectKey(subject: ContextProjectionSubjectRequest): string {
  return subject.kind === "lead_thread"
    ? "lead_thread"
    : `subagent_task:${uuidSchema.parse(subject.executionId)}`;
}

function responseSubjectKey(
  subject: ThreadContextProjection["subject"],
): string {
  return subject.kind === "lead_thread"
    ? "lead_thread"
    : `subagent_task:${subject.execution_id}`;
}

export function parseThreadContextProjection(
  value: unknown,
): ThreadContextProjection {
  return threadContextProjectionSchema.parse(value);
}

export function mergeThreadContextProjection(
  current: ThreadContextProjection | null | undefined,
  incoming: ThreadContextProjection,
): ThreadContextProjection {
  if (!current) return incoming;
  if (
    current.thread_id !== incoming.thread_id ||
    responseSubjectKey(current.subject) !== responseSubjectKey(incoming.subject)
  ) {
    throw new Error("Cannot merge different Context Subjects");
  }
  return compareDecimalStrings(
    incoming.projection_seq,
    current.projection_seq,
  ) > 0
    ? incoming
    : current;
}

export function threadContextUsageQueryKey(threadId?: string | null) {
  return ["thread-context-usage", threadId] as const;
}

export function threadContextProjectionStreamURL(
  apiBaseURL: string,
  threadId: string,
  afterSeq = "0",
) {
  const query = new URLSearchParams({
    after_seq: decimalStringSchema.parse(afterSeq),
  });
  return `${apiBaseURL}/threads/${encodeURIComponent(threadId)}/context-usage/stream?${query.toString()}`;
}

export async function fetchThreadContextUsage(
  threadId: string,
  options: Pick<ProjectPrivateWorkScope, "apiBaseURL"> & {
    subject?: ContextProjectionSubjectRequest;
    signal?: AbortSignal;
  },
): Promise<ThreadContextProjection | null> {
  const subject = options.subject ?? { kind: "lead_thread" as const };
  const query = new URLSearchParams();
  if (subject.kind === "subagent_task") {
    query.set("subject_kind", "subagent_task");
    query.set("subject_id", uuidSchema.parse(subject.executionId));
  }
  const queryString = query.toString();
  const response = await fetchWithAuth(
    `${options.apiBaseURL}/threads/${encodeURIComponent(threadId)}/context-usage${queryString ? `?${queryString}` : ""}`,
    { method: "GET", signal: options.signal },
  );

  if (!response.ok) {
    if (response.status === 403 || response.status === 404) return null;
    await throwGatewayApiError(response, "Failed to load context usage.");
  }
  const projection = parseThreadContextProjection(await response.json());
  if (projection.thread_id !== threadId) {
    throw new Error("Context Projection response belongs to another Thread");
  }
  if (responseSubjectKey(projection.subject) !== subjectKey(subject)) {
    throw new Error("Context Projection response belongs to another subject");
  }
  return projection;
}

export type ThreadContextProjectionReadModel = ReturnType<
  typeof createThreadContextProjectionReadModel
>;

export function createThreadContextProjectionReadModel({
  threadId,
  load,
  openStream,
  isActive,
  onEmpty,
}: {
  threadId: string;
  load: ProjectionLoader;
  openStream: ProjectionStreamOpener;
  isActive: () => boolean;
  onEmpty?: () => void;
}) {
  const states = new Map<string, ContextProjectionReadState>();
  const listeners = new Map<string, Set<ProjectionListener>>();
  const requests = new Map<string, AbortController>();
  const subjects = new Map<string, ContextProjectionSubjectRequest>();
  let stopStream: (() => void) | null = null;
  let highWaterProjectionSeq = "0";
  let disposed = false;
  let emptyEpoch = 0;

  function notify(key: string, state: ContextProjectionReadState) {
    listeners.get(key)?.forEach((listener) => listener(state));
  }

  function commit(
    key: string,
    update: (current: ContextProjectionReadState) => ContextProjectionReadState,
  ) {
    if (disposed || !isActive()) return;
    const current =
      states.get(key) ?? ({ error: null, isLoading: true } as const);
    const next = update(current);
    if (next === current) return;
    states.set(key, next);
    notify(key, next);
  }

  function acceptProjection(projection: ThreadContextProjection) {
    if (projection.thread_id !== threadId) return;
    if (
      compareDecimalStrings(projection.projection_seq, highWaterProjectionSeq) >
      0
    ) {
      highWaterProjectionSeq = projection.projection_seq;
    }
    const key = responseSubjectKey(projection.subject);
    if (!subjects.has(key)) return;
    commit(key, (current) => ({
      data: mergeThreadContextProjection(current.data, projection),
      error: null,
      isLoading: false,
    }));
  }

  function ensureStream() {
    if (stopStream || disposed) return;
    stopStream = openStream(highWaterProjectionSeq, (data) => {
      if (disposed || !isActive()) return;
      let payload: unknown;
      try {
        payload = JSON.parse(data);
      } catch {
        return;
      }
      const parsed = threadContextProjectionSchema.safeParse(payload);
      if (!parsed.success) return;
      acceptProjection(parsed.data);
    });
  }

  function reopenStream() {
    // Native EventSource owns transport retries and sends Last-Event-ID. This
    // seam is only for an explicit source replacement by the read-model owner.
    if (disposed || !isActive() || listeners.size === 0) return;
    stopStream?.();
    stopStream = null;
    ensureStream();
  }

  function ensureLoad(subject: ContextProjectionSubjectRequest, force = false) {
    const key = subjectKey(subject);
    if (requests.has(key) || (!force && states.get(key)?.data !== undefined)) {
      return;
    }
    const controller = new AbortController();
    subjects.set(key, subject);
    requests.set(key, controller);
    states.set(key, {
      ...states.get(key),
      error: null,
      isLoading: states.get(key)?.data === undefined,
    });
    void load(subject, controller.signal)
      .then((projection) => {
        if (projection === null) {
          commit(key, () => ({ data: null, error: null, isLoading: false }));
        } else {
          acceptProjection(projection);
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || !isActive()) return;
        commit(key, (current) => ({
          ...current,
          error,
          isLoading: false,
        }));
      })
      .finally(() => {
        if (requests.get(key) === controller) requests.delete(key);
      });
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    stopStream?.();
    stopStream = null;
    requests.forEach((controller) => controller.abort());
    requests.clear();
    states.clear();
    subjects.clear();
    listeners.clear();
    onEmpty?.();
  }

  return {
    subscribe(
      subject: ContextProjectionSubjectRequest,
      listener: ProjectionListener,
    ) {
      if (disposed) return () => undefined;
      emptyEpoch += 1;
      const key = subjectKey(subject);
      subjects.set(key, subject);
      const current = listeners.get(key) ?? new Set<ProjectionListener>();
      current.add(listener);
      listeners.set(key, current);
      ensureStream();
      ensureLoad(subject);
      return () => {
        current.delete(listener);
        if (current.size === 0) listeners.delete(key);
        if (listeners.size === 0) {
          const observedEpoch = ++emptyEpoch;
          queueMicrotask(() => {
            if (emptyEpoch === observedEpoch && listeners.size === 0) dispose();
          });
        }
      };
    },
    reopenStream,
    getSnapshot(
      subject: ContextProjectionSubjectRequest,
    ): ContextProjectionReadState {
      return (
        states.get(subjectKey(subject)) ?? EMPTY_CONTEXT_PROJECTION_READ_STATE
      );
    },
    refresh() {
      if (disposed || !isActive()) return;
      for (const [key, subject] of subjects) {
        requests.get(key)?.abort();
        requests.delete(key);
        states.set(key, {
          ...states.get(key),
          error: null,
          isLoading: states.get(key)?.data === undefined,
        });
        ensureLoad(subject, true);
      }
    },
    dispose,
  };
}

function sameScope(left: ProjectClientScope, right: ProjectClientScope) {
  return (
    left.accountId === right.accountId && left.projectId === right.projectId
  );
}

const scopedReadModels = new Map<
  ProjectPrivateWorkScope,
  Map<string, ThreadContextProjectionReadModel>
>();

function combineAbortSignals(
  localSignal: AbortSignal,
  scopeSignal?: AbortSignal,
): AbortSignal {
  if (!scopeSignal) return localSignal;
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([localSignal, scopeSignal]);
  }
  const controller = new AbortController();
  const abort = () => controller.abort();
  localSignal.addEventListener("abort", abort, { once: true });
  scopeSignal.addEventListener("abort", abort, { once: true });
  if (localSignal.aborted || scopeSignal.aborted) controller.abort();
  return controller.signal;
}

/**
 * Return the one live Context Projection read model for an authorized Thread
 * scope. Lead and Sub-Agent subscribers share its Thread-wide SSE cursor while
 * retaining independent subject snapshots.
 */
export function getThreadContextProjectionReadModel(
  privateWork: ProjectPrivateWorkScope,
  threadId: string,
): ThreadContextProjectionReadModel {
  const models = scopedReadModels.get(privateWork) ?? new Map();
  scopedReadModels.set(privateWork, models);
  const existing = models.get(threadId);
  if (existing) return existing;

  const model = createThreadContextProjectionReadModel({
    threadId,
    isActive: () => isPrivateWorkAccessActive(privateWork),
    load: (subject, localSignal) =>
      runPrivateWorkAbortable(privateWork, (scopeSignal) =>
        fetchThreadContextUsage(threadId, {
          apiBaseURL: privateWork.apiBaseURL,
          subject,
          signal: combineAbortSignals(localSignal, scopeSignal),
        }),
      ),
    openStream: (afterSeq, listener) =>
      privateWork.subscribeEventStream?.(
        threadContextProjectionStreamURL(
          privateWork.apiBaseURL,
          threadId,
          afterSeq,
        ),
        CONTEXT_PROJECTION_EVENT_NAME,
        listener,
      ) ?? (() => undefined),
    onEmpty: () => {
      if (models.get(threadId) === model) models.delete(threadId);
      if (models.size === 0) scopedReadModels.delete(privateWork);
    },
  });
  models.set(threadId, model);
  return model;
}

export function refreshThreadContextProjection(
  scope: ProjectClientScope,
  threadId: string,
): void {
  for (const [privateWork, models] of scopedReadModels) {
    if (
      sameScope(privateWork.scope, scope) &&
      isPrivateWorkAccessActive(privateWork)
    ) {
      models.get(threadId)?.refresh();
    }
  }
}

export function disposeThreadContextProjection(
  scope: ProjectClientScope,
  threadId: string,
): void {
  for (const [privateWork, models] of scopedReadModels) {
    if (sameScope(privateWork.scope, scope)) models.get(threadId)?.dispose();
  }
}
