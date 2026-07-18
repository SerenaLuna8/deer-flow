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
  const scope = useMemo(
    () => ({ accountId, projectId }),
    [accountId, projectId],
  );
  const access = useMemo(() => registry.acquire(scope), [registry, scope]);

  useEffect(() => {
    return () => {
      void transitionPrivateWorkScope(registry, queryClient, scope, null);
    };
  }, [queryClient, registry, scope]);

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
