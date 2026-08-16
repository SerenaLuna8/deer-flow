import type { Thread, ThreadState } from "@langchain/langgraph-sdk";
import type { Client as LangGraphClient } from "@langchain/langgraph-sdk/client";
import { z } from "zod";

import {
  clearReconnectRun,
  createProjectPrivateClient,
} from "@/core/api/api-client";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  projectClientScopeSchema,
  type ProjectClientScope,
  type RunMetadataStorage,
} from "./types";

type ProjectClientLifecycle = {
  active: boolean;
  controller: AbortController;
};

type ProjectClientEntry = ProjectClientLifecycle & {
  client: LangGraphClient;
  reconnectStorage: RunMetadataStorage;
};

const projectClients = new Map<string, ProjectClientEntry>();
const projectThreadVersions = new Map<string, Map<string, number>>();
const projectStreamCursorStates = new Map<string, ProjectStreamCursorState>();
const CANONICAL_POSITIVE_EVENT_ID = /^[1-9][0-9]*$/;
const CANONICAL_NONNEGATIVE_EVENT_ID = /^(?:0|[1-9][0-9]*)$/;
const POSTGRES_SIGNED_BIGINT_MAX = "9223372036854775807";

export type ProjectStreamCursorState = {
  // Keep the PostgreSQL BIGINT cursor as a canonical decimal string. JavaScript
  // numbers lose precision above 2^53 - 1 and could otherwise skip or replay
  // durable frames when a long-lived run_events sequence crosses that bound.
  lastEventId: string;
  terminalRunId: string | null;
};

export type ProjectStreamFrame = {
  id?: string;
  event: string;
  data: unknown;
};

export type ProjectStreamFrameDecision = {
  accepted: boolean;
  state: ProjectStreamCursorState;
};

export const PROJECT_RUN_TERMINAL_FAILURE = "PROJECT_RUN_TERMINAL_FAILURE";
export const PROJECT_STREAM_INCOMPLETE = "PROJECT_STREAM_INCOMPLETE";
export const MODEL_OUTPUT_LIMIT = "MODEL_OUTPUT_LIMIT";
export const OUTPUT_DELIVERY_INCOMPLETE = "OUTPUT_DELIVERY_INCOMPLETE";

export type ProjectRunFailureCode =
  | typeof MODEL_OUTPUT_LIMIT
  | typeof OUTPUT_DELIVERY_INCOMPLETE;

const FAILED_PROJECT_RUN_TERMINAL_STATUSES = new Set([
  "error",
  "failed",
  "timeout",
]);

export function projectStreamFailureName(
  frame: ProjectStreamFrame,
): ProjectRunFailureCode | null {
  if (
    (frame.event !== "error" && frame.event !== "end") ||
    typeof frame.data !== "object" ||
    frame.data === null
  ) {
    return null;
  }
  const name = Reflect.get(frame.data, "name");
  const legacyError = Reflect.get(frame.data, "error");
  const errorCode = Reflect.get(frame.data, "error_code");
  for (const failureCode of [
    MODEL_OUTPUT_LIMIT,
    OUTPUT_DELIVERY_INCOMPLETE,
  ] as const) {
    if (
      name === failureCode ||
      legacyError === failureCode ||
      errorCode === failureCode
    ) {
      return failureCode;
    }
  }
  return null;
}

function isProjectRunFailureCode(
  error: unknown,
  failureCode: ProjectRunFailureCode,
): boolean {
  if (error === failureCode) {
    return true;
  }
  if (typeof error !== "object" || error === null) {
    return false;
  }
  if (
    Reflect.get(error, "name") === failureCode ||
    Reflect.get(error, "message") === failureCode ||
    Reflect.get(error, "error_code") === failureCode
  ) {
    return true;
  }
  const nestedError = Reflect.get(error, "error");
  return (
    nestedError === failureCode ||
    (nestedError instanceof Error &&
      (nestedError.name === failureCode || nestedError.message === failureCode))
  );
}

export function isModelOutputLimitError(error: unknown): boolean {
  return isProjectRunFailureCode(error, MODEL_OUTPUT_LIMIT);
}

