import type { QueryClient } from "@tanstack/react-query";

import { getAPIClient } from "@/core/api";
import { getBackendBaseURL } from "@/core/config";

import {
  clearProjectReconnectStorage,
  disposeProjectAPIClient,
  getProjectAPIClient,
  projectPrivateWorkBaseURL,
  projectReconnectStorage,
} from "./api-client";
import { privateWorkRoot } from "./query-keys";
import {
  projectClientScopeSchema,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "./types";

export { projectReconnectStorage } from "./api-client";

function scopeKey(scope: ProjectClientScope): string {
  const parsed = projectClientScopeSchema.parse(scope);
  return `${parsed.accountId}:${parsed.projectId}`;
}

function sameScope(
  left: ProjectClientScope | null,
  right: ProjectClientScope | null,
): boolean {
  if (left === null || right === null) return left === right;
  return scopeKey(left) === scopeKey(right);
}

export class PrivateWorkScopeRegistry {
  private readonly entries = new Map<string, PrivateWorkAccess>();
  private readonly abortControllers = new Map<string, Set<AbortController>>();

  acquire(scope: ProjectClientScope): PrivateWorkAccess {
    const parsed = projectClientScopeSchema.parse(scope);
    const key = scopeKey(parsed);
    let access = this.entries.get(key);
    if (!access) {
      access = {
        scope: parsed,
        client: getProjectAPIClient(parsed),
        apiBaseURL: projectPrivateWorkBaseURL(parsed.projectId),
        queryKeyPrefix: privateWorkRoot(parsed),
        reconnectOnMount: () => projectReconnectStorage(parsed),
      };
      this.entries.set(key, access);
    }
    return access;
  }

  has(scope: ProjectClientScope): boolean {
    return this.entries.has(scopeKey(scope));
  }

  createAbortController(scope: ProjectClientScope): AbortController {
    const key = scopeKey(scope);
    const controller = new AbortController();
    const controllers = this.abortControllers.get(key) ?? new Set();
    controllers.add(controller);
    this.abortControllers.set(key, controllers);
    return controller;
  }

  dispose(scope: ProjectClientScope): void {
    const parsed = projectClientScopeSchema.parse(scope);
    const key = scopeKey(parsed);
    this.abortControllers.get(key)?.forEach((controller) => controller.abort());
    this.abortControllers.delete(key);
    clearProjectReconnectStorage(parsed);
    disposeProjectAPIClient(parsed);
    this.entries.delete(key);
  }
}

export function createPrivateWorkScopeRegistry(): PrivateWorkScopeRegistry {
  return new PrivateWorkScopeRegistry();
}

export function getDefaultPrivateWorkAccess(isMock = false): PrivateWorkAccess {
  const backendBaseURL = getBackendBaseURL();
  return {
    scope: null,
    client: getAPIClient(isMock),
    apiBaseURL: `${backendBaseURL}/api`,
    queryKeyPrefix: [],
    reconnectOnMount: true,
  };
}

export async function transitionPrivateWorkScope(
  registry: PrivateWorkScopeRegistry,
  queryClient: QueryClient,
  previous: ProjectClientScope | null,
  next: ProjectClientScope | null,
): Promise<boolean> {
  if (sameScope(previous, next)) return false;
  if (!previous) return true;

  const root = privateWorkRoot(previous);
  await queryClient.cancelQueries({ queryKey: root });
  queryClient.removeQueries({ queryKey: root });
  queryClient
    .getMutationCache()
    .findAll({ mutationKey: root })
    .forEach((mutation) => queryClient.getMutationCache().remove(mutation));
  registry.dispose(previous);
  return true;
}
