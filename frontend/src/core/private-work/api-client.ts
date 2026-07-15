import type { Client as LangGraphClient } from "@langchain/langgraph-sdk/client";

import { createCompatibleClient } from "@/core/api/api-client";
import { getBackendBaseURL } from "@/core/config";

import {
  projectClientScopeSchema,
  type ProjectClientScope,
  type RunMetadataStorage,
} from "./types";

const projectClients = new Map<string, LangGraphClient>();

function scopeKey(scope: ProjectClientScope): string {
  const parsed = projectClientScopeSchema.parse(scope);
  return `${parsed.accountId}:${parsed.projectId}`;
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
    projectClients.set(key, client);
  }
  return client;
}

export function disposeProjectAPIClient(scope: ProjectClientScope): void {
  projectClients.delete(scopeKey(scope));
}