export function isOutputDeliveryIncompleteError(error: unknown): boolean {
  return isProjectRunFailureCode(error, OUTPUT_DELIVERY_INCOMPLETE);
}

export function projectStreamFrameForUI<T extends ProjectStreamFrame>(
  frame: T,
  precedingFailureName: ProjectRunFailureCode | null = null,
): T {
  if (frame.event !== "end" || typeof frame.data !== "object" || !frame.data) {
    return frame;
  }
  const failureName =
    projectStreamFailureName(frame) ??
    precedingFailureName ??
    PROJECT_RUN_TERMINAL_FAILURE;
  const status = Reflect.get(frame.data, "status");
  if (
    typeof status !== "string" ||
    !FAILED_PROJECT_RUN_TERMINAL_STATUSES.has(status)
  ) {
    return frame;
  }
  // LangGraph's stream consumer only enters its error state for `event:error`.
  // ActWeave's durable protocol closes every Run with `event:end` and carries
  // the authoritative outcome in `data.status`, so translate only failed
  // terminal outcomes while preserving the durable event ID/cursor.
  return {
    ...frame,
    event: "error",
    data: {
      error: failureName,
      message: failureName,
    },
  } as T;
}

export function isProjectRunTerminalFailure(error: unknown): boolean {
  return error instanceof Error && error.name === PROJECT_RUN_TERMINAL_FAILURE;
}

function assertDurableProjectStreamCompleted({
  started,
  terminal,
  signal,
  entry,
}: {
  started: boolean;
  terminal: boolean;
  signal: AbortSignal;
  entry: ProjectClientEntry;
}): void {
  if (
    !started ||
    terminal ||
    signal.aborted ||
    !isProjectClientEntryActive(entry)
  ) {
    return;
  }
  const error = new Error(
    "Project stream ended before its durable terminal frame.",
  );
  error.name = PROJECT_STREAM_INCOMPLETE;
  throw error;
}

export function emptyProjectStreamCursorState(): ProjectStreamCursorState {
  return { lastEventId: "0", terminalRunId: null };
}

function compareCanonicalDecimal(left: string, right: string): number {
  if (left.length !== right.length) {
    return left.length < right.length ? -1 : 1;
  }
  if (left === right) return 0;
  return left < right ? -1 : 1;
}

export function advanceProjectStreamCursorState(
  current: ProjectStreamCursorState,
  incoming: ProjectStreamCursorState,
): ProjectStreamCursorState {
  const order = compareCanonicalDecimal(
    incoming.lastEventId,
    current.lastEventId,
  );
  if (order < 0) return current;
  if (order > 0) return incoming;
  if (current.terminalRunId !== null || incoming.terminalRunId === null) {
    return current;
  }
  return incoming;
}

function isPostgresBigintEventId(
  value: unknown,
  allowZero: boolean,
): value is string {
  if (typeof value !== "string") return false;
  const pattern = allowZero
    ? CANONICAL_NONNEGATIVE_EVENT_ID
    : CANONICAL_POSITIVE_EVENT_ID;
  return (
    pattern.test(value) &&
    compareCanonicalDecimal(value, POSTGRES_SIGNED_BIGINT_MAX) <= 0
  );
}

export function acceptProjectStreamFrame(
  state: ProjectStreamCursorState,
  frame: ProjectStreamFrame,
  runId: string,
): ProjectStreamFrameDecision {
  if (
    typeof frame.id !== "string" ||
    !isPostgresBigintEventId(frame.id, false)
  ) {
    return { accepted: false, state };
  }
  if (compareCanonicalDecimal(frame.id, state.lastEventId) <= 0) {
    return { accepted: false, state };
  }
  return {
    accepted: true,
    state: {
      lastEventId: frame.id,
      terminalRunId:
        frame.event === "end" && runId.length > 0 ? runId : state.terminalRunId,
    },
  };
}

export function shouldReconnectProjectStream(
  state: ProjectStreamCursorState,
  runId: string,
): boolean {
  return state.terminalRunId !== runId;
}

