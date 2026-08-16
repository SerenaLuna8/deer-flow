"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { PrivateWorkAccess } from "@/core/private-work/types";

import { fetchActiveExecutionApproval, fetchExecutionApproval } from "./api";
import {
  executionApprovalActiveQueryKey,
  executionApprovalQueryKey,
  executionApprovalRootQueryKey,
} from "./query-keys";
import {
  executionApprovalIsActive,
  selectNewerExecutionApprovalProjection,
  type ExecutionApprovalsActiveResponse,
} from "./schemas";

export const EXECUTION_APPROVAL_POLL_INTERVAL_MS = 1_000;

type ExecutionApprovalQuerySnapshot = {
  state: {
    status: "pending" | "error" | "success";
    data?: ExecutionApprovalsActiveResponse;
  };
};

export function executionApprovalByIdRefetchInterval(
  query: ExecutionApprovalQuerySnapshot,
) {
  if (query.state.status === "error") return false;
  const approval = query.state.data?.approval;
  if (query.state.status === "success" && approval === null) {
    return EXECUTION_APPROVAL_POLL_INTERVAL_MS;
  }
  return executionApprovalIsActive(approval) ||
    (approval?.status === "denied" &&
      approval.denial_delivery_status === "pending")
    ? EXECUTION_APPROVAL_POLL_INTERVAL_MS
    : false;
}

function normalizeOptionalApprovalId(value: string | null | undefined) {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  return trimmed;
}

export type ObservedExecutionApprovalAnchor = {
  threadId: string;
  approvalId: string;
};

export function shouldTrackPersistedExecutionApproval({
  activeApprovalId,
  persistedApprovalId,
}: {
  activeApprovalId?: string | null;
  persistedApprovalId?: string | null;
}) {
  return (
    !activeApprovalId ||
    !persistedApprovalId ||
    activeApprovalId === persistedApprovalId
  );
}

export function resolveObservedExecutionApprovalAnchor({
  activeApprovalId,
  current,
  persistedApprovalId,
  persistedApprovalChanged = false,
  threadId,
}: {
  activeApprovalId?: string | null;
  current: ObservedExecutionApprovalAnchor | null;
  persistedApprovalId?: string | null;
  persistedApprovalChanged?: boolean;
  threadId: string;
}): ObservedExecutionApprovalAnchor | null {
  if (activeApprovalId) {
    return current?.threadId === threadId &&
      current.approvalId === activeApprovalId
      ? current
      : { threadId, approvalId: activeApprovalId };
  }
  if (persistedApprovalId) {
    if (
      current?.threadId !== threadId ||
      current.approvalId === persistedApprovalId ||
      persistedApprovalChanged
    ) {
      return { threadId, approvalId: persistedApprovalId };
    }
  }
  if (current?.threadId === threadId) return current;
  if (persistedApprovalId) {
    return { threadId, approvalId: persistedApprovalId };
  }
  return null;
}

export function useActiveExecutionApproval({
  privateWork,
  threadId,
  enabled = true,
}: {
  privateWork: PrivateWorkAccess;
  threadId: string;
  enabled?: boolean;
}) {
  return useQuery({
    queryKey: executionApprovalActiveQueryKey(privateWork.scope, threadId),
    queryFn: ({ signal }) =>
      fetchActiveExecutionApproval(privateWork, threadId, signal),
    enabled: enabled && threadId.length > 0,
    refetchInterval: (query) =>
      executionApprovalIsActive(query.state.data?.approval)
        ? EXECUTION_APPROVAL_POLL_INTERVAL_MS
        : false,
    refetchIntervalInBackground: false,
    refetchOnMount: "always",
    refetchOnReconnect: "always",
    refetchOnWindowFocus: "always",
  });
}

export function useExecutionApproval({
  privateWork,
  threadId,
  approvalId,
  enabled = true,
}: {
  privateWork: PrivateWorkAccess;
  threadId: string;
  approvalId?: string | null;
  enabled?: boolean;
}) {
  const selectedApprovalId = approvalId?.trim() ? approvalId : null;
  return useQuery({
    queryKey: selectedApprovalId
      ? executionApprovalQueryKey(
          privateWork.scope,
          threadId,
          selectedApprovalId,
        )
      : [
          ...executionApprovalRootQueryKey(privateWork.scope, threadId),
          "inactive",
        ],
    queryFn: ({ signal }) => {
      if (!selectedApprovalId) {
        throw new Error("Execution approval id is required");
      }
      return fetchExecutionApproval(
        privateWork,
        threadId,
        selectedApprovalId,
        signal,
      );
    },
    enabled: enabled && threadId.length > 0 && selectedApprovalId !== null,
    refetchInterval: executionApprovalByIdRefetchInterval,
    refetchIntervalInBackground: false,
    refetchOnMount: "always",
    refetchOnReconnect: "always",
    refetchOnWindowFocus: "always",
  });
}

export function useThreadExecutionApproval({
  privateWork,
  threadId,
  persistedApprovalId = null,
  enabled = true,
}: {
  privateWork: PrivateWorkAccess;
  threadId: string;
  persistedApprovalId?: string | null;
  enabled?: boolean;
}) {
  const active = useActiveExecutionApproval({
    privateWork,
    threadId,
    enabled,
  });
  const [observed, setObserved] =
    useState<ObservedExecutionApprovalAnchor | null>(null);
  const lastPersistedRef = useRef<{
    threadId: string;
    approvalId: string | null;
  } | null>(null);
  const activeApproval = active.data?.approval ?? null;
  const selectedPersistedApprovalId =
    normalizeOptionalApprovalId(persistedApprovalId);

  useEffect(() => {
    const activeApprovalId = activeApproval?.approval_id;
    const previousPersisted = lastPersistedRef.current;
    const persistedApprovalChanged =
      selectedPersistedApprovalId !== null &&
      (previousPersisted?.threadId !== threadId ||
        previousPersisted.approvalId !== selectedPersistedApprovalId);
    if (
      shouldTrackPersistedExecutionApproval({
        activeApprovalId,
        persistedApprovalId: selectedPersistedApprovalId,
      })
    ) {
      lastPersistedRef.current = {
        threadId,
        approvalId: selectedPersistedApprovalId,
      };
    }
    setObserved((current) =>
      resolveObservedExecutionApprovalAnchor({
        activeApprovalId,
        current,
        persistedApprovalId: selectedPersistedApprovalId,
        persistedApprovalChanged,
        threadId,
      }),
    );
  }, [activeApproval?.approval_id, selectedPersistedApprovalId, threadId]);

  const observedApprovalId =
    activeApproval?.approval_id ??
    (observed?.threadId === threadId ? observed.approvalId : null) ??
    selectedPersistedApprovalId;
  const byId = useExecutionApproval({
    privateWork,
    threadId,
    approvalId: observedApprovalId,
    enabled,
  });
  const byIdApproval = byId.data?.approval ?? null;
  const matchingByIdApproval =
    byIdApproval?.approval_id === observedApprovalId ? byIdApproval : null;
  const matchingActiveApproval =
    activeApproval?.approval_id === observedApprovalId ? activeApproval : null;
  const approval = selectNewerExecutionApprovalProjection(
    matchingByIdApproval,
    matchingActiveApproval,
  );
  const isPreparing =
    approval === null &&
    observedApprovalId !== null &&
    (byId.isPending || (byId.isSuccess && byId.data?.approval === null));

  return {
    active,
    byId,
    approval,
    isPreparing,
    observedApprovalId,
  } as const;
}
