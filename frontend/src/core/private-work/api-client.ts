import type { Thread, ThreadState } from "@langchain/langgraph-sdk";
import type { Client as LangGraphClient } from "@langchain/langgraph-sdk/client";
import { z } from "zod";

import { createCompatibleClient } from "@/core/api/api-client";
import { fetch as fetchWithAuth } from "@/core/api/fetcher";
import { getBackendBaseURL } from "@/core/config";

import {
  projectClientScopeSchema,
  type ProjectClientScope,
  type RunMetadataStorage,
} from "./types";

const projectClients = new Map<string, LangGraphClient>();
const projectThreadVersions = new Map<string, Map<string, number>>();
const projectStreamCursorStates = new Map<string, ProjectStreamCursorState>();
const CANONICAL_POSITIVE_EVENT_ID = /^[1-9][0-9]*$/;

export type ProjectStreamCursorState = {
  lastEventId: number;
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

const FAILED_PROJECT_RUN_TERMINAL_STATUSES = new Set([
  "error",
  "failed",
  "timeout",
]);

export function projectStreamFrameForUI<T extends ProjectStreamFrame>(
  frame: T,
): T {
  if (frame.event !== "end" || typeof frame.data !== "object" || !frame.data) {
    return frame;
  }
  const status = Reflect.get(frame.data, "status");
  if (
    typeof status !== "string" ||
    !FAILED_PROJECT_RUN_TERMINAL_STATUSES.has(status)
  ) {
    return frame;
  }
  // LangGraph's stream consumer only enters its error state for `event:error`.
  // DeerFlow's durable protocol closes every Run with `event:end` and carries
  // the authoritative outcome in `data.status`, so translate only failed
  // terminal outcomes while preserving the durable event ID/cursor.
  return {
    ...frame,
    event: "error",
    data: {
      error: PROJECT_RUN_TERMINAL_FAILURE,
      message: PROJECT_RUN_TERMINAL_FAILURE,
    },
  } as T;
}

export function isProjectRunTerminalFailure(error: unknown): boolean {
  return error instanceof Error && error.name === PROJECT_RUN_TERMINAL_FAILURE;
}

export function emptyProjectStreamCursorState(): ProjectStreamCursorState {
  return { lastEventId: 0, terminalRunId: null };
}

export function acceptProjectStreamFrame(
  state: ProjectStreamCursorState,
  frame: ProjectStreamFrame,
  runId: string,
): ProjectStreamFrameDecision {
  if (
    typeof frame.id !== "string" ||
    !CANONICAL_POSITIVE_EVENT_ID.test(frame.id)
  ) {
    return { accepted: false, state };
  }
  const eventId = Number(frame.id);
  if (!Number.isSafeInteger(eventId) || eventId <= state.lastEventId) {
    return { accepted: false, state };
  }
  return {
    accepted: true,
    state: {
      lastEventId: eventId,
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

class ProjectResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly response: Response,
  ) {
    super(message);
    this.name = "ProjectResponseError";
  }
}

export type CreateProjectThreadInput = {
  threadId: string;
  agentAssetId: string;
  agentScope?: "project" | "system";
  displayName?: string | null;
  metadata?: Record<string, unknown>;
  signal?: AbortSignal;
};

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
    const error = await response.json().catch(() => ({ detail: fallback }));
    throw new ProjectResponseError(
      typeof error.detail === "string" ? error.detail : fallback,
      response.status,
      response,
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
          if (
            Number.isSafeInteger(lastEventId) &&
            typeof lastEventId === "number" &&
            lastEventId >= 0 &&
            (terminalRunId === null || typeof terminalRunId === "string")
          ) {
            const state = { lastEventId, terminalRunId };
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
): void {
  const key = projectStreamCursorStorageKey(scope, threadId);
  projectStreamCursorStates.set(key, state);
  try {
    browserSessionStorage()?.setItem(key, JSON.stringify(state));
  } catch {
    // The in-memory cursor still protects this mounted client from duplicates.
  }
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
): void {
  const reconnectStorage = projectReconnectStorage(scope);
  const originalRunStream = client.runs.stream.bind(client.runs);
  client.runs.stream = async function* (threadId, assistantId, payload) {
    if (threadId == null) {
      yield* originalRunStream(threadId, assistantId, payload);
      return;
    }

    let state = readProjectStreamCursorState(scope, threadId);
    if (state.terminalRunId !== null) {
      state = { ...state, terminalRunId: null };
      writeProjectStreamCursorState(scope, threadId, state);
    }
    let runId = "";
    for await (const frame of originalRunStream(
      threadId,
      assistantId,
      payload,
    )) {
      runId = streamFrameRunId(frame) ?? runId;
      const decision = acceptProjectStreamFrame(state, frame, runId);
      if (!decision.accepted) continue;
      state = decision.state;
      writeProjectStreamCursorState(scope, threadId, state);
      if (state.terminalRunId !== null) {
        reconnectStorage.removeItem(`lg:stream:${threadId}`);
      }
      yield projectStreamFrameForUI(frame);
    }
  } as typeof client.runs.stream;

  const originalJoinStream = client.runs.joinStream.bind(client.runs);
  client.runs.joinStream = async function* (threadId, runId, options) {
    if (threadId == null) {
      yield* originalJoinStream(threadId, runId, options);
      return;
    }

    let state = readProjectStreamCursorState(scope, threadId);
    if (!shouldReconnectProjectStream(state, runId)) {
      reconnectStorage.removeItem(`lg:stream:${threadId}`);
      return;
    }
    // The durable cursor intentionally resumes after frames the browser has
    // already consumed. A route change clears the SDK's in-memory projection,
    // though, so replaying only the tail would omit the current Human message
    // and Lead Agent task call. Hydrate the latest checkpoint first, without an
    // event id (and therefore without advancing or rewinding the durable
    // cursor), then continue with the exact persisted Last-Event-ID.
    if (
      reconnectStorage.getItem(`lg:stream:${threadId}`) === runId
    ) {
      try {
        const latestState = (
          await client.threads.getHistory(threadId, { limit: 1 })
        )[0];
        if (latestState?.values) {
          yield {
            event: "values",
            data: latestState.values,
          };
        }
      } catch {
        // Snapshot hydration is best effort. The durable tail remains the
        // authoritative reconnect path and must still be attempted.
      }
    }
    const selectedOptions =
      typeof AbortSignal !== "undefined" && options instanceof AbortSignal
        ? { signal: options, lastEventId: String(state.lastEventId) }
        : { ...options, lastEventId: String(state.lastEventId) };
    for await (const frame of originalJoinStream(
      threadId,
      runId,
      selectedOptions,
    )) {
      const decision = acceptProjectStreamFrame(state, frame, runId);
      if (!decision.accepted) continue;
      state = decision.state;
      writeProjectStreamCursorState(scope, threadId, state);
      if (state.terminalRunId === runId) {
        reconnectStorage.removeItem(`lg:stream:${threadId}`);
      }
      yield projectStreamFrameForUI(frame);
    }
  } as typeof client.runs.joinStream;
}

export function projectReconnectStorage(
  scope: ProjectClientScope,
  storageOverride?: Pick<Storage, "getItem" | "setItem" | "removeItem">,
): RunMetadataStorage {
  const parsed = projectClientScopeSchema.parse(scope);
  const storage = () => storageOverride ?? browserSessionStorage();
  return {
    getItem(key) {
      return storage()?.getItem(projectReconnectKey(parsed, key)) ?? null;
    },
    setItem(key, value) {
      storage()?.setItem(projectReconnectKey(parsed, key), value);
    },
    removeItem(key) {
      storage()?.removeItem(projectReconnectKey(parsed, key));
    },
  };
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
  let client = projectClients.get(key);
  if (!client) {
    client = createCompatibleClient({
      apiUrl: projectPrivateWorkBaseURL(parsed.projectId),
      runMetadataStorage: projectReconnectStorage(parsed),
    });
    installProjectThreadAdapter(client, parsed);
    installProjectStreamAdapter(client, parsed);
    projectClients.set(key, client);
  }
  return client;
}

export async function createProjectThread(
  scope: ProjectClientScope,
  input: CreateProjectThreadInput,
): Promise<Thread> {
  const parsedScope = projectClientScopeSchema.parse(scope);
  const threadId = z.string().uuid().parse(input.threadId);
  const agentAssetId = z.string().uuid().parse(input.agentAssetId);
  const response = await fetchWithAuth(
    `${projectPrivateWorkBaseURL(parsedScope.projectId)}/threads`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        thread_id: threadId,
        agent_asset_id: agentAssetId,
        agent_scope: input.agentScope ?? "project",
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
  projectClients.delete(key);
  projectThreadVersions.delete(key);
}