const privateThreadSchema = z
  .object({
    thread_id: z.string().uuid(),
    agent_asset_id: z.string().uuid(),
    agent_scope: z.enum(["project", "system"]),
    display_name: z.string().nullable(),
    status: z.enum(["idle", "busy", "interrupted", "error"]),
    metadata: z.record(z.string(), z.unknown()),
    version: z.number().int().positive(),
    created_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict();

const privateThreadSearchSchema = z
  .object({ items: z.array(privateThreadSchema) })
  .strict();

const privateThreadStateSchema = z
  .object({
    values: z.record(z.string(), z.unknown()),
    next: z.array(z.string()),
    metadata: z.record(z.string(), z.unknown()),
    checkpoint: z.record(z.string(), z.unknown()),
    checkpoint_id: z.string().nullable(),
    parent_checkpoint_id: z.string().nullable(),
    created_at: z.string().nullable(),
    tasks: z.array(z.record(z.string(), z.unknown())),
  })
  .strict();

type PrivateThread = z.infer<typeof privateThreadSchema>;

export const PROJECT_RESPONSE_ERROR_CODES = [
  "DEFAULT_AGENT_UNAVAILABLE",
] as const;
export type ProjectResponseErrorCode =
  (typeof PROJECT_RESPONSE_ERROR_CODES)[number];
const projectResponseErrorBodySchema = z.object({
  detail: z.object({ code: z.enum(PROJECT_RESPONSE_ERROR_CODES) }),
});

class ProjectResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly response: Response,
    readonly code: ProjectResponseErrorCode | null,
  ) {
    super(message);
    this.name = "ProjectResponseError";
  }
}

export function isProjectResponseErrorCode<
  Code extends ProjectResponseErrorCode,
>(
  error: unknown,
  code: Code,
): error is Error & { readonly code: Code; readonly status: number } {
  return error instanceof ProjectResponseError && error.code === code;
}

type CreateProjectThreadBaseInput = {
  threadId: string;
  displayName?: string | null;
  metadata?: Record<string, unknown>;
  signal?: AbortSignal;
};

type ExplicitProjectThreadAgentInput = {
  agentAssetId: string;
  agentScope?: "project" | "system";
};

type DefaultProjectThreadAgentInput = {
  agentAssetId?: never;
  agentScope?: never;
};

export type CreateProjectThreadInput = CreateProjectThreadBaseInput &
  (ExplicitProjectThreadAgentInput | DefaultProjectThreadAgentInput);

function scopeKey(scope: ProjectClientScope): string {
  const parsed = projectClientScopeSchema.parse(scope);
  return `${parsed.accountId}:${parsed.projectId}`;
}

function versionCache(scope: ProjectClientScope): Map<string, number> {
  const key = scopeKey(scope);
  let versions = projectThreadVersions.get(key);
  if (!versions) {
    versions = new Map();
    projectThreadVersions.set(key, versions);
  }
  return versions;
}

function mapPrivateThread(
  scope: ProjectClientScope,
  value: PrivateThread,
): Thread {
  versionCache(scope).set(value.thread_id, value.version);
  return {
    thread_id: value.thread_id,
    created_at: value.created_at,
    updated_at: value.updated_at,
    state_updated_at: value.updated_at,
    metadata: {
      ...value.metadata,
      agent_asset_id: value.agent_asset_id,
      agent_scope: value.agent_scope,
      private_work_version: value.version,
    },
    status: value.status,
    values: {
      title: value.display_name ?? "",
      messages: [],
      artifacts: [],
      todos: [],
    },
    interrupts: {},
  };
}

function mapPrivateThreadState(
  threadId: string,
  value: z.infer<typeof privateThreadStateSchema>,
): ThreadState {
  const checkpoint = {
    thread_id: threadId,
    checkpoint_ns: "",
    checkpoint_id: value.checkpoint_id,
    checkpoint_map: null,
  };
  return {
    values: value.values,
    next: value.next,
    checkpoint,
    metadata: value.metadata,
    created_at: value.created_at,
    parent_checkpoint:
      value.parent_checkpoint_id == null
        ? null
        : { ...checkpoint, checkpoint_id: value.parent_checkpoint_id },
    tasks: value.tasks.map((task) => ({
      id: typeof task.id === "string" ? task.id : "",
      name: typeof task.name === "string" ? task.name : "",
      error: null,
      interrupts: [],
      checkpoint: null,
      state: null,
    })),
  };
}

