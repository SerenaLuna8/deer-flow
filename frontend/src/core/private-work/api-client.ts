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

const privateThreadSchema = z
  .object({
    thread_id: z.string().uuid(),
    agent_asset_id: z.string().uuid(),
    agent_scope: z.enum(["project", "system"]),
    display_name: z.string().nullable(),
    status: z.enum(["idle", "busy", "interrupted", "error"]),
    metadata: z.record(z.string(), z.unknown()),
    version: z.number().int().positive(),
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

function privateThreadTimestamp(metadata: Record<string, unknown>): string {
  const timestamp = metadata.updated_at ?? metadata.created_at;
  return typeof timestamp === "string"
    ? timestamp
    : "1970-01-01T00:00:00.000Z";
}

function mapPrivateThread(
  scope: ProjectClientScope,
  value: PrivateThread,
): Thread {
  versionCache(scope).set(value.thread_id, value.version);
  const timestamp = privateThreadTimestamp(value.metadata);
  return {
    thread_id: value.thread_id,
    created_at: timestamp,
    updated_at: timestamp,
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
    throw new Error(
      typeof error.detail === "string" ? error.detail : fallback,
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
    getPrivateThread(scope, threadId, options?.signal)) as typeof client.threads.get;

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
      return renamePrivateThread(
        scope,
        threadId,
        displayName,
        payload?.signal,
      );
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
  const storage = browserSessionStorage();
  if (!storage) return;
  const prefix = `lg:stream:account:${parsed.accountId}:project:${parsed.projectId}:`;
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
