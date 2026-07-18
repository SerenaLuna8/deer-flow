import { useQuery } from "@tanstack/react-query";

import { useProjectPrivateWorkScope } from "@/core/private-work/provider";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";

import { fetchWorkspaceChanges } from "./api";
import type { WorkspaceChangesResponse } from "./types";

export function workspaceChangesQueryKey(
  scope: Parameters<typeof privateWorkQueryKey>[0],
  threadId: string | undefined,
  runId: string | undefined,
  includeFiles: boolean,
  includeDiff: boolean,
) {
  return privateWorkQueryKey(
    scope,
    "workspace-changes",
    threadId,
    runId,
    includeFiles,
    includeDiff,
  );
}

export function useWorkspaceChanges({
  threadId,
  runId,
  includeFiles = true,
  includeDiff = true,
  enabled = true,
}: {
  threadId?: string;
  runId?: string;
  includeFiles?: boolean;
  includeDiff?: boolean;
  enabled?: boolean;
}) {
  const privateWork = useProjectPrivateWorkScope();
  return useQuery<WorkspaceChangesResponse>({
    queryKey: workspaceChangesQueryKey(
      privateWork.scope,
      threadId,
      runId,
      includeFiles,
      includeDiff,
    ),
    queryFn: () => {
      if (!threadId || !runId) {
        throw new Error("threadId and runId are required");
      }
      return fetchWorkspaceChanges({
        privateWork,
        threadId,
        runId,
        includeFiles,
        includeDiff,
      });
    },
    enabled: enabled && Boolean(threadId) && Boolean(runId),
    retry: false,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  });
}