async function readProjectResponse<T>(
  response: Response,
  schema: z.ZodType<T>,
  fallback: string,
): Promise<T> {
  if (!response.ok) {
    const error: unknown = await response
      .json()
      .catch(() => ({ detail: fallback }));
    const stableError = projectResponseErrorBodySchema.safeParse(error);
    const detail =
      typeof error === "object" && error !== null
        ? Reflect.get(error, "detail")
        : null;
    throw new ProjectResponseError(
      typeof detail === "string" ? detail : fallback,
      response.status,
      response,
      stableError.success ? stableError.data.detail.code : null,
    );
  }
  return schema.parse(await response.json());
}

async function getPrivateThread(
  scope: ProjectClientScope,
  threadId: string,
  signal?: AbortSignal,
): Promise<Thread> {
  const parsedThreadId = z.string().uuid().parse(threadId);
  const response = await fetchWithAuth(
    `${projectPrivateWorkBaseURL(scope.projectId)}/threads/${parsedThreadId}`,
    { signal },
  );
  const value = await readProjectResponse(
    response,
    privateThreadSchema,
    "Failed to load project thread",
  );
  return mapPrivateThread(scope, value);
}

async function currentThreadVersion(
  scope: ProjectClientScope,
  threadId: string,
  signal?: AbortSignal,
): Promise<number> {
  const cached = versionCache(scope).get(threadId);
  if (cached != null) return cached;
  await getPrivateThread(scope, threadId, signal);
  const loaded = versionCache(scope).get(threadId);
  if (loaded == null) throw new Error("Project thread version is unavailable");
  return loaded;
}

