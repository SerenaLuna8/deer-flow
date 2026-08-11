"use client";

import { skipToken, useQuery } from "@tanstack/react-query";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  isPrivateWorkAccessActive,
  type PrivateWorkAccess,
  type ProjectClientScope,
} from "@/core/private-work/types";

import type { NodeCatalogResponseV1 } from "./catalog";
import { projectWorkflowQueryKey } from "./query-keys";
import type { WorkflowProjectReadinessV1 } from "./transport";

export type WorkflowReadinessTransport = {
  readProjectWorkflowReadiness(
    scope: ProjectClientScope,
    options: { signal: AbortSignal },
  ): Promise<WorkflowProjectReadinessV1>;
};

export type WorkflowNodeCatalogTransport = {
  readProjectWorkflowNodeCatalog(
    scope: ProjectClientScope,
    options: { signal: AbortSignal },
  ): Promise<NodeCatalogResponseV1>;
};

function inactiveScopeError(): Error {
  const error = new Error("Project Workflow scope is inactive");
  error.name = "AbortError";
  return error;
}

export function projectWorkflowReadinessQueryOptions(
  access: PrivateWorkAccess,
  canReadWorkflow: boolean,
  transport: WorkflowReadinessTransport,
) {
  const scope = access.scope;
  return {
    queryKey: projectWorkflowQueryKey(scope, "readiness"),
    queryFn: canReadWorkflow
      ? async ({ signal }: { signal: AbortSignal }) => {
          const result = await transport.readProjectWorkflowReadiness(scope, {
            signal,
          });
          if (!isPrivateWorkAccessActive(access)) {
            throw inactiveScopeError();
          }
          return result;
        }
      : skipToken,
    enabled: canReadWorkflow,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export function useProjectWorkflowReadiness(
  canReadWorkflow: boolean,
  transport: WorkflowReadinessTransport,
) {
  const access = usePrivateWorkAccess();
  return useQuery(
    projectWorkflowReadinessQueryOptions(access, canReadWorkflow, transport),
  );
}

export function projectWorkflowNodeCatalogQueryOptions(
  access: PrivateWorkAccess,
  canReadWorkflow: boolean,
  transport: WorkflowNodeCatalogTransport,
) {
  const scope = access.scope;
  return {
    queryKey: projectWorkflowQueryKey(scope, "node-catalog"),
    queryFn: canReadWorkflow
      ? async ({ signal }: { signal: AbortSignal }) => {
          const result = await transport.readProjectWorkflowNodeCatalog(scope, {
            signal,
          });
          if (!isPrivateWorkAccessActive(access)) {
            throw inactiveScopeError();
          }
          return result;
        }
      : skipToken,
    enabled: canReadWorkflow,
    retry: false,
    refetchOnWindowFocus: false,
  } as const;
}

export function useProjectWorkflowNodeCatalog(
  canReadWorkflow: boolean,
  transport: WorkflowNodeCatalogTransport,
) {
  const access = usePrivateWorkAccess();
  return useQuery(
    projectWorkflowNodeCatalogQueryOptions(access, canReadWorkflow, transport),
  );
}
