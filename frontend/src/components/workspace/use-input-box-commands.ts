"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";

import { GatewayApiError } from "@/core/api/errors";
import { fetch } from "@/core/api/fetcher";
import { useI18n } from "@/core/i18n/hooks";
import {
  getProjectMemory,
  restoreProjectMemoryVersion,
} from "@/core/private-work/memory/api";
import { useMemoryDreamPreparation } from "@/core/private-work/memory/preparation-hooks";
import { projectMemoryRootQueryKey } from "@/core/private-work/memory/query-keys";
import { commitProjectMemoryCacheChange } from "@/core/private-work/memory-freshness";
import { privateWorkQueryKey } from "@/core/private-work/query-keys";
import type { PrivateWorkAccess } from "@/core/private-work/types";
import type { GoalState } from "@/core/threads";
import { compactThreadContext } from "@/core/threads/api";
import { threadContextUsageQueryKey } from "@/core/threads/context-usage";
import { threadTokenUsageQueryKey } from "@/core/threads/token-usage";

import {
  abortGoalRequest,
  beginGoalRequest,
  createGoalRequestState,
  finishGoalRequest,
  isAbortError,
  isCurrentGoalRequest,
  readGoalResponseError,
  type GoalCommand,
} from "./input-box-helpers";
import {
  memoryDreamPreparationLabelKind,
  memoryDreamPreparationTerminalNotice,
} from "./memory-dream-preparation-view-model";

export type UseInputBoxCommandsOptions = {
  clearMemoryCommandInput: () => void;
  compactCommandEnabled: boolean;
  isMock?: boolean;
  markLatestCheckpoint: () => void;
  memoryRoutePath?: string;
  onGoalChange?: (goal: GoalState | null) => void;
  privateWork: PrivateWorkAccess;
  threadExists: boolean;
  threadId: string;
};