async function renamePrivateThread(
  scope: ProjectClientScope,
  threadId: string,
  displayName: string | null,
  signal?: AbortSignal,
): Promise<Thread> {
  const parsedThreadId = z.string().uuid().parse(threadId);
  const expectedVersion = await currentThreadVersion(
    scope,
    parsedThreadId,
    signal,
  );
  const response = await fetchWithAuth(
    `${projectPrivateWorkBaseURL(scope.projectId)}/threads/${parsedThreadId}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expected_version: expectedVersion,
        display_name: displayName,
      }),
      signal,
    },
  );
  const value = await readProjectResponse(
    response,
    privateThreadSchema,
    "Failed to rename project thread",
  );
  return mapPrivateThread(scope, value);
}

function installProjectThreadAdapter(
  client: LangGraphClient,
  scope: ProjectClientScope,
): void {
  client.threads.search = (async (query) => {
    const response = await fetchWithAuth(
      `${projectPrivateWorkBaseURL(scope.projectId)}/threads/search`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          limit: query?.limit ?? 10,
          offset: query?.offset ?? 0,
        }),
        signal: query?.signal,
      },
    );
    const result = await readProjectResponse(
      response,
      privateThreadSearchSchema,
      "Failed to search project threads",
    );
    return result.items.map((thread) => mapPrivateThread(scope, thread));
  }) as typeof client.threads.search;

  client.threads.get = ((threadId, options) =>
    getPrivateThread(
      scope,
      threadId,
      options?.signal,
    )) as typeof client.threads.get;

  client.threads.getHistory = (async (threadId, options) => {
    const parsedThreadId = z.string().uuid().parse(threadId);
    const response = await fetchWithAuth(
      `${projectPrivateWorkBaseURL(scope.projectId)}/threads/${parsedThreadId}/state`,
      { signal: options?.signal },
    );
    if (response.status === 404) return [];
    const state = await readProjectResponse(
      response,
      privateThreadStateSchema,
      "Failed to load project thread state",
    );
    return [mapPrivateThreadState(parsedThreadId, state)];
  }) as typeof client.threads.getHistory;

  client.threads.create = (async () => {
    throw new Error(
      "Project threads require an agent asset; use createProjectThread().",
    );
  }) as typeof client.threads.create;

  client.threads.updateState = (async (threadId, options) => {
    const values = options.values;
    const title =
      typeof values === "object" &&
      values !== null &&
      typeof Reflect.get(values, "title") === "string"
        ? String(Reflect.get(values, "title"))
        : null;
    if (title == null) {
      throw new Error("Project thread updates only support renaming by title.");
    }
    await renamePrivateThread(scope, threadId, title, options.signal);
    return { configurable: { thread_id: threadId } };
  }) as typeof client.threads.updateState;

  client.threads.update = (async (threadId, payload) => {
    const displayName = payload?.metadata?.display_name;
    const title = payload?.metadata?.title;
    if (typeof displayName === "string" || displayName === null) {
      return renamePrivateThread(scope, threadId, displayName, payload?.signal);
    }
    if (typeof title === "string") {
      return renamePrivateThread(scope, threadId, title, payload?.signal);
    }
    return getPrivateThread(scope, threadId, payload?.signal);
  }) as typeof client.threads.update;

  client.threads.delete = (async (threadId, options) => {
    const parsedThreadId = z.string().uuid().parse(threadId);
    const expectedVersion = await currentThreadVersion(
      scope,
      parsedThreadId,
      options?.signal,
    );
    const response = await fetchWithAuth(
      `${projectPrivateWorkBaseURL(scope.projectId)}/threads/${parsedThreadId}?expected_version=${expectedVersion}`,
      { method: "DELETE", signal: options?.signal },
    );
    if (!response.ok) {
      const error = await response
        .json()
        .catch(() => ({ detail: "Failed to delete project thread" }));
      throw new Error(
        typeof error.detail === "string"
          ? error.detail
          : "Failed to delete project thread",
      );
    }
    versionCache(scope).delete(parsedThreadId);
  }) as typeof client.threads.delete;
}

function browserSessionStorage(): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function projectReconnectKey(
  scope: ProjectClientScope,
  key: `lg:stream:${string}`,
): string {
  return `lg:stream:account:${scope.accountId}:project:${scope.projectId}:${key.slice("lg:stream:".length)}`;
}

function projectReconnectPrefix(scope: ProjectClientScope): string {
  return `lg:stream:account:${scope.accountId}:project:${scope.projectId}:`;
}

export function projectStreamCursorStorageKey(
  scope: ProjectClientScope,
  threadId: string,
): string {
  const parsed = projectClientScopeSchema.parse(scope);
  const selectedThreadId = z.string().min(1).parse(threadId);
  return `${projectReconnectPrefix(parsed)}cursor:${selectedThreadId}`;
}

function readProjectStreamCursorState(
  scope: ProjectClientScope,
  threadId: string,
): ProjectStreamCursorState {
  const key = projectStreamCursorStorageKey(scope, threadId);
  const cached = projectStreamCursorStates.get(key);
  if (cached) return cached;

  const storage = browserSessionStorage();
  if (storage) {
    try {
      const raw = storage.getItem(key);
      if (raw) {
        const value: unknown = JSON.parse(raw);
        if (typeof value === "object" && value !== null) {
          const lastEventId = Reflect.get(value, "lastEventId");
          const terminalRunId = Reflect.get(value, "terminalRunId");
          const normalizedLastEventId = isPostgresBigintEventId(
            lastEventId,
            true,
          )
            ? lastEventId
            : typeof lastEventId === "number" &&
                Number.isSafeInteger(lastEventId) &&
                lastEventId >= 0
              ? String(lastEventId)
              : null;
          if (
            normalizedLastEventId !== null &&
            (terminalRunId === null || typeof terminalRunId === "string")
          ) {
            const state: ProjectStreamCursorState = {
              lastEventId: normalizedLastEventId,
              terminalRunId,
            };
            projectStreamCursorStates.set(key, state);
            return state;
          }
        }
      }
    } catch {
      // A damaged browser entry is ignored and replaced after the next frame.
    }
  }
  const state = emptyProjectStreamCursorState();
  projectStreamCursorStates.set(key, state);
  return state;
}

function writeProjectStreamCursorState(
  scope: ProjectClientScope,
  threadId: string,
  state: ProjectStreamCursorState,
  lifecycle?: ProjectClientLifecycle,
): void {
  if (lifecycle && !lifecycle.active) return;
  const key = projectStreamCursorStorageKey(scope, threadId);
  const current = readProjectStreamCursorState(scope, threadId);
  const next = advanceProjectStreamCursorState(current, state);
  if (next === current) return;
  projectStreamCursorStates.set(key, next);
  try {
    browserSessionStorage()?.setItem(key, JSON.stringify(next));
  } catch {
    // The in-memory cursor still protects this mounted client from duplicates.
  }
}

function isAbortSignal(value: unknown): value is AbortSignal {
  return typeof AbortSignal !== "undefined" && value instanceof AbortSignal;
}

function mergeProjectStreamSignals(
  callerSignal: AbortSignal | undefined,
  scopeSignal: AbortSignal,
): AbortSignal {
  if (!callerSignal || callerSignal === scopeSignal) return scopeSignal;
  if (callerSignal.aborted) return callerSignal;
  if (scopeSignal.aborted) return scopeSignal;

  const anySignal = Reflect.get(AbortSignal, "any");
  if (typeof anySignal === "function") {
    return Reflect.apply(anySignal, AbortSignal, [[callerSignal, scopeSignal]]);
  }

  const controller = new AbortController();
  const abort = () => controller.abort();
  callerSignal.addEventListener("abort", abort, { once: true });
  scopeSignal.addEventListener("abort", abort, { once: true });
  return controller.signal;
}

function isProjectClientEntryActive(entry: ProjectClientEntry): boolean {
  return entry.active && !entry.controller.signal.aborted;
}

function streamFrameRunId(frame: ProjectStreamFrame): string | null {
  if (frame.event !== "metadata") return null;
  if (typeof frame.data !== "object" || frame.data === null) return null;
  const runId = Reflect.get(frame.data, "run_id");
  return typeof runId === "string" && runId.length > 0 ? runId : null;
}

function installProjectStreamAdapter(
  client: LangGraphClient,
  scope: ProjectClientScope,
  entry: ProjectClientEntry,
): void {
  const reconnectStorage = entry.reconnectStorage;
  const originalGetRun = client.runs.get.bind(client.runs);
  client.runs.get = ((threadId, runId, options) =>
    originalGetRun(threadId, runId, {
      ...options,
      signal: mergeProjectStreamSignals(
        options?.signal,
        entry.controller.signal,
      ),
    })) as typeof client.runs.get;

  const originalRunStream = client.runs.stream.bind(client.runs);
  client.runs.stream = async function* (threadId, assistantId, payload) {
    if (!isProjectClientEntryActive(entry)) return;
    let runId = "";
    const payloadSignal =
      payload && typeof payload === "object"
        ? Reflect.get(payload, "signal")
        : undefined;
    const callerOnRunCreated = payload?.onRunCreated;
    const selectedPayload = {
      ...(payload ?? {}),
      signal: mergeProjectStreamSignals(
        isAbortSignal(payloadSignal) ? payloadSignal : undefined,
        entry.controller.signal,
      ),
      onRunCreated(params: { run_id: string; thread_id?: string }) {
        runId = params.run_id;
        callerOnRunCreated?.(params);
      },
    };
    if (threadId == null) {
      for await (const frame of originalRunStream(
        threadId,
        assistantId,
        selectedPayload,
      )) {
        if (!isProjectClientEntryActive(entry)) return;
        yield frame;
      }
      return;
    }

    let state = emptyProjectStreamCursorState();
    let started = false;
    let terminal = false;
    let failureName: ProjectRunFailureCode | null = null;
    for await (const frame of originalRunStream(
      threadId,
      assistantId,
      selectedPayload,
    )) {
      if (!isProjectClientEntryActive(entry)) return;
      const projectFrame: ProjectStreamFrame = frame;
      runId = streamFrameRunId(projectFrame) ?? runId;
      const decision = acceptProjectStreamFrame(state, projectFrame, runId);
      if (!decision.accepted) continue;
      state = decision.state;
      started = true;
      terminal ||= projectFrame.event === "end";
      writeProjectStreamCursorState(scope, threadId, state, entry);
      if (state.terminalRunId !== null) {
        clearReconnectRun(threadId, state.terminalRunId, reconnectStorage);
      }
      if (projectFrame.event === "error") {
        // Worker failures are persisted as a diagnostic error frame followed
        // by the authoritative durable end frame. Exposing the first frame to
        // LangGraph's StreamManager would stop consumption before that end.
        failureName = projectStreamFailureName(projectFrame) ?? failureName;
        continue;
      }
      yield projectStreamFrameForUI(frame, failureName);
    }
    assertDurableProjectStreamCompleted({
      started,
      terminal,
      signal: selectedPayload.signal,
      entry,
    });
  } as typeof client.runs.stream;

  const originalJoinStream = client.runs.joinStream.bind(client.runs);
  client.runs.joinStream = async function* (threadId, runId, options) {
    if (!isProjectClientEntryActive(entry)) return;
    const callerSignal = isAbortSignal(options) ? options : options?.signal;
    const selectedOptions = {
      ...(isAbortSignal(options) ? {} : options),
      signal: mergeProjectStreamSignals(callerSignal, entry.controller.signal),
      // A newly mounted UI projection must rebuild the complete current Run.
      // Shared cursors are diagnostic only: an old invisible consumer may have
      // advanced one beyond frames the new UI has actually rendered.
      lastEventId: "0",
    };
    if (threadId == null) {
      for await (const frame of originalJoinStream(
        threadId,
        runId,
        selectedOptions,
      )) {
        if (!isProjectClientEntryActive(entry)) return;
        yield frame;
      }
      return;
    }

    let state = emptyProjectStreamCursorState();
    let started = false;
    let terminal = false;
    let failureName: ProjectRunFailureCode | null = null;
    for await (const frame of originalJoinStream(
      threadId,
      runId,
      selectedOptions,
    )) {
      if (!isProjectClientEntryActive(entry)) return;
      const projectFrame: ProjectStreamFrame = frame;
      const decision = acceptProjectStreamFrame(state, projectFrame, runId);
      if (!decision.accepted) continue;
      state = decision.state;
      started = true;
      terminal ||= projectFrame.event === "end";
      writeProjectStreamCursorState(scope, threadId, state, entry);
      if (state.terminalRunId === runId) {
        clearReconnectRun(threadId, runId, reconnectStorage);
      }
      if (projectFrame.event === "error") {
        failureName = projectStreamFailureName(projectFrame) ?? failureName;
        continue;
      }
      yield projectStreamFrameForUI(frame, failureName);
    }
    assertDurableProjectStreamCompleted({
      started,
      terminal,
      signal: selectedOptions.signal,
      entry,
    });
  } as typeof client.runs.joinStream;
}

function createProjectReconnectStorage(
  scope: ProjectClientScope,
  storageOverride?: Pick<Storage, "getItem" | "setItem" | "removeItem">,
  lifecycle?: ProjectClientLifecycle,
): RunMetadataStorage {
  const parsed = projectClientScopeSchema.parse(scope);
  const storage = () => storageOverride ?? browserSessionStorage();
  const ownedRunIds = new Map<`lg:stream:${string}`, string>();
  const isActive = () => !lifecycle || lifecycle.active;
  return {
    getItem(key) {
      if (!isActive()) return null;
      const value =
        storage()?.getItem(projectReconnectKey(parsed, key)) ?? null;
      if (value !== null) ownedRunIds.set(key, value);
      return value;
    },
    setItem(key, value) {
      if (!isActive()) return;
      storage()?.setItem(projectReconnectKey(parsed, key), value);
      ownedRunIds.set(key, value);
    },
    removeItem(key) {
      if (!isActive()) return;
      const ownedRunId = ownedRunIds.get(key);
      if (ownedRunId === undefined) return;
      const selectedStorage = storage();
      const scopedKey = projectReconnectKey(parsed, key);
      if (selectedStorage?.getItem(scopedKey) === ownedRunId) {
        selectedStorage.removeItem(scopedKey);
      }
      ownedRunIds.delete(key);
    },
  };
}

export function projectReconnectStorage(
  scope: ProjectClientScope,
  storageOverride?: Pick<Storage, "getItem" | "setItem" | "removeItem">,
): RunMetadataStorage {
  const parsed = projectClientScopeSchema.parse(scope);
  const entry = projectClients.get(scopeKey(parsed));
  return createProjectReconnectStorage(parsed, storageOverride, entry);
}

export function clearProjectReconnectStorage(scope: ProjectClientScope): void {
  const parsed = projectClientScopeSchema.parse(scope);
  const prefix = projectReconnectPrefix(parsed);
  for (const key of projectStreamCursorStates.keys()) {
    if (key.startsWith(prefix)) projectStreamCursorStates.delete(key);
  }
  const storage = browserSessionStorage();
  if (!storage) return;
  try {
    const keys = Array.from({ length: storage.length }, (_, index) =>
      storage.key(index),
    ).filter((key): key is string => Boolean(key?.startsWith(prefix)));
    keys.forEach((key) => storage.removeItem(key));
  } catch {
    // Storage cleanup is best effort; scope cancellation still prevents reuse.
  }
}

export function projectPrivateWorkBaseURL(projectId: string): string {
  const parsedProjectId =
    projectClientScopeSchema.shape.projectId.parse(projectId);
  const path = `/api/projects/${parsedProjectId}/private-work`;
  const backendBaseURL = getBackendBaseURL();
  if (backendBaseURL) return `${backendBaseURL}${path}`;
  const origin =
    typeof window === "undefined"
      ? "http://localhost:2026"
      : window.location.origin;
  return new URL(path, origin).toString().replace(/\/$/, "");
}

export function getProjectAPIClient(
  scope: ProjectClientScope,
): LangGraphClient {
  const parsed = projectClientScopeSchema.parse(scope);
  const key = scopeKey(parsed);
  let entry = projectClients.get(key);
  if (!entry) {
    const lifecycle: ProjectClientLifecycle = {
      active: true,
      controller: new AbortController(),
    };
    const reconnectStorage = createProjectReconnectStorage(
      parsed,
      undefined,
      lifecycle,
    );
    const client = createProjectPrivateClient({
      apiUrl: projectPrivateWorkBaseURL(parsed.projectId),
    });
    entry = Object.assign(lifecycle, {
      client,
      reconnectStorage,
    });
    installProjectThreadAdapter(client, parsed);
    installProjectStreamAdapter(client, parsed, entry);
    projectClients.set(key, entry);
  }
  return entry.client;
}

export async function createProjectThread(
  scope: ProjectClientScope,
  input: CreateProjectThreadInput,
): Promise<Thread> {
  const parsedScope = projectClientScopeSchema.parse(scope);
  const threadId = z.string().uuid().parse(input.threadId);
  if (input.agentAssetId === undefined && input.agentScope !== undefined) {
    throw new TypeError("Agent scope requires an Agent asset ID");
  }
  const agentSelection =
    input.agentAssetId === undefined
      ? {}
      : {
          agent_asset_id: z.string().uuid().parse(input.agentAssetId),
          agent_scope: input.agentScope ?? "project",
        };
  const response = await fetchWithAuth(
    `${projectPrivateWorkBaseURL(parsedScope.projectId)}/threads`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: threadId,
        ...agentSelection,
        display_name: input.displayName ?? null,
        metadata: input.metadata ?? {},
      }),
      signal: input.signal,
    },
  );
  const value = await readProjectResponse(
    response,
    privateThreadSchema,
    "Failed to create project thread",
  );
  return mapPrivateThread(parsedScope, value);
}

export function disposeProjectAPIClient(scope: ProjectClientScope): void {
  const key = scopeKey(scope);
  const entry = projectClients.get(key);
  if (entry) {
    entry.active = false;
    entry.controller.abort();
  }
  projectClients.delete(key);
  projectThreadVersions.delete(key);
}
