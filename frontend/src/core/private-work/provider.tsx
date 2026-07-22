"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef } from "react";

import {
  createPrivateWorkScopeRegistry,
  transitionPrivateWorkScope,
} from "./scope-registry";
import type {
  PrivateWorkAccess,
  ProjectClientScope,
  ProjectPrivateWorkScope,
} from "./types";

const PrivateWorkContext = createContext<PrivateWorkAccess | undefined>(
  undefined,
);

type DeferredScopeRelease = {
  scope: ProjectClientScope;
  cancelled: boolean;
};

function sameProjectScope(
  left: ProjectClientScope,
  right: ProjectClientScope,
): boolean {
  return (
    left.accountId === right.accountId && left.projectId === right.projectId
  );
}

export function createDeferredPrivateWorkScopeRelease(
  schedule: (task: () => void) => void = queueMicrotask,
) {
  const pending = new Set<DeferredScopeRelease>();
  return {
    retain(scope: ProjectClientScope) {
      for (const release of pending) {
        if (sameProjectScope(release.scope, scope)) {
          release.cancelled = true;
        }
      }
    },
    defer(scope: ProjectClientScope, dispose: () => void) {
      const release: DeferredScopeRelease = { scope, cancelled: false };
      pending.add(release);
      schedule(() => {
        pending.delete(release);
        if (!release.cancelled) dispose();
      });
    },
  };
}

export function PrivateWorkProvider({
  access,
  children,
}: {
  access: PrivateWorkAccess;
  children: React.ReactNode;
}) {
  return (
    <PrivateWorkContext.Provider value={access}>
      {children}
    </PrivateWorkContext.Provider>
  );
}

export function ProjectPrivateWorkProvider({
  accountId,
  projectId,
  children,
}: ProjectClientScope & { children?: React.ReactNode }) {
  const queryClient = useQueryClient();
  const registryRef = useRef<ReturnType<
    typeof createPrivateWorkScopeRegistry
  > | null>(null);
  registryRef.current ??= createPrivateWorkScopeRegistry();
  const registry = registryRef.current;
  const deferredReleaseRef = useRef<ReturnType<
    typeof createDeferredPrivateWorkScopeRelease
  > | null>(null);
  deferredReleaseRef.current ??= createDeferredPrivateWorkScopeRelease();
  const deferredRelease = deferredReleaseRef.current;
  const scope = useMemo(
    () => ({ accountId, projectId }),
    [accountId, projectId],
  );
  const access = useMemo(() => registry.acquire(scope), [registry, scope]);

  useEffect(() => {
    deferredRelease.retain(scope);
    return () => {
      deferredRelease.defer(scope, () => {
        void transitionPrivateWorkScope(registry, queryClient, scope, null);
      });
    };
  }, [deferredRelease, queryClient, registry, scope]);

  return <PrivateWorkProvider access={access}>{children}</PrivateWorkProvider>;
}

export function usePrivateWorkAccess(
  explicit?: ProjectPrivateWorkScope,
): ProjectPrivateWorkScope {
  const contextual = useContext(PrivateWorkContext);
  const access = explicit ?? contextual;
  if (!access) {
    throw new Error("Private work access requires a project provider");
  }
  return access;
}

export function useProjectPrivateWorkScope(
  explicit?: ProjectPrivateWorkScope,
): ProjectPrivateWorkScope {
  return usePrivateWorkAccess(explicit);
}
