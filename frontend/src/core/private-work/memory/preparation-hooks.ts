"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { GatewayApiError } from "@/core/api/errors";
import {
  runPrivateWorkAbortable,
  type PrivateWorkAccess,
} from "@/core/private-work/types";

import {
  admitProjectMemoryDreamPreparation,
  cancelProjectMemoryDreamPreparation,
  getLatestProjectMemoryDreamPreparation,
} from "./api";
import {
  projectMemoryLatestDreamPreparationQueryKey,
  projectMemoryMutationKey,
  projectMemoryRootQueryKey,
} from "./query-keys";
import type { MemoryDreamPreparationStatus } from "./types";

export const MEMORY_DREAM_PREPARATION_POLL_INTERVAL_MS = 2_000;

export function memoryDreamPreparationIsActive(
  value: MemoryDreamPreparationStatus | null | undefined,
) {
  return value?.status === "queued" || value?.status === "running";
}

export function useMemoryDreamPreparation({
  privateWork,
  threadId,
  enabled,
}: {
  privateWork: PrivateWorkAccess;
  threadId: string;
  enabled: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = enabled
    ? projectMemoryLatestDreamPreparationQueryKey(privateWork.scope, threadId)
    : ([
        ...projectMemoryRootQueryKey(privateWork.scope),
        "dream-preparation",
        "disabled",
      ] as const);
  const statusQuery = useQuery({
    queryKey,
    queryFn: async ({ signal }) => {
      try {
        return await getLatestProjectMemoryDreamPreparation(
          privateWork,
          threadId,
          signal,
        );
      } catch (error) {
        if (error instanceof GatewayApiError && error.status === 404) {
          return null;
        }
        throw error;
      }
    },
    enabled,
    refetchInterval: (query) =>
      memoryDreamPreparationIsActive(query.state.data)
        ? MEMORY_DREAM_PREPARATION_POLL_INTERVAL_MS
        : false,
    refetchIntervalInBackground: false,
    refetchOnMount: "always",
    refetchOnReconnect: "always",
    refetchOnWindowFocus: "always",
  });
  const startMutation = useMutation({
    mutationKey: projectMemoryMutationKey(privateWork.scope, "dream-prepare"),
    mutationFn: (operationId: string) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        admitProjectMemoryDreamPreparation(
          privateWork,
          { threadId, operationId },
          signal,
        ),
      ),
    onSuccess: async () => {
      await statusQuery.refetch();
    },
  });
  const cancelMutation = useMutation({
    mutationKey: projectMemoryMutationKey(
      privateWork.scope,
      "dream-prepare-cancel",
    ),
    mutationFn: (jobId: string) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        cancelProjectMemoryDreamPreparation(privateWork, jobId, signal),
      ),
    onSuccess: (value) => {
      queryClient.setQueryData(queryKey, value);
    },
  });
  const start = useCallback(
    (operationId: string) => startMutation.mutateAsync(operationId),
    [startMutation],
  );
  const cancel = useCallback(async () => {
    const jobId = statusQuery.data?.jobId;
    if (jobId) await cancelMutation.mutateAsync(jobId);
  }, [cancelMutation, statusQuery.data?.jobId]);

  return {
    preparation: statusQuery.data ?? null,
    recovering: statusQuery.isLoading,
    starting: startMutation.isPending,
    cancelling: cancelMutation.isPending,
    start,
    cancel,
  };
}