export function useInputBoxCommands({
  clearMemoryCommandInput,
  compactCommandEnabled,
  isMock,
  markLatestCheckpoint,
  memoryRoutePath,
  onGoalChange,
  privateWork,
  threadExists,
  threadId,
}: UseInputBoxCommandsOptions) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const router = useRouter();
  const goalRequestStateRef = useRef(createGoalRequestState());
  const compactRequestStateRef = useRef(createGoalRequestState());
  const dreamRestoreRequestStateRef = useRef(createGoalRequestState());
  const commandScopeSequenceRef = useRef(0);
  const mountedRef = useRef(true);
  const dreamPreparationNotificationRef = useRef<string | null>(null);
  const [pendingDreamRestoreVersion, setPendingDreamRestoreVersion] = useState<
    number | null
  >(null);
  const [restoringMemoryVersion, setRestoringMemoryVersion] = useState(false);
  const dreamPreparation = useMemoryDreamPreparation({
    privateWork,
    threadId,
    enabled: compactCommandEnabled && threadExists && !isMock,
  });

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const cleanupCommandRequests = useCallback(() => {
    commandScopeSequenceRef.current += 1;
    abortGoalRequest(goalRequestStateRef.current);
    abortGoalRequest(compactRequestStateRef.current);
    abortGoalRequest(dreamRestoreRequestStateRef.current);
  }, []);

  const handleGoalCommand = useCallback(
    async (command: GoalCommand): Promise<boolean> => {
      const request = beginGoalRequest(goalRequestStateRef.current, threadId);
      const signal = request.controller.signal;
      try {
        let goal: GoalState | null = null;
        if (command.kind === "status") {
          const response = await fetch(
            `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
              threadId,
            )}/goal`,
            { method: "GET", signal },
          );
          if (!response.ok) {
            throw new Error(await readGoalResponseError(response));
          }
          goal =
            ((await response.json()) as { goal?: GoalState | null }).goal ??
            null;
          if (
            !isCurrentGoalRequest(
              goalRequestStateRef.current,
              request,
              threadId,
            )
          ) {
            throw new DOMException("Goal request superseded", "AbortError");
          }
          const objective = goal?.objective;
          toast.info(
            objective !== undefined
              ? // Function replacer so a goal containing `$&`/`$1` isn't
                // interpreted as a replacement pattern.
                t.inputBox.goalActive.replace("{goal}", () => objective)
              : t.inputBox.goalNone,
          );
          onGoalChange?.(goal);
        } else if (command.kind === "clear") {
          const response = await fetch(
            `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
              threadId,
            )}/goal`,
            { method: "DELETE", signal },
          );
          if (!response.ok) {
            throw new Error(await readGoalResponseError(response));
          }
          if (
            !isCurrentGoalRequest(
              goalRequestStateRef.current,
              request,
              threadId,
            )
          ) {
            throw new DOMException("Goal request superseded", "AbortError");
          }
          toast.success(t.inputBox.goalCleared);
          onGoalChange?.(null);
        } else {
          const response = await fetch(
            `${privateWork.apiBaseURL}/threads/${encodeURIComponent(
              threadId,
            )}/goal`,
            {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ objective: command.objective }),
              signal,
            },
          );
          if (!response.ok) {
            throw new Error(await readGoalResponseError(response));
          }
          goal =
            ((await response.json()) as { goal?: GoalState | null }).goal ??
            null;
          if (
            !isCurrentGoalRequest(
              goalRequestStateRef.current,
              request,
              threadId,
            )
          ) {
            throw new DOMException("Goal request superseded", "AbortError");
          }
          toast.success(t.inputBox.goalSet);
          onGoalChange?.(goal);
        }
        return true;
      } catch (error) {
        if (
          isAbortError(error) ||
          !isCurrentGoalRequest(goalRequestStateRef.current, request, threadId)
        ) {
          throw error;
        }
        toast.error(
          error instanceof Error ? error.message : t.inputBox.goalFailed,
        );
        throw error;
      } finally {
        finishGoalRequest(goalRequestStateRef.current, request);
      }
    },
    [
      onGoalChange,
      t.inputBox.goalActive,
      t.inputBox.goalCleared,
      t.inputBox.goalFailed,
      t.inputBox.goalNone,
      t.inputBox.goalSet,
      privateWork.apiBaseURL,
      threadId,
    ],
  );

  const handleCompactCommand = useCallback(async (): Promise<void> => {
    if (!threadExists) {
      toast.info(t.inputBox.compactSkipped);
      return;
    }
    const request = beginGoalRequest(compactRequestStateRef.current, threadId);
    const signal = request.controller.signal;
    try {
      const result = await compactThreadContext(threadId, {
        apiBaseURL: privateWork.apiBaseURL,
        signal,
      });
      if (
        !isCurrentGoalRequest(compactRequestStateRef.current, request, threadId)
      ) {
        throw new DOMException("Compact request superseded", "AbortError");
      }
      if (result.compacted) {
        markLatestCheckpoint();
        clearMemoryCommandInput();
        toast.success(t.inputBox.compactSuccess);
      } else {
        toast.info(t.inputBox.compactSkipped);
      }
      await Promise.all([
        queryClient.invalidateQueries({
          queryKey: privateWorkQueryKey(privateWork.scope, "thread", threadId),
        }),
        queryClient.invalidateQueries({
          queryKey: privateWorkQueryKey(
            privateWork.scope,
            ...threadTokenUsageQueryKey(threadId),
          ),
        }),
        queryClient.invalidateQueries({
          queryKey: privateWorkQueryKey(
            privateWork.scope,
            ...threadContextUsageQueryKey(threadId),
          ),
        }),
        ...(result.compacted
          ? [
              commitProjectMemoryCacheChange(
                queryClient,
                privateWork.scope,
                "pending",
              ),
            ]
          : []),
      ]);
    } catch (error) {
      if (
        isAbortError(error) ||
        !isCurrentGoalRequest(compactRequestStateRef.current, request, threadId)
      ) {
        throw error;
      }
      toast.error(
        error instanceof Error ? error.message : t.inputBox.compactFailed,
      );
      throw error;
    } finally {
      finishGoalRequest(compactRequestStateRef.current, request);
    }
  }, [
    clearMemoryCommandInput,
    markLatestCheckpoint,
    queryClient,
    t.inputBox.compactFailed,
    t.inputBox.compactSkipped,
    t.inputBox.compactSuccess,
    privateWork,
    threadExists,
    threadId,
  ]);

  const handleDreamCommand = useCallback(async (): Promise<void> => {
    if (!threadExists) {
      toast.info(t.inputBox.dreamRequiresThread);
      return;
    }
    const commandScopeSequence = commandScopeSequenceRef.current;
    try {
      const result = await dreamPreparation.start(crypto.randomUUID());
      if (
        !mountedRef.current ||
        commandScopeSequenceRef.current !== commandScopeSequence
      ) {
        throw new DOMException("Dream request superseded", "AbortError");
      }
      clearMemoryCommandInput();
      if (result.disposition === "already_running") {
        toast.info(t.inputBox.dreamAlreadyRunning);
      } else {
        toast.success(t.inputBox.dreamPreparationStarted);
      }
    } catch (error) {
      if (
        isAbortError(error) ||
        !mountedRef.current ||
        commandScopeSequenceRef.current !== commandScopeSequence
      ) {
        throw error;
      }
      toast.error(
        error instanceof Error ? error.message : t.inputBox.dreamFailed,
      );
      throw error;
    }
  }, [
    clearMemoryCommandInput,
    dreamPreparation,
    t.inputBox.dreamAlreadyRunning,
    t.inputBox.dreamFailed,
    t.inputBox.dreamPreparationStarted,
    t.inputBox.dreamRequiresThread,
    threadExists,
  ]);

  const handleDreamLogCommand = useCallback(
    (version: number | null): void => {
      if (!memoryRoutePath) {
        toast.error(t.inputBox.dreamRouteUnavailable);
        return;
      }
      clearMemoryCommandInput();
      const query = version === null ? "" : `?version=${version}`;
      router.push(`${memoryRoutePath}${query}`);
    },
    [
      clearMemoryCommandInput,
      memoryRoutePath,
      router,
      t.inputBox.dreamRouteUnavailable,
    ],
  );

  useEffect(() => {
    const preparation = dreamPreparation.preparation;
    if (!preparation) return;
    if (preparation.compactedPasses > 0) {
      markLatestCheckpoint();
    }
    const terminalNotice = memoryDreamPreparationTerminalNotice(preparation);
    if (terminalNotice.kind === "none") return;
    const notificationKey = `${preparation.jobId}:${preparation.status}`;
    if (dreamPreparationNotificationRef.current === notificationKey) return;
    dreamPreparationNotificationRef.current = notificationKey;
    void Promise.all([
      queryClient.invalidateQueries({
        queryKey: privateWorkQueryKey(privateWork.scope, "thread", threadId),
      }),
      queryClient.invalidateQueries({
        queryKey: privateWorkQueryKey(
          privateWork.scope,
          ...threadTokenUsageQueryKey(threadId),
        ),
      }),
      queryClient.invalidateQueries({
        queryKey: privateWorkQueryKey(
          privateWork.scope,
          ...threadContextUsageQueryKey(threadId),
        ),
      }),
      commitProjectMemoryCacheChange(queryClient, privateWork.scope, "pending"),
      commitProjectMemoryCacheChange(
        queryClient,
        privateWork.scope,
        "document",
      ),
    ]).catch(() => undefined);
    if (terminalNotice.kind === "nothing_pending") {
      toast.info(t.inputBox.dreamNothingPending);
    } else if (terminalNotice.kind === "already_running") {
      toast.info(t.inputBox.dreamAlreadyRunning);
    } else if (terminalNotice.kind === "budget_rewrite") {
      toast.success(t.projectMemory.dreamQueuedBudget);
    } else if (terminalNotice.kind === "queued") {
      toast.success(
        t.inputBox.dreamQueued.replace(
          "{count}",
          String(terminalNotice.historyCount),
        ),
      );
    } else if (terminalNotice.kind === "cancelled") {
      toast.info(t.inputBox.dreamPreparationCancelled);
    } else {
      toast.error(t.inputBox.dreamFailed);
    }
  }, [
    dreamPreparation.preparation,
    markLatestCheckpoint,
    privateWork.scope,
    queryClient,
    t.inputBox.dreamAlreadyRunning,
    t.inputBox.dreamFailed,
    t.inputBox.dreamNothingPending,
    t.inputBox.dreamPreparationCancelled,
    t.inputBox.dreamQueued,
    t.projectMemory.dreamQueuedBudget,
    threadId,
  ]);

  const dreamPreparationLabel = useMemo(() => {
    const preparation = dreamPreparation.preparation;
    if (!preparation) return null;
    const labels = {
      queued: t.inputBox.dreamPreparationQueued,
      running: t.inputBox.dreamPreparationRunning,
      verifying: t.inputBox.dreamPreparationVerifying,
      completed: t.inputBox.dreamPreparationCompleted,
      cancelled: t.inputBox.dreamPreparationCancelled,
      failed: t.inputBox.dreamPreparationFailed,
    } as const;
    return labels[memoryDreamPreparationLabelKind(preparation)];
  }, [dreamPreparation.preparation, t.inputBox]);

  const handleDreamRestoreCommand = useCallback(
    (version: number): void => {
      if (!memoryRoutePath) {
        toast.error(t.inputBox.dreamRouteUnavailable);
        return;
      }
      clearMemoryCommandInput();
      setPendingDreamRestoreVersion(version);
    },
    [
      clearMemoryCommandInput,
      memoryRoutePath,
      t.inputBox.dreamRouteUnavailable,
    ],
  );

  const dismissDreamRestore = useCallback(() => {
    setPendingDreamRestoreVersion(null);
  }, []);

  const confirmDreamRestore = useCallback(async (): Promise<void> => {
    if (pendingDreamRestoreVersion === null || !memoryRoutePath) return;
    const request = beginGoalRequest(
      dreamRestoreRequestStateRef.current,
      threadId,
    );
    setRestoringMemoryVersion(true);
    try {
      const current = await getProjectMemory(
        privateWork,
        request.controller.signal,
      );
      const result = await restoreProjectMemoryVersion(
        privateWork,
        pendingDreamRestoreVersion,
        { expectedCurrentVersion: current.version },
        request.controller.signal,
      );
      if (
        !isCurrentGoalRequest(
          dreamRestoreRequestStateRef.current,
          request,
          threadId,
        )
      ) {
        throw new DOMException("Memory restore superseded", "AbortError");
      }
      await commitProjectMemoryCacheChange(
        queryClient,
        privateWork.scope,
        "document",
      );
      if (
        !isCurrentGoalRequest(
          dreamRestoreRequestStateRef.current,
          request,
          threadId,
        )
      ) {
        throw new DOMException("Memory restore superseded", "AbortError");
      }
      setPendingDreamRestoreVersion(null);
      toast.success(
        t.inputBox.dreamRestoreSuccess.replace(
          "{version}",
          String(result.version),
        ),
      );
      router.push(`${memoryRoutePath}?version=${result.version}`);
    } catch (error) {
      if (
        isAbortError(error) ||
        !isCurrentGoalRequest(
          dreamRestoreRequestStateRef.current,
          request,
          threadId,
        )
      ) {
        throw error;
      }
      if (error instanceof GatewayApiError && error.status === 409) {
        await queryClient.invalidateQueries({
          queryKey: projectMemoryRootQueryKey(privateWork.scope),
        });
        if (
          !isCurrentGoalRequest(
            dreamRestoreRequestStateRef.current,
            request,
            threadId,
          )
        ) {
          throw error;
        }
      }
      toast.error(
        error instanceof Error ? error.message : t.inputBox.dreamRestoreFailed,
      );
      throw error;
    } finally {
      const activeController = dreamRestoreRequestStateRef.current.controller;
      if (
        mountedRef.current &&
        (activeController === request.controller || activeController === null)
      ) {
        setRestoringMemoryVersion(false);
      }
      finishGoalRequest(dreamRestoreRequestStateRef.current, request);
    }
  }, [
    memoryRoutePath,
    pendingDreamRestoreVersion,
    privateWork,
    queryClient,
    router,
    t.inputBox.dreamRestoreFailed,
    t.inputBox.dreamRestoreSuccess,
    threadId,
  ]);

  return {
    cleanupCommandRequests,
    confirmDreamRestore,
    dismissDreamRestore,
    dreamPreparation: dreamPreparation.preparation,
    dreamPreparationCancel: dreamPreparation.cancel,
    dreamPreparationCancelling: dreamPreparation.cancelling,
    dreamPreparationLabel,
    handleCompactCommand,
    handleDreamCommand,
    handleDreamLogCommand,
    handleDreamRestoreCommand,
    handleGoalCommand,
    pendingDreamRestoreVersion,
    restoringMemoryVersion,
  };
}
