import type { QueryClient } from "@tanstack/react-query";

import { agentBuilderRootKey } from "@/core/agent-builder/query-keys";
import { automationRoot } from "@/core/project-automations/query-keys";
import { governanceRoot } from "@/core/project-governance/query-keys";
import { projectSharedAssetRoot } from "@/core/shared-assets/query-keys";

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
      const nextAccess: PrivateWorkAccess = {
        scope: parsed,
        client: getProjectAPIClient(parsed),
        apiBaseURL: projectPrivateWorkBaseURL(parsed.projectId),
        queryKeyPrefix: privateWorkRoot(parsed),
        reconnectOnMount: () => projectReconnectStorage(parsed),
        runAbortable: async <T>(
          operation: (signal: AbortSignal) => Promise<T>,
        ) => {
          if (this.entries.get(key) !== nextAccess) {
            const error = new Error("Private-work scope is inactive");
            error.name = "AbortError";
            throw error;
          }
          const controller = this.createAbortController(parsed);
          try {
            return await operation(controller.signal);
          } finally {
            this.releaseAbortController(parsed, controller);
          }
        },
        isActive: () => this.entries.get(key) === nextAccess,
      };
      access = nextAccess;
      this.entries.set(key, nextAccess);
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

  private releaseAbortController(
    scope: ProjectClientScope,
    controller: AbortController,
  ): void {
    const key = scopeKey(scope);
    const controllers = this.abortControllers.get(key);
    controllers?.delete(controller);
    if (controllers?.size === 0) {
      this.abortControllers.delete(key);
    }
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

export async function transitionPrivateWorkScope(
  registry: PrivateWorkScopeRegistry,
  queryClient: QueryClient,
  previous: ProjectClientScope | null,
  next: ProjectClientScope | null,
): Promise<boolean> {
  if (sameScope(previous, next)) return false;
  if (!previous) return true;

  const roots = [
    privateWorkRoot(previous),
    agentBuilderRootKey(previous.accountId, previous.projectId),
    automationRoot(previous),
    governanceRoot(previous),
    projectSharedAssetRoot(previous),
  ];
  const cancellations = roots.map((queryKey) =>
    queryClient.cancelQueries({ queryKey }),
  );
  await Promise.all(cancellations);
  registry.dispose(previous);
  for (const root of roots) {
    queryClient.removeQueries({ queryKey: root });
    queryClient
      .getMutationCache()
      .findAll({ mutationKey: root })
      .forEach((mutation) => queryClient.getMutationCache().remove(mutation));
  }
  return true;
}
