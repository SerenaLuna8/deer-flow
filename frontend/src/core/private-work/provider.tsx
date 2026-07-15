"use client";

import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef } from "react";

import {
  createPrivateWorkScopeRegistry,
  getDefaultPrivateWorkAccess,
  transitionPrivateWorkScope,
} from "./scope-registry";
import type { PrivateWorkAccess, ProjectClientScope } from "./types";

const PrivateWorkContext = createContext<PrivateWorkAccess | undefined>(
  undefined,
);

const defaultAccess = getDefaultPrivateWorkAccess();
const mockAccess = getDefaultPrivateWorkAccess(true);

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
}: ProjectClientScope & { children: React.ReactNode }) {
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
  explicit?: PrivateWorkAccess,
  isMock = false,
): PrivateWorkAccess {
  const contextual = useContext(PrivateWorkContext);
  return explicit ?? contextual ?? (isMock ? mockAccess : defaultAccess);
}
