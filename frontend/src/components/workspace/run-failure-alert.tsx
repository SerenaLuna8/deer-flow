"use client";

import { useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/core/i18n/hooks";
import {
  MODEL_OUTPUT_LIMIT,
  OUTPUT_DELIVERY_INCOMPLETE,
  SIDE_EFFECT_STATE_UNKNOWN,
  type ProjectRunFailureCode,
} from "@/core/private-work/api-client";
import { resolveRunFailureCopy } from "@/core/threads/run-failure-presentation";

export function canRetryModelOutputLimit({
  canRun,
  isRunLoading,
  hasRegenerationTarget,
  retrySurfaceAvailable,
}: {
  canRun: boolean;
  isRunLoading: boolean;
  hasRegenerationTarget: boolean;
  retrySurfaceAvailable: boolean;
}): boolean {
  return (
    canRun && !isRunLoading && hasRegenerationTarget && retrySurfaceAvailable
  );
}

export function canRestoreRunFailureInput(
  failureCode: ProjectRunFailureCode | null,
): boolean {
  return failureCode !== MODEL_OUTPUT_LIMIT && canReplayRunFailure(failureCode);
}

export function canReplayRunFailure(
  failureCode: ProjectRunFailureCode | null,
): boolean {
  return (
    failureCode !== OUTPUT_DELIVERY_INCOMPLETE &&
    failureCode !== SIDE_EFFECT_STATE_UNKNOWN
  );
}

export function shouldShowRunFailureAlert({
  hasTerminalRunFailure,
}: {
  hasTerminalRunFailure: boolean;
  streamError: unknown;
}): boolean {
  // Stream errors can occur before admission or while refreshing history. The
  // durable Run status is the authority for the terminal-failure surface.
  return hasTerminalRunFailure;
}

export function RunFailureAlert({
  failureCode,
  retryDisabled = false,
  onRetryWithoutThinking,
  onRestoreInput,
}: {
  failureCode: ProjectRunFailureCode | null;
  retryDisabled?: boolean;
  onRetryWithoutThinking?: () => boolean | Promise<boolean>;
  onRestoreInput?: () => void;
}) {
  const { t } = useI18n();
  const [retrying, setRetrying] = useState(false);
  const isModelOutputLimit = failureCode === MODEL_OUTPUT_LIMIT;
  const copy = resolveRunFailureCopy(t.conversation, failureCode);

  return (
    <Alert
      variant="destructive"
      className="border-destructive/30 bg-destructive/5 mb-3"
      data-testid="run-failure-alert"
      data-run-failure-code={failureCode ?? "generic"}
    >
      <AlertTitle>{copy.title}</AlertTitle>
      <AlertDescription className="flex flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
        <span>{copy.description}</span>
        {isModelOutputLimit && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="shrink-0"
            disabled={retrying || retryDisabled || !onRetryWithoutThinking}
            onClick={() => {
              if (!onRetryWithoutThinking) {
                return;
              }
              setRetrying(true);
              void Promise.resolve()
                .then(onRetryWithoutThinking)
                .catch(() => false)
                .finally(() => {
                  setRetrying(false);
                });
            }}
          >
            {retrying
              ? t.conversation.modelOutputLimitRetrying
              : t.conversation.modelOutputLimitRetry}
          </Button>
        )}
        {canRestoreRunFailureInput(failureCode) && onRestoreInput && (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="shrink-0"
            onClick={onRestoreInput}
          >
            {t.conversation.restoreFailedInput}
          </Button>
        )}
      </AlertDescription>
    </Alert>
  );
}
